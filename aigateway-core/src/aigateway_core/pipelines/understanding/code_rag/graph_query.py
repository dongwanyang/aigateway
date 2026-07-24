"""通过官方 codegraph CLI 子进程查询符号级 callers/callees/imports.

重构后不再手写 SQLite 查询:callers/callees/影响范围全部走 codegraph CLI
(`@colbymchenry/codegraph` v1.4.1) 的 JSON 输出,由 CLI 自己产的 db schema 最清楚,
避免我们手写 SQL 踩 `src/` 前缀、`_` 通配符等坑。

CLI 接口实测(v1.4.1)—— `--json` 与 `-p` 支持不一致,必须分两类 helper:

| 命令 | `--json` | `-p/--path` | 用法 |
|---|---|---|---|
| query/callers/callees/impact/files | ✅ | ✅ | _run_codegraph_json |
| node | ❌ 无 | ✅ | _run_codegraph_raw(node 无 --json,markdown 输出) |
| init/sync | ❌ | ❌(位置参数 [path]) | _run_codegraph_raw(位置参数) |

graph_repo_path 指向 `{graph_db_dir}/{document_id}/` 目录(codegraph CLI 用
`-p {graph_repo_path}` 查询,读 `{graph_repo_path}/.codegraph/codegraph.db`)。
db 里 `file_path` 带 `src/` 前缀(graph_builder 把源码 symlink 成 work_dir/src),
调用方传入的 file_path 是相对源根路径(无 src/ 前缀)——本模块内部统一按后缀归一化匹配。

对外暴露两类接口:
- strict import-time API:任何 CLI/解析问题都抛异常,导入整体 failed
- tolerant retrieval-time API:任何问题都返回空结果,不打断主链路

imports 字段:codegraph CLI 没有 imports 查询命令,但 db 里 `edges.kind='contains'`
连接 file node → import node。这里用一个**有界的 sqlite 读取**(只读、单文件、
点查询)取 imports,与 callers/callees(走 CLI)分离。imports 在下游只进 payload、
不参与检索逻辑(callers/callees 才是检索增强用的)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

_EMPTY_METADATA: dict[str, Any] = {
    "callers": [],
    "callees": [],
    "imports": [],
    "chunk_type": "module",
    "function_name": None,
    "class_name": None,
}

# 检索路径的 edges 缓存:key=graph_repo_path, value=(file_hashes_snapshot, callers_map, callees_map)。
# 失效靠 files.content_hash 快照比对(增量 sync 改 db → 下次检索自动重建)。
# split 阶段不进缓存(导入时只跑一次 read_call_edges_strict 用完即丢,不污染)。
# 有界 LRU(cachetools):每个仓库 ~2.8MB map,32 仓库上限 ~90MB,防长期累积泄漏;
# 仓库删除时主动 pop(见 invalidate_edges_cache)。
#
# 线程安全:cachetools.LRUCache 的 __getitem__ 会 __touch 内部 OrderedDict(读即写),
# 并发 get/set/pop 会抛 "OrderedDict mutated during iteration" 或丢条目。本缓存被两类
# 线程访问 —— admin /callers 路由在 run_in_executor 的默认线程池,RAG retrieval 在
# event-loop 线程,外加 invalidate 在 loop 线程。故所有缓存访问串行化在 _cache_lock 下;
# SQL 重建(read_call_edges_strict,~114ms)留在锁外,不阻塞其它仓库的缓存命中。
import threading
from cachetools import LRUCache  # type: ignore[import-untyped]
_edges_cache: LRUCache = LRUCache(maxsize=32)
_cache_lock = threading.Lock()

# 损坏哨兵:read_call_edges_strict 抛错时缓存此值,避免后续每次检索都重新打开损坏 db
# 重抛(性能悬崖)。哨兵让重试有界——直到 files.content_hash 变化(下次 sync)才重建。
# 结构同正常条目 (snapshot, callers, callees),但 callers/callees 为 None 标记"损坏"。
_CORRUPT_MARK = None


def _cache_key(graph_repo_path: str) -> str:
    """统一缓存键:解析为绝对路径,消除符号链接/相对路径/尾斜杠差异。
    保证 seed/retrieval 用的 key 与 invalidate_edges_cache(pop)用的 key 一致。
    """
    return str(Path(graph_repo_path).resolve())


def invalidate_edges_cache(graph_repo_path: str) -> None:
    """删除某仓库的 edges 缓存条目。仓库被删时调,避免残留失效条目。"""
    with _cache_lock:
        _edges_cache.pop(_cache_key(graph_repo_path), None)

# codegraph CLI 默认查询超时(秒)。查询类命令都是点查,30s 足够;build/sync 用
# graph_builder.py 里更长的 timeout。
_QUERY_TIMEOUT = 30.0


def _graph_db_path(graph_repo_path: str) -> str:
    """从 graph_repo_path 目录推出 .codegraph/codegraph.db 绝对路径。"""
    return str(Path(graph_repo_path) / ".codegraph" / "codegraph.db")


def read_file_hashes(graph_repo_path: str) -> dict[str, str]:
    """读 db 的 files 表,返回 {path: content_hash} 快照(供增量 sync 前后对比)。

    db 里 path 带 src/ 前缀;这里原样返回(与 nodes.file_path 一致),
    调用方按需剥前缀对齐 Qdrant payload 的 file_path(无 src/ 前缀)。
    """
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT path, content_hash FROM files").fetchall()
            return {str(p): str(h) for (p, h) in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def read_call_edges(graph_repo_path: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """一次查全图 callers/callees,返回 (callers_map, callees_map)。

    比"逐符号 spawn codegraph CLI"快 ~5000x(5k 符号:114ms vs 10k 子进程)。
    key=node id(精确,避免同名跨文件误并);value=ref 行列表。
    file_path 保留 src/ 前缀(与 read_file_hashes 一致,调用方按需剥)。

    db 不存在或异常返回 ({}, {})(retrieval 容忍 —— 绝不让检索链因 db 问题断掉)。
    """
    return _read_call_edges_impl(graph_repo_path, strict=False)


def read_call_edges_strict(graph_repo_path: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """严格版:用于 import 阶段。db 不存在返回 ({}, {})(正常:图谱还没建);
    但真正的 sqlite3.Error(db 损坏/schema 异常)抛出,不吞 —— 让 import 失败暴露,
    而不是静默降级成空 callers/callees 误判导入"成功"。
    """
    return _read_call_edges_impl(graph_repo_path, strict=True)


def _read_call_edges_impl(
    graph_repo_path: str, *, strict: bool
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return {}, {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            callees: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute(
                """
                SELECT e.source AS sym_id, t.name, t.kind, t.file_path, t.start_line, t.end_line
                FROM edges e JOIN nodes t ON t.id = e.target
                WHERE e.kind = 'calls'
                """
            ):
                callees.setdefault(str(r["sym_id"]), []).append({
                    "name": str(r["name"]),
                    "kind": str(r["kind"] or "function"),
                    "file_path": str(r["file_path"] or ""),
                    "start_line": int(r["start_line"] or 1),
                    "end_line": int(r["end_line"] or r["start_line"] or 1),
                })
            callers: dict[str, list[dict[str, Any]]] = {}
            for r in conn.execute(
                """
                SELECT e.target AS sym_id, s.name, s.kind, s.file_path, s.start_line, s.end_line
                FROM edges e JOIN nodes s ON s.id = e.source
                WHERE e.kind = 'calls'
                """
            ):
                callers.setdefault(str(r["sym_id"]), []).append({
                    "name": str(r["name"]),
                    "kind": str(r["kind"] or "function"),
                    "file_path": str(r["file_path"] or ""),
                    "start_line": int(r["start_line"] or 1),
                    "end_line": int(r["end_line"] or r["start_line"] or 1),
                })
            return callers, callees
        finally:
            conn.close()
    except sqlite3.Error:
        if strict:
            raise
        return {}, {}


def _get_cached_edges(graph_repo_path: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """懒加载 callers/callees map,按 file hash 快照失效。

    返回 (callers_map, callees_map)。hash 不变 → 复用缓存;变了/首次 → 重建。
    重建用 read_call_edges_strict —— db 存在但损坏时抛 sqlite3.Error,不静默缓存空 map
    (retrieval 路径的调用方包了 try/except 降级,strict 路径让它冒泡暴露)。
    db 不存在仍返回 ({},{}) —— 严格路径首次检索前图谱还没建是合法状态。

    线程安全:缓存读写串行化在 _cache_lock;SQL 重建在锁外(不阻塞其它仓库命中)。
    损坏 db:缓存 _CORRUPT_SENTINEL(哨兵),避免后续每次检索都重新打开损坏 db 重抛
    (性能悬崖);直到 files.content_hash 变化(下次 sync)才重建。
    """
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return {}, {}
    key = _cache_key(graph_repo_path)
    snapshot = read_file_hashes(graph_repo_path)
    with _cache_lock:
        cached = _edges_cache.get(key)
    if cached is not None and cached[0] == snapshot:
        # 损坏哨兵:hash 未变 → 不重试(避免性能悬崖),让调用方降级。
        # 非损坏 → 返回缓存的 maps。
        if cached[1] is _CORRUPT_MARK:
            raise sqlite3.Error(f"codegraph db 损坏(缓存哨兵命中): {graph_repo_path}")
        return cached[1], cached[2]
    try:
        callers, callees = read_call_edges_strict(graph_repo_path)
    except sqlite3.Error:
        # 缓存哨兵(带当前 snapshot):bound 重试,直到下次 file-hash 变化才重建。
        with _cache_lock:
            _edges_cache[key] = (snapshot, _CORRUPT_MARK, _CORRUPT_MARK)
        raise
    with _cache_lock:
        _edges_cache[key] = (snapshot, callers, callees)
    return callers, callees


def run_codegraph_sync(graph_repo_path: str, *, timeout: float = 1800.0) -> str:
    """跑 `codegraph sync <graph_repo_path>`(位置参数,不接 -p),返回 raw stdout。

    供增量 sync 端点用。cwd 指向 {graph_repo_path}(CLI 在此解析 src/ 源码与 .codegraph/)。
    """
    return _run_codegraph_raw(
        ["sync", graph_repo_path], cwd=graph_repo_path, timeout=timeout
    )


def _run_codegraph_json(
    args: list[str], *, repo_path: str, timeout: float = _QUERY_TIMEOUT
) -> Any:
    """跑支持 --json 的查询类命令(query/callers/callees/impact/files)。

    构造: codegraph <args...> -p <repo_path> --json。
    失败/超时/非 JSON 输出都抛 RuntimeError(strict 链路用)。

    用 Popen + start_new_session=True 启动子进程,超时后 killpg 收割整个
    进程组(含 codegraph spawn 的孙进程),避免孙进程变僵尸卡住导入。
    """
    import signal

    cmd = ["codegraph", *args, "-p", repo_path, "--json"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codegraph CLI 未安装;请在 gateway 镜像中安装 @colbymchenry/codegraph"
        ) from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # killpg 收割整个进程组(start_new_session=True 让子进程自成一组)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        raise RuntimeError(
            f"codegraph command timed out after {timeout}s: {' '.join(cmd)}"
        )

    if proc.returncode != 0:
        err = (stderr or stdout or "").strip()
        raise RuntimeError(
            f"codegraph command failed ({' '.join(cmd)}): {err[:2000]}"
        )

    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"codegraph 输出不是合法 JSON ({' '.join(cmd)}): {stdout[:500]}"
        ) from exc


def _run_codegraph_raw(
    args: list[str], *, cwd: Optional[str] = None, timeout: float = _QUERY_TIMEOUT
) -> str:
    """跑不支持 --json 的命令(node / init / sync),返回原始 stdout 文本。

    init/sync 用位置参数: `codegraph sync <path>`;不接 -p。
    node 接 -p: 由调用方在 args 里带 `-p <repo_path>`。

    用 Popen + start_new_session=True 启动,超时后 killpg 收割整个进程组。
    """
    import signal

    cmd = ["codegraph", *args]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codegraph CLI 未安装;请在 gateway 镜像中安装 @colbymchenry/codegraph"
        ) from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        raise RuntimeError(
            f"codegraph command timed out after {timeout}s: {' '.join(cmd)}"
        )

    if proc.returncode != 0:
        err = (stderr or stdout or "").strip()
        raise RuntimeError(
            f"codegraph command failed ({' '.join(cmd)}): {err[:2000]}"
        )
    return (stdout or "").strip()


def _classify_chunk_type(kind: str, symbol_name: str | None, chunk_text: str) -> str:
    if kind in ('function', 'method'):
        return 'function'
    if kind == 'class':
        return 'class'
    if chunk_text.lstrip().startswith('class '):
        return 'class'
    if symbol_name:
        return 'function'
    return 'module'


def _file_path_matches(stored: str, query_path: str) -> bool:
    """db 里 file_path 带 src/ 前缀(graph_builder symlink 成 work_dir/src),
    调用方传的是相对源根路径(无前缀)。按后缀匹配容忍任意前缀,加斜杠避免
    auth.py 误匹配 xauth.py。stored == query_path 也算命中(前缀已对齐的情况)。
    """
    if not stored:
        return False
    if stored == query_path:
        return True
    return stored.endswith("/" + query_path)


def _strip_builder_prefix(stored: str, query_path: str = "") -> str:
    """把 db 里带 src/ 前缀的 file_path 剥成相对源根路径(与 Qdrant payload 对齐)。

    retrieval 端用 BFS/impact 返回的 file_path 去 Qdrant scroll 相关 chunk,
    而 Qdrant 存的是 splitter 路径(无 src/ 前缀),必须剥掉前缀否则 scroll 全 miss。

    graph_builder 恒把源码 symlink 成 work_dir/src 后索引,所以 db 里 file_path
    一律带 src/ 前缀。这里直接剥掉已知前缀,不依赖 query_path(不同文件的 ref
    传进来的 query_path 是起点的文件路径,后缀匹配只对同文件 ref 成立,跨文件会
    误走 split 兜底——以前 prefix 偶尔不是 src/ 时会出错,现在恒剥 src/)。
    query_path 仅作幂等优化:若 stored 已等于它说明无前缀,原样返回。
    """
    if not stored:
        return stored
    if query_path and stored == query_path:
        return stored
    if stored.startswith("src/"):
        return stored[len("src/"):]
    if stored.startswith("src\\"):
        return stored[len("src\\"):]
    return stored


def _get_imports_for_file(graph_repo_path: str, file_path: str) -> list[str]:
    """从 db 读单文件的 import 符号名(有界 sqlite 点查询)。

    codegraph CLI 没有 imports 命令,只能直读 db。edges.kind='contains' 连接
    file node → import node。先按后缀匹配定位 file node(容忍 src/ 前缀),
    再取其 import 邻居。

    任何异常返回空列表(imports 不参与检索逻辑,失败可降级)。
    """
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            file_rows = conn.execute(
                "SELECT id, file_path FROM nodes WHERE kind = 'file'"
            ).fetchall()
            matched_ids = [
                str(r["id"]) for r in file_rows
                if _file_path_matches(str(r["file_path"] or ""), file_path)
            ]
            if not matched_ids:
                return []
            placeholders = ",".join("?" for _ in matched_ids)
            rows = conn.execute(
                f"""
                SELECT DISTINCT n.name
                FROM edges e
                JOIN nodes n ON n.id = e.target
                WHERE e.kind = 'contains'
                  AND e.source IN ({placeholders})
                  AND n.kind = 'import'
                  AND n.name IS NOT NULL
                  AND n.name != ''
                ORDER BY n.name
                """,
                matched_ids,
            ).fetchall()
            return [str(r["name"]) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _query_symbol_node(
    graph_repo_path: str, file_path: str, symbol_name: str
) -> Optional[dict[str, Any]]:
    """调 `codegraph query <symbol> --json`,过滤出 name==symbol 且 file_path 匹配的节点。

    query 是模糊搜索(带 score),可能返回近似名;这里要求精确 name 匹配。
    file_path 用后缀匹配容忍 src/ 前缀。匹配不到返回 None。
    """
    data = _run_codegraph_json(["query", symbol_name, "--limit", "50"], repo_path=graph_repo_path)
    if not data or not isinstance(data, list):
        return None
    candidates: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        node = item.get("node") or {}
        if str(node.get("name") or "") != symbol_name:
            continue
        stored = str(node.get("filePath") or "")
        if _file_path_matches(stored, file_path):
            return node
        # 暂存未匹配 file_path 的同名节点(同名跨文件时退而求其次)
        candidates.append(node)
    return candidates[0] if candidates else None


def lookup_symbol_metadata_strict(
    graph_repo_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """严格版:用于 import 阶段。任何 CLI/解析问题都抛异常。

    走 codegraph query + callers + callees;imports 走 db 点查询。
    """
    if not symbol_name:
        return dict(_EMPTY_METADATA)

    node = _query_symbol_node(graph_repo_path, file_path, symbol_name)
    imports = _get_imports_for_file(graph_repo_path, file_path)
    if node is None:
        result = dict(_EMPTY_METADATA)
        result['imports'] = imports
        return result

    kind = str(node.get("kind") or 'module')
    callers = [str(r["name"]) for r in get_callers(graph_repo_path, symbol_name)]
    callees = [str(r["name"]) for r in get_callees(graph_repo_path, symbol_name)]
    chunk_type = _classify_chunk_type(kind, symbol_name, chunk_text)
    return {
        'callers': callers,
        'callees': callees,
        'imports': imports,
        'chunk_type': chunk_type,
        'function_name': symbol_name if chunk_type == 'function' else None,
        'class_name': symbol_name if chunk_type == 'class' else None,
    }


def lookup_symbol_metadata(
    graph_repo_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """宽松版:用于 retrieval 阶段。任何问题都降级为空 metadata。"""
    try:
        return lookup_symbol_metadata_strict(graph_repo_path, file_path, symbol_name, chunk_text)
    except Exception:
        return dict(_EMPTY_METADATA)


def lookup_related_symbols_strict(
    graph_repo_path: str,
    file_path: str,
    symbol_name: str,
    *,
    hops: int = 1,
) -> list[dict[str, Any]]:
    """严格版:取符号的邻接符号(callers + callees 双向 BFS),替代手写 SQL BFS。

    codegraph impact 只追踪上游(callers 方向),不覆盖 callees。为了与旧 BFS
    行为一致(双向 N 跳),这里:
    - hops=1:直接 callers(codegraph callers)+ 直接 callees(codegraph callees)。
    - hops>=2:以 callers/callees 为 frontier,逐层 BFS,每层对 frontier 中每个
      符号再取其 callers/callees,共扩展 hops-1 层(总深度 = hops)。

    去重按 (symbol_name, file_path) 而非仅 symbol_name——同名跨文件/跨类方法是
    不同符号,不能因同名互相吞掉(旧代码按 name 去重会误并)。

    返回项:{file_path, symbol_name, kind, start_line, end_line}
    file_path 剥掉 src/ 前缀,与 Qdrant payload 路径对齐。
    """
    if hops <= 0:
        return []

    related: list[dict[str, Any]] = []
    # key=(symbol_name, file_path);起点不计入结果
    seen: set[tuple[str, str]] = {(symbol_name, file_path)}

    def _add_from_refs(refs: list[dict[str, Any]]) -> list[str]:
        """把新邻居并入 related;返回本轮新增的 symbol_name(下一跳 frontier)。"""
        new_frontier: list[str] = []
        for ref in refs:
            name = ref.get("name")
            if not name:
                continue
            stored = str(ref.get("file_path") or "")
            norm_path = _strip_builder_prefix(stored, file_path)
            key = (str(name), norm_path)
            if key in seen:
                continue
            seen.add(key)
            related.append(
                {
                    'file_path': norm_path,
                    'symbol_name': str(name),
                    'kind': str(ref.get("kind") or 'function'),
                    'start_line': int(ref.get("start_line") or 1),
                    'end_line': int(ref.get("end_line") or ref.get("start_line") or 1),
                }
            )
            new_frontier.append(str(name))
        return new_frontier

    # 第一跳:callers + callees
    frontier = _add_from_refs(get_callers(graph_repo_path, symbol_name))
    frontier += _add_from_refs(get_callees(graph_repo_path, symbol_name))

    # 更深跳数:逐层 BFS(hops-1 层,每层对当前 frontier 全体取 callers/callees)
    remaining_layers = hops - 1
    while remaining_layers > 0 and frontier:
        next_frontier: list[str] = []
        for sym in frontier:
            try:
                next_frontier += _add_from_refs(get_callers(graph_repo_path, sym))
                next_frontier += _add_from_refs(get_callees(graph_repo_path, sym))
            except Exception:
                continue
        frontier = next_frontier
        remaining_layers -= 1

    return related


def lookup_related_symbols(
    graph_repo_path: str,
    file_path: str,
    symbol_name: str,
    *,
    hops: int = 1,
) -> list[dict[str, Any]]:
    try:
        return lookup_related_symbols_strict(
            graph_repo_path, file_path, symbol_name, hops=hops
        )
    except Exception:
        return []


# ----------------------------------------------------------------------
# CLI / API 层用的查询函数(都走 codegraph CLI)
# ----------------------------------------------------------------------


def query_symbols(
    graph_repo_path: str,
    search: str,
    *,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """包装 `codegraph query <search> --json`。返回规范化节点列表。"""
    args = ["query", search, "--limit", str(limit)]
    if kind:
        args += ["--kind", kind]
    data = _run_codegraph_json(args, repo_path=graph_repo_path)
    if not data or not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        node = item.get("node") or {}
        if not isinstance(node, dict):
            continue
        out.append(_normalize_node(node))
    return out


def _lookup_symbol_id_by_name(graph_repo_path: str, symbol: str) -> str | None:
    """按 name 查 node id(精确,取第一个 kind IN function/method/class 的)。db 直读,~1ms。"""
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT id FROM nodes WHERE name=? AND kind IN ('function','method','class') LIMIT 1",
                (symbol,),
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def get_callers(graph_repo_path: str, symbol: str) -> list[dict[str, Any]]:
    """返回 caller 节点列表(db 直读,走缓存 map)。"""
    callers, _ = _get_cached_edges(graph_repo_path)
    sym_id = _lookup_symbol_id_by_name(graph_repo_path, symbol)
    if sym_id is None:
        return []
    return list(callers.get(sym_id, []))


def get_callees(graph_repo_path: str, symbol: str) -> list[dict[str, Any]]:
    """返回 callee 节点列表(db 直读,走缓存 map)。"""
    _, callees = _get_cached_edges(graph_repo_path)
    sym_id = _lookup_symbol_id_by_name(graph_repo_path, symbol)
    if sym_id is None:
        return []
    return list(callees.get(sym_id, []))


def get_impact(graph_repo_path: str, symbol: str, *, depth: int = 2) -> dict[str, Any]:
    """包装 `codegraph impact <symbol> --depth N --json`。返回原始结构(含 affected 列表)。"""
    data = _run_codegraph_json(
        ["impact", symbol, "--depth", str(depth)],
        repo_path=graph_repo_path,
    )
    if not data or not isinstance(data, dict):
        return {"symbol": symbol, "depth": depth, "affected": []}
    affected = [
        _normalize_ref(a)
        for a in (data.get("affected") or [])
        if isinstance(a, dict)
    ]
    return {
        "symbol": data.get("symbol") or symbol,
        "depth": data.get("depth") or depth,
        "node_count": data.get("nodeCount"),
        "edge_count": data.get("edgeCount"),
        "affected": affected,
    }


def list_files(graph_repo_path: str) -> list[dict[str, Any]]:
    """包装 `codegraph files --json`。返回文件列表。"""
    data = _run_codegraph_json(["files"], repo_path=graph_repo_path)
    if not data or not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "path": str(item.get("path") or ""),
                "language": str(item.get("language") or ""),
                "node_count": item.get("nodeCount"),
                "size": item.get("size"),
            }
        )
    return out


def get_node(graph_repo_path: str, symbol: str) -> dict[str, Any]:
    """包装 `codegraph node <symbol> -p <repo>`(raw,markdown 输出)。

    node 命令无 --json,输出 markdown:位置/签名/源码块(源文件在场时)/调用链 trail。
    返回 {symbol, raw, repo_path};调用方自行解析或直接展示 raw。
    源码块依赖 `{graph_repo_path}/src/` 下源文件在场(git/server_path 持久化源有;
    folder/zip 已删源 → 只有调用链 trail,无源码块)。
    """
    raw = _run_codegraph_raw(["node", symbol, "-p", graph_repo_path])
    return {"symbol": symbol, "raw": raw, "repo_path": graph_repo_path}


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """把 codegraph JSON 的 camelCase 节点字段归一化为 snake_case。"""
    return {
        "id": node.get("id"),
        "kind": node.get("kind"),
        "name": node.get("name"),
        "qualified_name": node.get("qualifiedName"),
        "file_path": node.get("filePath"),
        "language": node.get("language"),
        "start_line": node.get("startLine"),
        "end_line": node.get("endLine"),
        "signature": node.get("signature"),
        "docstring": node.get("docstring"),
        "is_async": node.get("isAsync"),
        "is_exported": node.get("isExported"),
    }


def _normalize_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """callers/callees/impact 里的引用项(camelCase → snake_case)。"""
    return {
        "name": ref.get("name"),
        "kind": ref.get("kind"),
        "file_path": ref.get("filePath"),
        "start_line": ref.get("startLine"),
        "end_line": ref.get("endLine"),
    }
