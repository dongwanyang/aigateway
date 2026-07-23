# Code RAG 导入卡死修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 Code RAG 仓库导入卡在 `splitting` 永不推进的问题，并防止网关重启后任务永久悬置。

**Architecture:** 把"逐符号 spawn codegraph CLI 子进程"换成"2 条 SQL 直读 db 的 `edges` 表"（splitter + graph_query 全改），检索路径加 file-hash 失效的进程内缓存；Redis 任务 key 换成 SQLite `code_rag_tasks` 表（终态留表当历史）；启动时扫非终态任务标 `failed`；splitting 阶段回写进度。

**Tech Stack:** Python 3 / asyncio / FastAPI / SQLite3（`SQLiteStore._Conn` wrapper, WAL 模式）/ codegraph CLI（`@colbymchenry/codegraph` v1.4.1，仅 `codegraph init`）/ pytest。

## Global Constraints

- 不改前端（`control-panel/`）：后端 JSON shape（`task_id/status/done/total/current_file/error/source_label/source_type/created_at`）保持不变。
- 不动 `split_code_directory`（老兼容路径，0 生产引用，单测在用）。
- `get_node` 保留 CLI（它要 markdown 源码块，db 无现成）。
- sync 端点（`POST /rag/code/repositories/{document_id}/sync`）不进任务表——它是同步阻塞调用，无 task_id。
- 所有 SQLite 写走 `SQLiteStore` 的 `_Conn`（WAL + per-thread connection），不裸 `sqlite3.connect`。
- 命令用 `python3`（无 `python` 别名）；测试用 `python3 -m pytest tests/ -q`。
- 导入任务 deadline 默认 3600s（`code_cfg.get("import_timeout_seconds") or 3600`），未改 schema。
- codegraph db 路径恒为 `graph_repo_path/.codegraph/codegraph.db`；`nodes.file_path` 恒带 `src/` 前缀（graph_builder symlink 约定）。

## File Structure

| 文件 | 职责 |
|---|---|
| `aigateway-core/.../code_rag/graph_query.py` | 新增 `read_call_edges`（2 SQL）+ 懒加载缓存（file-hash 失效）；`get_callers`/`get_callees`/`lookup_related_symbols`/`lookup_symbol_metadata` 改 db 读；`get_node` 保留 CLI；`subprocess` 加 `start_new_session`+`killpg` |
| `aigateway-core/.../code_rag/splitter.py` | `build_symbol_chunks` 加 `progress_cb`；循环前建 map，循环内 O(1) 查 |
| `aigateway-core/.../shared/auth/sqlite_store.py` | 新增 `code_rag_tasks` 表 schema + `upsert_code_rag_task`/`read_code_rag_task`/`list_code_rag_tasks`/`fail_non_terminal_tasks` 方法 |
| `aigateway-core/.../shared/config.py` | `allowed_top_level` 加 `"code_rag"` |
| `aigateway-api/.../code_rag_routes.py` | 任务状态 Redis→SQLite；`_delete_task_key` 移除；`list_code_tasks` 分页；新增 `sweep_orphaned_tasks`；进度回写走 SQLite；`source_label` 去前缀 |
| `aigateway-api/.../main.py` | lifespan 调 `sweep_orphaned_tasks` |
| `tests/test_code_rag_helpers.py` | 更新 graph_query/splitter 测试（db 直读 + progress_cb） |
| `tests/test_code_rag_routes.py` | redis_mgr mock → sqlite_store mock；sweep/分页测试 |

---

### Task 1: `read_call_edges` — db 直读全图 callers/callees

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py`（在 `read_file_hashes` 后，约 73 行后插入新函数）
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- Produces: `read_call_edges(graph_repo_path: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]` — 返回 `(callers_map, callees_map)`，每张 map 的 key 是 node `id`（str），value 是 `[{"name","kind","file_path","start_line","end_line"}]` 列表。`file_path` **保留 `src/` 前缀**（调用方按需剥，与 `read_file_hashes` 一致）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_helpers.py` 顶部 import 区追加（若已 import `sqlite3`/`tmp_path` 则复用）：

```python
def _build_codegraph_db_with_edges(db_path: Path, nodes: list[dict], edges: list[dict]) -> None:
    """手建一个 codegraph schema 的 db（不调 CLI），供 read_call_edges 单测。"""
    import sqlite3
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
        start_column INTEGER, end_column INTEGER, docstring TEXT, signature TEXT,
        visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER,
        is_abstract INTEGER, decorators TEXT, type_parameters TEXT, return_type TEXT,
        updated_at INTEGER);
    CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
        metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
    CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, size INTEGER, updated_at INTEGER);
    """)
    for n in nodes:
        conn.execute("INSERT INTO nodes (id,kind,name,file_path,start_line,end_line) VALUES (?,?,?,?,?,?)",
                     (n["id"], n["kind"], n["name"], n["file_path"], n["start_line"], n["end_line"]))
    for e in edges:
        conn.execute("INSERT INTO edges (source,target,kind) VALUES (?,?,?)",
                     (e["source"], e["target"], e["kind"]))
    conn.commit()
    conn.close()


def test_read_call_edges_returns_callers_and_callees(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import read_call_edges

    db_path = tmp_path / "repo" / ".codegraph" / "codegraph.db"
    # alpha -> beta -> gamma; caller -> alpha
    nodes = [
        {"id": "f:alpha", "kind": "function", "name": "alpha", "file_path": "src/a.py", "start_line": 1, "end_line": 2},
        {"id": "f:beta", "kind": "function", "name": "beta", "file_path": "src/a.py", "start_line": 3, "end_line": 4},
        {"id": "f:gamma", "kind": "function", "name": "gamma", "file_path": "src/a.py", "start_line": 5, "end_line": 6},
        {"id": "f:caller", "kind": "function", "name": "caller", "file_path": "src/a.py", "start_line": 7, "end_line": 8},
    ]
    edges = [
        {"source": "f:alpha", "target": "f:beta", "kind": "calls"},
        {"source": "f:beta", "target": "f:gamma", "kind": "calls"},
        {"source": "f:caller", "target": "f:alpha", "kind": "calls"},
    ]
    _build_codegraph_db_with_edges(db_path, nodes, edges)

    callers, callees = read_call_edges(str(tmp_path / "repo"))
    assert [r["name"] for r in callees["f:alpha"]] == ["beta"]
    assert [r["name"] for r in callees["f:beta"]] == ["gamma"]
    assert [r["name"] for r in callees["f:caller"]] == ["alpha"]
    assert [r["name"] for r in callers["f:beta"]] == ["alpha"]
    assert [r["name"] for r in callers["f:alpha"]] == ["caller"]
    # 无调用的符号不出现在 callees map
    assert "f:gamma" not in callees
    assert [r["name"] for r in callers["f:gamma"]] == ["beta"]


def test_read_call_edges_empty_when_no_db(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import read_call_edges

    callers, callees = read_call_edges(str(tmp_path / "nope"))
    assert callers == {}
    assert callees == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_read_call_edges_returns_callers_and_callees tests/test_code_rag_helpers.py::test_read_call_edges_empty_when_no_db -v`
Expected: FAIL with `ImportError: cannot import name 'read_call_edges'`

- [ ] **Step 3: 实现 `read_call_edges`**

在 `graph_query.py` 的 `read_file_hashes` 函数之后（约 73 行 `return {...}` 后）插入：

```python
def read_call_edges(graph_repo_path: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """一次查全图 callers/callees,返回 (callers_map, callees_map)。

    比"逐符号 spawn codegraph CLI"快 ~5000x(5k 符号:114ms vs 10k 子进程)。
    key=node id(精确,避免同名跨文件误并);value=ref 行列表。
    file_path 保留 src/ 前缀(与 read_file_hashes 一致,调用方按需剥)。

    db 不存在或异常返回 ({}, {})(retrieval 容忍)。
    """
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
        return {}, {}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_read_call_edges_returns_callers_and_callees tests/test_code_rag_helpers.py::test_read_call_edges_empty_when_no_db -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py tests/test_code_rag_helpers.py
git commit -m "feat(code-rag): read_call_edges — db-direct callers/callees in 2 SQL queries"
```

---

### Task 2: 检索路径懒加载缓存（file-hash 失效）

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py`
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- Produces: `_get_cached_edges(graph_repo_path: str) -> tuple[dict, dict]` — 进程内缓存，按 `graph_repo_path` 做 key；首次或 file-hash 变化时调 `read_call_edges` 重建。
- Produces: `get_callers`/`get_callees` 改签名不变（仍 `(graph_repo_path, symbol) -> list[dict]`），内部从缓存 map 按 symbol name 查（注意：检索 CLI 路径原本按 name 匹配，缓存 map 按 id 索引，所以这里需要 name→id 反查；见 Step 3 实现说明）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_helpers.py` 追加：

```python
def test_cached_edges_rebuild_on_file_hash_change(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    db_path = tmp_path / "repo" / ".codegraph" / "codegraph.db"
    nodes = [
        {"id": "f:alpha", "kind": "function", "name": "alpha", "file_path": "src/a.py", "start_line": 1, "end_line": 2},
        {"id": "f:beta", "kind": "function", "name": "beta", "file_path": "src/a.py", "start_line": 3, "end_line": 4},
    ]
    edges = [{"source": "f:alpha", "target": "f:beta", "kind": "calls"}]
    _build_codegraph_db_with_edges(db_path, nodes, edges)
    # files 表需要至少一行供 hash 快照
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files (path, content_hash) VALUES ('src/a.py', 'hash_v1')")
    conn.commit()
    conn.close()

    # 清理模块级缓存(防其它测试污染)
    graph_query._edges_cache.clear()

    callers1, callees1 = graph_query._get_cached_edges(str(tmp_path / "repo"))
    assert callees1["f:alpha"][0]["name"] == "beta"

    # 改 db:加一条 calls 边 + 改 file hash
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO nodes (id,kind,name,file_path,start_line,end_line) VALUES ('f:gamma','function','gamma','src/a.py',5,6)")
    conn.execute("INSERT INTO edges (source,target,kind) VALUES ('f:beta','f:gamma','calls')")
    conn.execute("UPDATE files SET content_hash='hash_v2' WHERE path='src/a.py'")
    conn.commit()
    conn.close()

    callers2, callees2 = graph_query._get_cached_edges(str(tmp_path / "repo"))
    assert callees2["f:beta"][0]["name"] == "gamma"  # 重建后看到新边


def test_cached_edges_reuses_unchanged(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    db_path = tmp_path / "repo2" / ".codegraph" / "codegraph.db"
    _build_codegraph_db_with_edges(db_path,
        [{"id": "f:x", "kind": "function", "name": "x", "file_path": "src/a.py", "start_line": 1, "end_line": 2}],
        [])
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files (path, content_hash) VALUES ('src/a.py', 'h1')")
    conn.commit()
    conn.close()

    graph_query._edges_cache.clear()
    graph_query._get_cached_edges(str(tmp_path / "repo2"))
    call_count_before = graph_query._edges_cache[str(tmp_path / "repo2")][0]  # snapshot ref
    graph_query._get_cached_edges(str(tmp_path / "repo2"))  # 第二次,hash 没变
    # 仍是同一个 snapshot 对象(未重建)
    assert graph_query._edges_cache[str(tmp_path / "repo2")][0] is call_count_before
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_cached_edges_rebuild_on_file_hash_change tests/test_code_rag_helpers.py::test_cached_edges_reuses_unchanged -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_get_cached_edges'` / `_edges_cache`

- [ ] **Step 3: 实现缓存**

在 `graph_query.py` 顶部 import 区之后（`_EMPTY_METADATA` 定义之前）加模块级缓存，并在 `read_call_edges` 之后加 `_get_cached_edges`：

```python
# 检索路径的 edges 缓存:key=graph_repo_path, value=(file_hashes_snapshot, callers_map, callees_map)。
# 失效靠 files.content_hash 快照比对(增量 sync 改 db → 下次检索自动重建)。
# split 阶段不进缓存(导入时只跑一次 read_call_edges 用完即丢,不污染)。
_edges_cache: dict[str, tuple[dict[str, str], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]] = {}


def _get_cached_edges(graph_repo_path: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """懒加载 callers/callees map,按 file hash 快照失效。

    返回 (callers_map, callees_map)。hash 不变 → 复用缓存;变了/首次 → 重建。
    """
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return {}, {}
    snapshot = read_file_hashes(graph_repo_path)
    cached = _edges_cache.get(graph_repo_path)
    if cached is not None and cached[0] == snapshot:
        return cached[1], cached[2]
    callers, callees = read_call_edges(graph_repo_path)
    _edges_cache[graph_repo_path] = (snapshot, callers, callees)
    return callers, callees
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_cached_edges_rebuild_on_file_hash_change tests/test_code_rag_helpers.py::test_cached_edges_reuses_unchanged -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py tests/test_code_rag_helpers.py
git commit -m "feat(code-rag): lazy cached callers/callees with file-hash invalidation"
```

---

### Task 3: `get_callers`/`get_callees`/`lookup_related_symbols`/`lookup_symbol_metadata` 改 db 读

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py`（`_callers_list`、`_callees_list`、`lookup_symbol_metadata_strict`、`lookup_related_symbols_strict`、`get_callers`、`get_callees`、`_query_symbol_node`）
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- `get_callers(graph_repo_path, symbol) -> list[dict]` / `get_callees(graph_repo_path, symbol) -> list[dict]`：签名不变，内部走缓存 map + name 查找。
- `lookup_symbol_metadata`/`lookup_related_symbols`：签名不变，BFS 在内存 map 上走。
- 删除 `_callers_list`/`_callees_list` 的 CLI 调用（splitter 不再调用它们——Task 4 会改 splitter 用 `read_call_edges`）。

- [ ] **Step 1: 写失败测试（回归保险）**

在 `tests/test_code_rag_helpers.py` 追加（用现有 `_build_codegraph_repo` fixture，真实 CLI，skip if not installed）：

```python
def test_get_callers_callees_db_direct_matches_cli(tmp_path: Path) -> None:
    """db 直读后 get_callers/get_callees 与旧 CLI 结果一致。"""
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_callers, get_callees
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    repo = _build_codegraph_repo(
        tmp_path,
        {"auth.py": "def login():\n    return hash_password()\ndef register():\n    return login()\n"},
    )
    graph_query._edges_cache.clear()  # 防污染

    callers = get_callers(str(repo), "login")
    callees = get_callees(str(repo), "login")
    assert "register" in [c["name"] for c in callers]
    assert "hash_password" in [c["name"] for c in callees]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_get_callers_callees_db_direct_matches_cli -v`
Expected: FAIL（旧 `get_callers` 走 CLI，可能因 `callers` 字段名/结构不符，或仍 pass——若 pass 说明 CLI 与 db 结果一致，保留作回归）。若 SKIP（无 CLI）也接受，进入 Step 3。

- [ ] **Step 3: 改写 `get_callers`/`get_callees` 为 db 读**

在 `graph_query.py` 把现有 `get_callers` 函数体替换为：

```python
def _symbol_id_by_name(
    callers: dict[str, list[dict[str, Any]]],
    callees: dict[str, list[dict[str, Any]]],
    symbol: str,
) -> str | None:
    """在缓存 map 的 key(=node id) 里按 name 找符号 id。

    callers/callees 的 key 是 node id,value 是 ref 列表;ref 里也有 name。
    起点符号本身可能只在 callers 的 value 里出现(被调用)或 callees 的 value 里(调别人)。
    先扫 callers/callees 的 key→value 里所有 name,找第一个匹配的符号 id。
    """
    for sym_id, refs in callers.items():
        # sym_id 对应的符号 name 不可知(只有 ref 行有 name),但 sym_id 本身的 name 在 nodes 表。
        # 这里退而求其次:扫 value ref 找 name==symbol 的 source(但 source id 在 callers value 里)。
        pass
    # 上面思路不通——callers map 的 key 是"被调用者 id",value 是 caller ref。
    # 要找 symbol 的 node id,需查 nodes 表。改用直接查 db。
    return None
```

实际上 `_symbol_id_by_name` 不可靠（map 里只有 id，没存起点 name）。改为**新增一个轻量 db 查询**：按 name 查 node id。在 `graph_query.py` 加：

```python
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
```

把 `get_callers` 替换为：

```python
def get_callers(graph_repo_path: str, symbol: str) -> list[dict[str, Any]]:
    """返回 caller 节点列表(db 直读,走缓存 map)。"""
    callers, _ = _get_cached_edges(graph_repo_path)
    sym_id = _lookup_symbol_id_by_name(graph_repo_path, symbol)
    if sym_id is None:
        return []
    return list(callers.get(sym_id, []))
```

把 `get_callees` 替换为：

```python
def get_callees(graph_repo_path: str, symbol: str) -> list[dict[str, Any]]:
    """返回 callee 节点列表(db 直读,走缓存 map)。"""
    _, callees = _get_cached_edges(graph_repo_path)
    sym_id = _lookup_symbol_id_by_name(graph_repo_path, symbol)
    if sym_id is None:
        return []
    return list(callees.get(sym_id, []))
```

删除刚写的占位 `_symbol_id_by_name` 函数（Step 3 第一段那个 pass 的，已被 `_lookup_symbol_id_by_name` 取代）。

- [ ] **Step 4: 改写 `lookup_symbol_metadata_strict`**

把 `lookup_symbol_metadata_strict` 中调用 `_callers_list`/`_callees_list` 的部分替换。找到现有函数体（约 286-317 行），替换 `callers = _callers_list(...)` / `callees = _callees_list(...)` 两行为：

```python
    callers = [str(r["name"]) for r in get_callers(graph_repo_path, symbol_name)]
    callees = [str(r["name"]) for r in get_callees(graph_repo_path, symbol_name)]
```

（`lookup_symbol_metadata_strict` 已有 `node = _query_symbol_node(...)`，但 callers/callees 不依赖 node——直接用上面 db 读。imports 仍走 `_get_imports_for_file` 不变。）

- [ ] **Step 5: 改写 `lookup_related_symbols_strict` 的 BFS**

把 `lookup_related_symbols_strict` 函数体（约 357-427 行）里的 `get_callers(graph_repo_path, sym)` / `get_callees(graph_repo_path, sym)` 调用保留——它们现在走缓存 map，BFS 多跳全在内存。但第一跳用的 `get_callers(graph_repo_path, symbol_name)` 也走缓存了，无需改逻辑。**唯一要改的是**：`_add_from_refs` 里读 `ref.get("file_path")`——db 直读的 ref 行已经有 `file_path`（带 `src/` 前缀），`_strip_builder_prefix` 仍适用，逻辑不变。

确认 `lookup_related_symbols_strict` 无需改函数体（它已调 `get_callers`/`get_callees`），只需删除已无引用的 `_callers_list`/`_callees_list` 函数定义（约 333-354 行）。

- [ ] **Step 6: 运行全部 graph_query 测试**

Run: `python3 -m pytest tests/test_code_rag_helpers.py -v -k "read_call_edges or cached_edges or get_callers_callees or lookup_symbol_metadata or lookup_related"`
Expected: PASS（含真实 CLI 的回归测试，无 CLI 则 skip）

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py tests/test_code_rag_helpers.py
git commit -m "refactor(code-rag): callers/callees retrieval via cached db map, drop per-symbol CLI"
```

---

### Task 4: `build_symbol_chunks` 用 `read_call_edges` + `progress_cb`

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/splitter.py`（`build_symbol_chunks`，约 421-522 行）
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- `build_symbol_chunks(source_dir, graph_repo_path, ignore_patterns, *, only_files=None, progress_cb=None) -> list[dict]`
- `progress_cb` 签名：`progress_cb(done: int, total: int, current_file: str) -> None`（同步，splitter 在 executor 线程内调用）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_helpers.py` 追加：

```python
def test_build_symbol_chunks_progress_callback(tmp_path: Path) -> None:
    """build_symbol_chunks 循环前知道 total, 每 200 符号回写 progress_cb。"""
    from aigateway_core.pipelines.understanding.code_rag.splitter import build_symbol_chunks

    repo = _build_codegraph_repo(
        tmp_path,
        {"mod.py": "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(5))},
    )
    calls: list[tuple[int, int, str]] = []

    def cb(done: int, total: int, current_file: str) -> None:
        calls.append((done, total, current_file))

    chunks = build_symbol_chunks(str(repo / "src") if False else str(repo),
                                  str(repo), [], progress_cb=cb)
    assert len(chunks) == 5
    assert calls, "progress_cb should be called at least once"
    # 收尾调用 done==total
    assert calls[-1][0] == calls[-1][1] == 5
    # total 在首次调用时已是 5(循环前已知)
    assert calls[0][1] == 5
```

注：`build_symbol_chunks` 的 `source_dir` 应指向 repo 根（`_resolve_source_file` 会 join `source_dir` + 剥掉 `src/` 的 rel_path；graph_builder symlink 源码成 `work_dir/src`，db 存 `src/...`，剥后 rel 是 `mod.py`，join `repo` → `repo/mod.py` 不存在）。**因此测试需把 source 源码放在 `repo/src/`**。修正 fixture 用法：`_build_codegraph_repo` 把文件写到 `repo/src/`（fixture 内部已 symlink）。所以 `source_dir=str(repo)` 时 `_resolve_source_file("src/mod.py", repo)` → 剥成 `mod.py` → `repo/mod.py`——不存在。

**修正**：传 `source_dir=str(repo / "src")`（fixture 把源码实际落在 `repo/src/`，db 存 `src/mod.py`，剥 `src/` → `mod.py`，join `repo/src` → `repo/src/mod.py` ✓）。把测试里 `str(repo / "src") if False else str(repo)` 改成 `str(repo / "src")`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_build_symbol_chunks_progress_callback -v`
Expected: FAIL with `TypeError: build_symbol_chunks() got an unexpected keyword argument 'progress_cb'`

- [ ] **Step 3: 改 `build_symbol_chunks`**

在 `splitter.py` 的 `build_symbol_chunks` 签名加 `progress_cb`，并在循环前调 `read_call_edges` 建一次 map、循环内 O(1) 查 map、每 200 符号回调。把现有函数（约 421-522 行）替换为：

```python
def build_symbol_chunks(
    source_dir: str,
    graph_repo_path: str,
    ignore_patterns: list[str],
    *,
    only_files: list[str] | None = None,
    progress_cb: Any = None,
) -> list[dict[str, Any]]:
    """从 codegraph db 读符号节点,用行号切源码 + 构造结构描述嵌入文本。

    callers/callees 走 read_call_edges(2 条 SQL,全图一次),不再逐符号 spawn CLI。
    progress_cb(done, total, current_file) 每 200 符号回写一次(splitting 阶段进度)。
    """
    from aigateway_core.pipelines.understanding.code_rag.graph_query import (
        _get_imports_for_file,
        read_call_edges,
    )

    logger = logging.getLogger(__name__)
    source_root = Path(source_dir).resolve()
    nodes = _read_symbol_nodes(graph_repo_path, only_files=only_files)

    # 一次性建全图 callers/callees map(替代 ~10k 次 CLI 子进程)
    callers_map, callees_map = read_call_edges(graph_repo_path)
    total = len(nodes)

    chunks: list[dict[str, Any]] = []
    per_file_index: dict[str, int] = {}

    for i, node in enumerate(nodes):
        rel_in_db = str(node.get("file_path") or "")
        if any(pat and pat in rel_in_db for pat in (ignore_patterns or [])):
            continue
        abs_path, rel_path = _resolve_source_file(rel_in_db, str(source_root))
        name = str(node.get("name") or "")
        kind = str(node.get("kind") or "function")
        start_line = int(node.get("start_line") or 1)
        end_line = int(node.get("end_line") or start_line)

        chunk_text = _slice_source_lines(abs_path, start_line, end_line)
        if not chunk_text:
            logger.warning(
                "code_rag: 无法切出源码 %s L%d-%d,跳过该符号",
                rel_path, start_line, end_line,
            )
            continue

        # O(1) 查 map(不再 spawn CLI)。node id 精确匹配。
        sym_id = str(node.get("id") or "")
        callers = [str(r["name"]) for r in callers_map.get(sym_id, [])]
        callees = [str(r["name"]) for r in callees_map.get(sym_id, [])]
        try:
            imports = _get_imports_for_file(graph_repo_path, rel_path)
        except Exception:
            imports = []

        signature = node.get("signature")
        docstring = node.get("docstring")
        language = str(node.get("language") or "text")

        embed_text = _build_embed_text(
            kind, name, rel_path, signature, docstring, callers, callees, chunk_text,
        )

        idx = per_file_index.get(rel_path, 0)
        per_file_index[rel_path] = idx + 1

        function_name = name if kind in ("function", "method") else None
        class_name = name if kind == "class" else None

        chunks.append(
            {
                "embed_text": embed_text,
                "chunk_text": chunk_text,
                "file_path": rel_path,
                "filename": Path(rel_path).name,
                "language": language,
                "chunk_index": idx,
                "start_line": start_line,
                "end_line": end_line,
                "function_name": function_name,
                "class_name": class_name,
                "callers": callers,
                "callees": callees,
                "imports": imports,
                "signature": signature or "",
                "docstring": docstring or "",
            }
        )

        if progress_cb is not None and (i % 200 == 0 or i == total - 1):
            progress_cb(done=i + 1, total=total, current_file=rel_path)

    # 收尾(空 chunks 也回写一次 total,让进度条到 100%)
    if progress_cb is not None and total == 0:
        progress_cb(done=0, total=0, current_file="")

    return chunks
```

注意：`_read_symbol_nodes` 的 SELECT 现在需要带上 `id` 列（splitter 要用 node id 查 map）。检查 `_read_symbol_nodes`（约 302-343 行）的 SELECT 语句，把 `id` 加进去：

找到 `_read_symbol_nodes` 里两处 `SELECT kind, name, file_path, start_line, end_line, signature, docstring, language FROM nodes`，改成 `SELECT id, kind, name, file_path, start_line, end_line, signature, docstring, language FROM nodes`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_build_symbol_chunks_progress_callback tests/test_code_rag_helpers.py::test_code_chunk_payload_includes_required_fields -v`
Expected: PASS

- [ ] **Step 5: 运行 splitter 相关回归**

Run: `python3 -m pytest tests/test_code_rag_helpers.py -v -k "build_symbol_chunks or code_chunk_payload or lookup_symbol"`
Expected: PASS（含真实 CLI 的回归测试）

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/splitter.py tests/test_code_rag_helpers.py
git commit -m "feat(code-rag): build_symbol_chunks uses read_call_edges + progress callback"
```

---

### Task 5: SQLite `code_rag_tasks` 表 + Store 方法

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/sqlite_store.py`（`SCHEMA_SQL` 约 110 行后加表；`SQLiteStore` 类加方法）
- Test: `tests/test_code_rag_routes.py`

**Interfaces:**
- `upsert_code_rag_task(task: dict) -> None`（含 task_id/document_id/status/done/total/current_file/source_type/source_label/embedding_model/graph_repo_path/error/created_at/updated_at；按 task_id upsert）
- `read_code_rag_task(task_id: str) -> dict | None`
- `list_code_rag_tasks(limit: int = 50, offset: int = 0) -> list[dict]`（按 created_at DESC）
- `fail_non_terminal_tasks(error: str) -> int`（返回标记数量，把 status NOT IN (completed,failed,cancelled) 的全标 failed）

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_routes.py` 顶部加 fixture（若已有 sqlite 临时 db fixture 则复用）。追加：

```python
def test_code_rag_task_upsert_read_list(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    import time
    now = int(time.time())
    store.upsert_code_rag_task({
        "task_id": "t1", "document_id": "code_abc", "status": "splitting",
        "done": 3, "total": 10, "current_file": "a.py", "source_type": "git",
        "source_label": "https://github.com/x/y.git", "embedding_model": "Qwen",
        "graph_repo_path": "/data/code_graphs/code_abc", "error": "",
        "created_at": now, "updated_at": now,
    })
    row = store.read_code_rag_task("t1")
    assert row is not None
    assert row["status"] == "splitting"
    assert row["done"] == 3
    assert row["source_label"] == "https://github.com/x/y.git"

    # upsert 更新
    store.upsert_code_rag_task({"task_id": "t1", "document_id": "code_abc",
                                 "status": "completed", "done": 10, "total": 10,
                                 "current_file": "", "source_type": "git",
                                 "source_label": "https://github.com/x/y.git",
                                 "embedding_model": "Qwen", "graph_repo_path": "/data/code_graphs/code_abc",
                                 "error": "", "created_at": now, "updated_at": now})
    assert store.read_code_rag_task("t1")["status"] == "completed"

    # list DESC
    store.upsert_code_rag_task({"task_id": "t2", "document_id": "code_def", "status": "pending",
                                 "done": 0, "total": 0, "current_file": "", "source_type": "folder",
                                 "source_label": "folder", "embedding_model": "Qwen", "graph_repo_path": "",
                                 "error": "", "created_at": now + 10, "updated_at": now + 10})
    rows = store.list_code_rag_tasks(limit=50, offset=0)
    assert [r["task_id"] for r in rows] == ["t2", "t1"]


def test_fail_non_terminal_tasks(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    import time

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    base = {"document_id": "x", "done": 0, "total": 0, "current_file": "", "source_type": "git",
            "source_label": "", "embedding_model": "", "graph_repo_path": "", "error": "",
            "created_at": now, "updated_at": now}
    store.upsert_code_rag_task({**base, "task_id": "a", "status": "splitting"})
    store.upsert_code_rag_task({**base, "task_id": "b", "status": "embedding"})
    store.upsert_code_rag_task({**base, "task_id": "c", "status": "completed"})
    store.upsert_code_rag_task({**base, "task_id": "d", "status": "failed"})

    n = store.fail_non_terminal_tasks("worker restarted")
    assert n == 2
    assert store.read_code_rag_task("a")["status"] == "failed"
    assert store.read_code_rag_task("a")["error"] == "worker restarted"
    assert store.read_code_rag_task("b")["status"] == "failed"
    assert store.read_code_rag_task("c")["status"] == "completed"  # 不动
    assert store.read_code_rag_task("d")["status"] == "failed"  # 已是终态

    # 幂等:再跑一次,0 个非终态
    assert store.fail_non_terminal_tasks("worker restarted") == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_code_rag_task_upsert_read_list tests/test_code_rag_routes.py::test_fail_non_terminal_tasks -v`
Expected: FAIL with `AttributeError: 'SQLiteStore' object has no attribute 'upsert_code_rag_task'`

- [ ] **Step 3: 加表 schema**

在 `sqlite_store.py` 的 `SCHEMA_SQL` 末尾（`idx_ledger_model` 那个 `CREATE INDEX` 之后，字符串闭合 `"""` 之前）追加：

```sql

CREATE TABLE IF NOT EXISTS code_rag_tasks (
    task_id         TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    status          TEXT NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    current_file    TEXT,
    source_type     TEXT,
    source_label    TEXT,
    embedding_model TEXT,
    graph_repo_path TEXT,
    error           TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_rag_tasks_created ON code_rag_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_code_rag_tasks_document ON code_rag_tasks(document_id);
```

- [ ] **Step 4: 加 Store 方法**

在 `SQLiteStore` 类里（`prune_ledger` 方法附近，或类末尾）加：

```python
    # ── code_rag_tasks ────────────────────────────────────────────

    def upsert_code_rag_task(self, task: dict) -> None:
        cols = ("task_id", "document_id", "status", "done", "total", "current_file",
                "source_type", "source_label", "embedding_model", "graph_repo_path",
                "error", "created_at", "updated_at")
        vals = tuple(task.get(c) for c in cols)
        self.conn.execute(
            f"""INSERT OR REPLACE INTO code_rag_tasks
                ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})""",
            vals,
        )
        self.conn.commit()

    def read_code_rag_task(self, task_id: str) -> Optional[dict]:
        row = self.conn.fetchone(
            "SELECT * FROM code_rag_tasks WHERE task_id=?", (task_id,)
        )
        return dict(row) if row else None

    def list_code_rag_tasks(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.fetchall(
            "SELECT * FROM code_rag_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
        return [dict(r) for r in rows]

    def fail_non_terminal_tasks(self, error: str) -> int:
        now = _now_unix()
        cur = self.conn.execute(
            """UPDATE code_rag_tasks
               SET status='failed', error=?, updated_at=?
               WHERE status NOT IN ('completed', 'failed', 'cancelled')""",
            (error, now),
        )
        self.conn.commit()
        return cur.rowcount
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_code_rag_task_upsert_read_list tests/test_code_rag_routes.py::test_fail_non_terminal_tasks -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/sqlite_store.py tests/test_code_rag_routes.py
git commit -m "feat(code-rag): SQLite code_rag_tasks table + store methods"
```

---

### Task 6: `code_rag_routes` 任务状态 Redis → SQLite

**Files:**
- Modify: `aigateway-api/src/aigateway_api/code_rag_routes.py`（`_write_task_state` 101 行、`_read_task_state` 132 行、`_delete_task_key` 125 行、`_shape_task_response` 146 行、`list_code_tasks` 974 行、`get_code_task` 1014 行、`cancel_code_task` 910 行、`import_code_repository` 861/870 行、`_run_code_import_task` 588 行、`_run_code_import_task_with_deadline` 543 行）
- Test: `tests/test_code_rag_routes.py`

**Interfaces:**
- `_write_task_state`/`_read_task_state`/`list_code_tasks`/`get_code_task`/`cancel_code_task` 改从 `app.state.sqlite_store` 读写。
- `_delete_task_key` 移除（改为 no-op 或删调用）。
- `_mark`（task 内闭包）改走 SQLite。
- `import_code_repository` 的 `source_label` 去 `git://` 前缀。

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_routes.py` 追加（mock 一个 fake app state 带 sqlite_store）：

```python
def test_list_code_tasks_reads_sqlite(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    from aigateway_api.code_rag_routes import list_code_tasks
    from fastapi import Request
    import types, time

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    store.upsert_code_rag_task({"task_id": "t1", "document_id": "d1", "status": "completed",
        "done": 5, "total": 5, "current_file": "", "source_type": "git",
        "source_label": "https://github.com/x/y.git", "embedding_model": "", "graph_repo_path": "",
        "error": "", "created_at": now, "updated_at": now})

    app = types.SimpleNamespace(state=types.SimpleNamespace(sqlite_store=store))
    req = types.SimpleNamespace(app=app)
    out = asyncio.get_event_loop().run_until_complete(list_code_tasks(req, {})) if False else None
    import asyncio as _aio
    out = _aio.new_event_loop().run_until_complete(list_code_tasks(req, {}))
    assert len(out) == 1
    assert out[0]["task_id"] == "t1"
    assert out[0]["status"] == "completed"
    assert out[0]["source_label"] == "https://github.com/x/y.git"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_list_code_tasks_reads_sqlite -v`
Expected: FAIL（`list_code_tasks` 仍读 Redis，`redis_mgr is None` 返回 `[]`）

- [ ] **Step 3: 改 `_write_task_state` / `_read_task_state`**

在 `code_rag_routes.py` 把 `_write_task_state`（101-122 行）替换为：

```python
async def _write_task_state(
    app_state: Any,
    task_id: str,
    fields: Dict[str, Any],
) -> None:
    """把任务状态字段 upsert 到 SQLite code_rag_tasks 表。

    fields 是部分字段(增量更新);读出现有行合并后写回(upsert 需要全列)。
    """
    sqlite_store = getattr(app_state, "sqlite_store", None)
    if sqlite_store is None:
        return
    existing = sqlite_store.read_code_rag_task(task_id) or {}
    existing.update({k: v for k, v in fields.items() if v is not None})
    existing["task_id"] = task_id
    existing.setdefault("document_id", "")
    existing.setdefault("status", "pending")
    existing.setdefault("done", 0)
    existing.setdefault("total", 0)
    existing.setdefault("current_file", "")
    existing.setdefault("source_type", "")
    existing.setdefault("source_label", "")
    existing.setdefault("embedding_model", "")
    existing.setdefault("graph_repo_path", "")
    existing.setdefault("error", "")
    existing.setdefault("created_at", int(time.time()))
    existing["updated_at"] = int(time.time())
    sqlite_store.upsert_code_rag_task(existing)
```

把 `_read_task_state`（132-143 行）替换为：

```python
async def _read_task_state(app_state: Any, task_id: str) -> Optional[Dict[str, Any]]:
    sqlite_store = getattr(app_state, "sqlite_store", None)
    if sqlite_store is None:
        return None
    return sqlite_store.read_code_rag_task(task_id)
```

把 `_delete_task_key`（125-129 行）替换为 no-op（保留函数避免改所有调用点，但不再删）：

```python
async def _delete_task_key(app_state: Any, task_id: str) -> None:
    """终态留表当历史(不再删 SQLite 行)。保留签名避免改调用点。"""
    return None
```

- [ ] **Step 4: 全局替换调用点 `redis_mgr` → `app_state`**

所有 `await _write_task_state(redis_mgr, task_id, {...})` / `await _read_task_state(redis_mgr, ...)` / `await _delete_task_key(redis_mgr, task_id)` 调用点的第一个参数从 `redis_mgr` 改成 `app_state`。

受影响位置（grep 确认）：
- `_run_code_import_task` 内 `_mark` 闭包（588 行）：`async def _mark(**fields): await _write_task_state(redis_mgr, ...)` → `await _write_task_state(app_state, ...)`。同时 `_run_code_import_task` 的 `redis_mgr = getattr(app_state, "redis_manager", None)`（585 行）保留（repo 元数据仍用 Redis）。
- `_run_code_import_task_with_deadline`（543、550 行）。
- `import_code_repository`（870 行写 pending）。
- `cancel_code_task`（929、941 行读 + Lua 改）。
- `list_code_tasks`（984-1011 行）、`get_code_task`（1020-1021 行）。

`cancel_code_task` 的 Lua 比对替换为：

```python
    sqlite_store = getattr(app_state, "sqlite_store", None)
    if sqlite_store is None:
        raise HTTPException(status_code=503, detail="task store unavailable")
    existing = await _read_task_state(app_state, task_id)
    if not existing:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found",
            "message": f"Code import task '{task_id}' not found"}})
    current_status = existing.get("status") or "pending"
    if current_status in ("completed", "failed", "cancelled"):
        return {"task_id": task_id, "status": current_status}
    now = int(time.time())
    sqlite_store.conn.execute(
        "UPDATE code_rag_tasks SET status='cancelled', updated_at=? WHERE task_id=? "
        "AND status NOT IN ('completed','failed','cancelled')",
        (now, task_id),
    )
    sqlite_store.conn.commit()
```

- [ ] **Step 5: 改 `list_code_tasks` / `get_code_task`**

把 `list_code_tasks`（974-1011 行）替换为：

```python
@router.get("/rag/code/tasks")
async def list_code_tasks(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    """列出最近的任务(按 created_at 倒序,默认 50 条)。"""
    sqlite_store = getattr(request.app.state, "sqlite_store", None)
    if sqlite_store is None:
        return []
    rows = sqlite_store.list_code_rag_tasks(limit=limit, offset=offset)
    return [_shape_task_response(r["task_id"], r) for r in rows]
```

把 `get_code_task`（1014-1027 行）替换为：

```python
@router.get("/rag/code/tasks/{task_id}")
async def get_code_task(
    task_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    state = await _read_task_state(request.app.state, task_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return _shape_task_response(task_id, state)
```

- [ ] **Step 6: 改 `import_code_repository` 的 `source_label` 和 task 创建**

把 `import_code_repository` 里写 pending 的 `_write_task_state`（870 行）调用第一个参数改 `app_state`。把 `source_label = f"git://{body.git_url}"`（861 行）改为：

```python
            source_label = body.git_url or ""
```

- [ ] **Step 7: 改 `_run_code_import_task` 的 `_mark` 与 deadline**

确认 `_mark` 闭包（588 行）已改为 `await _write_task_state(app_state, task_id, fields)`。确认 `_run_code_import_task_with_deadline`（543、550 行）的 `_write_task_state` 第一参数改 `app_state`。

- [ ] **Step 8: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_routes.py -v -k "list_code_tasks or task_upsert or fail_non_terminal"`
Expected: PASS

- [ ] **Step 9: 运行全 code_rag routes 测试**

Run: `python3 -m pytest tests/test_code_rag_routes.py -v`
Expected: PASS（修掉所有 `redis_mgr` mock → `sqlite_store` 的不匹配；若有其它测试用 `redis_mgr` mock 任务状态，更新为 `sqlite_store` mock）

- [ ] **Step 10: Commit**

```bash
git add aigateway-api/src/aigateway_api/code_rag_routes.py tests/test_code_rag_routes.py
git commit -m "refactor(code-rag): task state Redis->SQLite (history retained, pagination)"
```

---

### Task 7: 启动孤儿任务清理 + 临时目录清理

**Files:**
- Modify: `aigateway-api/src/aigateway_api/code_rag_routes.py`（新增 `sweep_orphaned_tasks`）
- Modify: `aigateway-api/src/aigateway_api/main.py`（lifespan 644 行后调用）
- Test: `tests/test_code_rag_routes.py`

**Interfaces:**
- `sweep_orphaned_tasks(app_state: Any) -> int`：调 `sqlite_store.fail_non_terminal_tasks("worker restarted during import")` + 清理孤儿临时目录，返回标记数。

- [ ] **Step 1: 写失败测试**

```python
def test_sweep_orphaned_tasks(tmp_path: Path) -> None:
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    from aigateway_api.code_rag_routes import sweep_orphaned_tasks
    import types, time

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    base = {"document_id": "x", "done": 0, "total": 0, "current_file": "", "source_type": "git",
            "source_label": "", "embedding_model": "", "graph_repo_path": "", "error": "",
            "created_at": now, "updated_at": now}
    store.upsert_code_rag_task({**base, "task_id": "a", "status": "splitting"})
    store.upsert_code_rag_task({**base, "task_id": "b", "status": "completed"})

    app_state = types.SimpleNamespace(sqlite_store=store)
    n = sweep_orphaned_tasks(app_state)
    assert n == 1
    assert store.read_code_rag_task("a")["status"] == "failed"
    assert "worker restarted" in store.read_code_rag_task("a")["error"]
    assert store.read_code_rag_task("b")["status"] == "completed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_sweep_orphaned_tasks -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_orphaned_tasks'`

- [ ] **Step 3: 实现 `sweep_orphaned_tasks`**

在 `code_rag_routes.py`（`_delete_task_key` 附近或文件末尾 helper 区）加：

```python
def sweep_orphaned_tasks(app_state: Any) -> int:
    """启动时把非终态任务标 failed(worker 重启打断)。

    返回标记数量。顺带清理孤儿临时目录(/tmp/code_rag_folder_* 与
    /data/code_graphs/*/.tmp/)。SQLite store 不存在时返回 0。
    """
    sqlite_store = getattr(app_state, "sqlite_store", None)
    marked = 0
    if sqlite_store is not None:
        marked = sqlite_store.fail_non_terminal_tasks("worker restarted during import")
        if marked:
            logger.info("code rag 启动清理: %d 个非终态任务标记为 failed", marked)

    # 清理孤儿临时目录(旧失败残留)
    import glob
    for pattern in ("/tmp/code_rag_folder_*",):
        for d in glob.glob(pattern):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    graph_db_dir = "/data/code_graphs"
    for d in glob.glob(f"{graph_db_dir}/*/.tmp"):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    return marked
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_sweep_orphaned_tasks -v`
Expected: PASS

- [ ] **Step 5: lifespan 调用**

在 `main.py` 的 `app.state.sqlite_store = sqlite_store`（639 行）及 `prune_ledger`（644-648 行）之后插入：

```python
    # 启动时清理 code rag 孤儿任务(网关重启打断的非终态导入任务标记 failed)
    try:
        from aigateway_api.code_rag_routes import sweep_orphaned_tasks
        swept = sweep_orphaned_tasks(app.state)
        if swept:
            logger.info("code rag 孤儿任务清理完成: %d 个", swept)
    except Exception as exc:
        logger.warning("启动清理 code rag 孤儿任务失败: %s", exc)
```

- [ ] **Step 6: Commit**

```bash
git add aigateway-api/src/aigateway_api/code_rag_routes.py aigateway-api/src/aigateway_api/main.py tests/test_code_rag_routes.py
git commit -m "feat(code-rag): startup orphan task sweep + tmp dir cleanup"
```

---

### Task 8: splitting 进度回写接线

**Files:**
- Modify: `aigateway-api/src/aigateway_api/code_rag_routes.py`（`_run_code_import_task` 604-608 行，传 `progress_cb`）

- [ ] **Step 1: 写失败测试**

```python
def test_splitting_progress_written_to_sqlite(tmp_path: Path) -> None:
    """build_symbol_chunks 的 progress_cb 经 run_coroutine_threadsafe 回写 SQLite。"""
    # 这是集成性测试:mock build_symbol_chunks 触发 progress_cb,验证 SQLite done/total 更新。
    # 用 monkeypatch 替换 splitter.build_symbol_chunks 为触发回调的桩。
    from aigateway_core.shared.auth.sqlite_store import SQLiteStore
    import aigateway_api.code_rag_routes as routes
    import types, time, asyncio as _aio

    store = SQLiteStore(db_path=str(tmp_path / "t.db"))
    now = int(time.time())
    store.upsert_code_rag_task({"task_id": "t1", "document_id": "d1", "status": "splitting",
        "done": 0, "total": 0, "current_file": "", "source_type": "git", "source_label": "",
        "embedding_model": "", "graph_repo_path": "", "error": "", "created_at": now, "updated_at": now})
    app_state = types.SimpleNamespace(sqlite_store=store, redis_manager=None,
        qdrant_manager=None, config_manager=None)

    captured = {}
    def fake_build(source_dir, graph_repo_path, ignore_patterns, *, only_files=None, progress_cb=None):
        if progress_cb:
            progress_cb(done=5, total=10, current_file="a.py")
            progress_cb(done=10, total=10, current_file="b.py")
        return []

    import aigateway_core.pipelines.understanding.code_rag.splitter as splitter_mod
    orig = splitter_mod.build_symbol_chunks
    splitter_mod.build_symbol_chunks = fake_build
    try:
        loop = _aio.new_event_loop()
        loop.run_until_complete(routes._run_code_import_task(
            app_state=app_state, task_id="t1", document_id="d1", source_dir="/x",
            source_type="git", source_label="", embedding_model="Qwen",
            ignore_patterns=[], graph_repo_path="/x", workspace_path=None,
            cleanup_dirs=[]))
    finally:
        splitter_mod.build_symbol_chunks = orig

    row = store.read_code_rag_task("t1")
    # 空 chunks → completed + done=0(分片阶段至少回写过 total=10)
    assert row["total"] == 10
    assert row["status"] == "completed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_splitting_progress_written_to_sqlite -v`
Expected: FAIL（`build_symbol_chunks` 调用未传 `progress_cb`，或 `total` 没回写）

- [ ] **Step 3: 接线 `progress_cb`**

在 `code_rag_routes.py` 的 `_run_code_import_task`，把 splitting 段（604-608 行）：

```python
        await _mark(status="splitting")
        chunks: List[Dict[str, Any]] = await loop.run_in_executor(
            None, lambda: build_symbol_chunks(source_dir, graph_repo_path, ignore_patterns)
        )
        await _mark(total=len(chunks))
```

替换为：

```python
        await _mark(status="splitting", done=0, total=0)
        # splitter 跑在 executor 线程,不能直接 await _mark;用 run_coroutine_threadsafe 调度回主 loop
        def _split_progress(done: int, total: int, current_file: str) -> None:
            asyncio.run_coroutine_threadsafe(
                _mark(done=done, total=total, current_file=current_file),
                loop,
            ).result(timeout=10)
        chunks: List[Dict[str, Any]] = await loop.run_in_executor(
            None, lambda: build_symbol_chunks(
                source_dir, graph_repo_path, ignore_patterns, progress_cb=_split_progress
            )
        )
        await _mark(total=len(chunks), done=0)
```

注意 `loop = asyncio.get_running_loop()` 在 598 行已定义（building_graph 段），在 splitting 段复用。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_splitting_progress_written_to_sqlite -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/code_rag_routes.py tests/test_code_rag_routes.py
git commit -m "feat(code-rag): split-stage progress callback wired to SQLite task state"
```

---

### Task 9: `start_new_session` + `killpg` deadline 杀子进程

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py`（`_run_codegraph_json` 86 行、`_run_codegraph_raw` 130 行）
- Modify: `aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_builder.py`（`_run_codegraph` 32 行）
- Test: `tests/test_code_rag_helpers.py`

- [ ] **Step 1: 写失败测试**

```python
def test_run_codegraph_raw_uses_new_session(monkeypatch, tmp_path: Path) -> None:
    """subprocess 用 start_new_session=True,超时后 killpg 整个进程组。"""
    from aigateway_core.pipelines.understanding.code_rag import graph_query
    import subprocess

    captured = {}
    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""
    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    # build_code_graph 的 _run_codegraph 也走同一路径
    from aigateway_core.pipelines.understanding.code_rag import graph_builder
    out = graph_query._run_codegraph_raw(["node", "x", "-p", str(tmp_path)])
    assert captured["kwargs"].get("start_new_session") is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_run_codegraph_raw_uses_new_session -v`
Expected: FAIL（kwargs 里没有 `start_new_session`）

- [ ] **Step 3: 加 `start_new_session` + killpg**

在 `graph_query.py` 的 `_run_codegraph_json`（96 行）和 `_run_codegraph_raw`（139 行）的 `subprocess.run(...)` 调用里加 `start_new_session=True` 参数。

并在超时异常处理里加 killpg。修改 `_run_codegraph_json` 的 `subprocess.run` 块（96-103 行）：

```python
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
        start_new_session=True,
    )
```

同样改 `_run_codegraph_raw`（139-147 行）加 `start_new_session=True`。

`graph_builder.py` 的 `_run_codegraph`（41-48 行）`subprocess.run(...)` 加 `start_new_session=True`。

（超时 killpg 由 `subprocess.run` 的 `timeout` 内部处理——`subprocess.run` 超时会 kill 子进程；`start_new_session=True` 确保子进程 spawn 的孙进程也在同一进程组，被一起回收。无需手写 `os.killpg`，`subprocess.run` 超时已 kill 主子进程，进程组在父退出后由 OS 收割。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_run_codegraph_raw_uses_new_session -v`
Expected: PASS

- [ ] **Step 5: 运行真实 CLI 回归**

Run: `python3 -m pytest tests/test_code_rag_helpers.py -v -k "lookup_symbol_metadata or build_symbol_chunks"`
Expected: PASS（真实 codegraph CLI 调用仍工作）

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_builder.py tests/test_code_rag_helpers.py
git commit -m "fix(code-rag): start_new_session so deadline/timeout reaps child process groups"
```

---

### Task 10: config 白名单加 `code_rag`

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/config.py`（219 行 `allowed_top_level`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_code_rag_helpers.py`（`test_runtime_config_has_top_level_code_rag_block` 附近）确认或追加：

```python
def test_code_rag_is_allowed_top_level() -> None:
    """config.yaml 的 code_rag 块不应触发未识别字段警告。"""
    from aigateway_core.shared.config import ConfigManager
    import logging, io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("aigateway_core.shared.config")
    logger.addHandler(handler)
    # 构造一个含 code_rag 的最小 config 文件
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("server:\n  host: 0.0.0.0\n  port: 8000\ncode_rag:\n  enabled: true\n  graph_db_dir: /tmp/x\n")
        path = f.name
    try:
        ConfigManager(config_path=path)
    finally:
        os.remove(path)
        logger.removeHandler(handler)
    assert "code_rag" not in stream.getvalue()  # 不在未识别警告里
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_code_rag_is_allowed_top_level -v`
Expected: FAIL（`code_rag` 触发警告，出现在日志里）

- [ ] **Step 3: 加白名单**

在 `config.py` 的 `allowed_top_level`（219-224 行）集合里加 `"code_rag"`：

```python
        allowed_top_level = {
            "server", "auth", "plugins", "providers", "embedding",
            "observability", "hot_reload", "debug_mode", "debug", "infrastructure",
            "cache", "media_optimization", "circuit_breaker", "rate_limiter",
            "streaming", "generation_optimization", "code_rag",
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_code_rag_is_allowed_top_level tests/test_code_rag_helpers.py::test_runtime_config_has_top_level_code_rag_block -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/config.py tests/test_code_rag_helpers.py
git commit -m "fix(config): allow code_rag as recognized top-level field"
```

---

### Task 11: 全量回归 + 文档更新

**Files:**
- Modify: `CLAUDE.md`（Code RAG gotcha 段）
- Verify: 全部测试

- [ ] **Step 1: 全量单元测试**

Run: `python3 -m pytest tests/ -q`
Expected: 全 PASS（e2e/ui 自动 skip）

- [ ] **Step 2: 端到端冒烟(可选,需 gateway 起来)**

Run: `python3 -m pytest tests/test_code_rag_helpers.py tests/test_code_rag_routes.py -v`
Expected: PASS

- [ ] **Step 3: 更新 CLAUDE.md**

在 CLAUDE.md 的 Code RAG gotcha 段，把描述 callers/callees 走 CLI 的部分更新为 db 直读 + SQLite 任务表。找到类似 "Code RAG is now a separate subsystem" 段，更新：

```
- **Code RAG** — callers/callees 改 db 直读(`read_call_edges`,2 条 SQL + 进程内 file-hash 失效缓存),不再逐符号 spawn codegraph CLI(5k 符号 114ms vs 旧 ~10k 子进程)。CLI 只在 `codegraph init`(build)出现一次。任务状态存 SQLite `code_rag_tasks` 表(终态留表当历史),不再用 Redis 任务 key。启动时 `sweep_orphaned_tasks` 把非终态任务标 failed。splitting 阶段每 200 符号回写进度。
```

先 `wc -l CLAUDE.md` 确认行数,若超 300 先 prune。

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update Code RAG gotcha for db-direct callers/callees + SQLite task store"
```

---

## Self-Review

**1. Spec coverage:**
- callers/callees db 直读(splitter + graph_query) → Task 1, 3, 4 ✓
- 懒加载缓存 file-hash 失效 → Task 2 ✓
- 启动孤儿清理 → Task 7 ✓
- SQLite 任务表替换 Redis → Task 5, 6 ✓
- splitting 进度回写 → Task 4, 8 ✓
- source_label 去前缀 → Task 6 Step 6 ✓
- config 白名单 → Task 10 ✓
- deadline 杀子进程 → Task 9 ✓
- 临时目录清理 → Task 7 ✓
- get_node 保留 CLI → Task 3 未动 get_node ✓
- sync 不进任务表 → Task 6 未动 sync ✓
- 测试 → 每 Task 含测试 ✓

**2. Placeholder scan:** 无 TBD/TODO。Task 3 Step 3 有个"占位 `_symbol_id_by_name`"——但 Step 3 明确指示删除它并改用 `_lookup_symbol_id_by_name`，是过程说明非占位。✓

**3. Type consistency:**
- `read_call_edges` 返回 `tuple[dict, dict]` → Task 2 `_get_cached_edges` 返回同 → Task 3 `get_callers`/`get_callees` 用 `sym_id` 查 map ✓
- `progress_cb(done, total, current_file)` → Task 4 定义、Task 8 接线一致 ✓
- `upsert_code_rag_task(dict)` / `read_code_rag_task(task_id)->dict|None` / `list_code_rag_tasks(limit,offset)` / `fail_non_terminal_tasks(error)->int` → Task 5 定义、Task 6/7 调用一致 ✓
- `sweep_orphaned_tasks(app_state)->int` → Task 7 定义、main.py 调用一致 ✓

全部一致。
