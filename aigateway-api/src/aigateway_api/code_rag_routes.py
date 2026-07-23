"""Code RAG 管理路由(Task 3).

四个端点:
- POST   /admin/rag/code/import                创建异步导入任务(folder/server_path/git/zip)
- GET    /admin/rag/code/tasks/{task_id}        轮询任务进度
- GET    /admin/rag/code/repositories           列出已导入代码仓库
- DELETE /admin/rag/code/repositories/{doc_id}  级联删除 Qdrant 向量 + Redis 元数据 + 图谱库

设计原则(与 spec 一致):
- 严格于导入,容忍于检索:任务生命周期任一硬失败(嵌入模型加载失败/
  维度探测失败/图谱构建失败/Qdrant 写失败并回滚)整体记为 failed。
- 每个嵌入模型独立 Qdrant collection: rag_code_<slug>,避免维度冲突。
- server_path 走 allowed_server_paths 白名单 + realpath 展开,防止符号
  链接逃逸。
- Git 只支持 https:// 公共仓库 + 浅克隆(--depth=1)。
- ZIP 解压做 zip-slip 检查 + 单文件大小上限 + 总体积上限。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from .auth_middleware import authenticate_admin

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------------
# Redis key naming (repo metadata list — still Redis-backed)
# ----------------------------------------------------------------------

_REPO_LIST_KEY = "aigateway:rag:code:documents"
_CANCELLED = "cancelled"


# ----------------------------------------------------------------------
# Request / response schemas
# ----------------------------------------------------------------------


class _CodeImportJsonBody(BaseModel):
    """JSON 请求体(server_path / git 两种源)."""

    source_type: str = Field(..., description="folder | server_path | git | zip")
    server_path: Optional[str] = None
    git_url: Optional[str] = None
    git_branch: Optional[str] = None
    embedding_model: str = Field(default="Qwen/Qwen3-Embedding-0.6B")


# ----------------------------------------------------------------------
# Task state helpers (SQLite-backed code_rag_tasks table)
# ----------------------------------------------------------------------


async def _write_task_state(
    app_state: Any,
    task_id: str,
    fields: Dict[str, Any],
) -> None:
    """把任务状态字段 upsert 到 SQLite code_rag_tasks 表。

    fields 是部分字段(增量更新);读出现有行合并后写回(upsert 需要全列)。
    终态留表当历史(不再像 Redis 那样 _delete_task_key 删 key)。
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


async def _delete_task_key(app_state: Any, task_id: str) -> None:
    """终态留表当历史(不再删 SQLite 行)。保留签名避免改调用点。"""
    return None


async def _read_task_state(app_state: Any, task_id: str) -> Optional[Dict[str, Any]]:
    sqlite_store = getattr(app_state, "sqlite_store", None)
    if sqlite_store is None:
        return None
    return sqlite_store.read_code_rag_task(task_id)


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


def _shape_task_response(task_id: str, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 store 里的字段规范化为 API 输出结构.

    created_at 缺失时返回 0 (非 null)，保证 list 端按时间倒序时老任务排末尾.
    """
    if not state:
        return {
            "task_id": task_id,
            "status": "pending",
            "done": 0,
            "total": 0,
            "current_file": None,
            "error": None,
            "source_label": None,
            "source_type": None,
            "created_at": 0,
        }
    try:
        done = int(state.get("done") or 0)
    except (TypeError, ValueError):
        done = 0
    try:
        total = int(state.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        created_at = int(state.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    return {
        "task_id": task_id,
        "status": state.get("status") or "pending",
        "done": done,
        "total": total,
        "current_file": state.get("current_file") or None,
        "error": state.get("error") or None,
        "source_label": state.get("source_label") or None,
        "source_type": state.get("source_type") or None,
        "created_at": created_at,
    }


# ----------------------------------------------------------------------
# Repository metadata (Redis list)
# ----------------------------------------------------------------------


def _spawn_import_task(app_state: Any, coro: Any, *, task_id: str) -> Any:
    """把后台导入任务塞进 app.state.code_rag_active_tasks,避免 GC 掉。

    直接 asyncio.create_task 会让 loop 只持弱引用,长任务在低负载下可能被回收,
    弹出 "Task was destroyed but it is pending" 且中断真实导入。我们:
    - 把 Task 放进一个 dict, strong-ref 直到完成
    - 挂 done_callback: 移出 dict + 打印未捕获异常(finally 里 _mark(failed)
      走 Redis 是主路径,这里只做兜底日志)
    - dict 的键是 task_id,方便取消时查找
    """
    active: Dict[str, asyncio.Task] = getattr(app_state, "code_rag_active_tasks", None)  # type: ignore[assignment]
    if active is None:
        active = {}
        app_state.code_rag_active_tasks = active

    task = asyncio.create_task(coro, name=f"code_rag_import:{task_id}")
    active[task_id] = task

    def _done(t: Any) -> None:
        active.pop(task_id, None)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            logger.warning("code rag import task %s cancelled", task_id)
            return
        except Exception:
            return
        if exc is not None:
            logger.error(
                "code rag import task %s raised uncaught exception: %r", task_id, exc
            )

    task.add_done_callback(_done)
    return task


async def _append_repository(redis_mgr: Any, repo_meta: Dict[str, Any]) -> None:
    if redis_mgr is None or redis_mgr.redis is None:
        return
    await redis_mgr.redis.lpush(_REPO_LIST_KEY, json.dumps(repo_meta, ensure_ascii=False))


async def _list_repositories(redis_mgr: Any) -> List[Dict[str, Any]]:
    if redis_mgr is None or redis_mgr.redis is None:
        return []
    raw = await redis_mgr.redis.lrange(_REPO_LIST_KEY, 0, -1)
    out: List[Dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except Exception:
            continue
    return out


async def _remove_repository(redis_mgr: Any, document_id: str) -> None:
    """删除仓库元数据(线性扫 + lrem,与已有 rag_documents 一致)."""
    if redis_mgr is None or redis_mgr.redis is None:
        return
    raw_list = await redis_mgr.redis.lrange(_REPO_LIST_KEY, 0, -1)
    for item in raw_list:
        try:
            doc = json.loads(item.decode() if isinstance(item, bytes) else item)
        except Exception:
            continue
        if doc.get("document_id") == document_id:
            await redis_mgr.redis.lrem(_REPO_LIST_KEY, 1, item)


# ----------------------------------------------------------------------
# Source materialization
# ----------------------------------------------------------------------


_ZIP_SIZE_HARD_CAP_MB = 200


def _is_server_path_allowed(candidate: str, allowed_roots: List[str]) -> bool:
    """走 realpath 展开的白名单检查,拒绝符号链接逃逸."""
    # lazy import 避免 admin 路由启动阶段拉整个 code_rag 包
    from aigateway_core.pipelines.understanding.code_rag.splitter import is_path_allowed

    return is_path_allowed(candidate, allowed_roots)


def _validate_server_path(candidate: str, allowed_roots: List[str]) -> Path:
    if not candidate:
        raise HTTPException(status_code=400, detail="server_path 不能为空")
    if not _is_server_path_allowed(candidate, allowed_roots):
        raise HTTPException(status_code=403, detail="server_path 不在允许列表中")
    resolved = Path(candidate).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail="server_path 不存在或不是目录")
    return resolved


def _validate_git_url(git_url: str) -> str:
    if not git_url or not git_url.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="git_url 必须是 https:// 开头的公共仓库地址(phase 1 不支持 ssh/私仓)",
        )
    return git_url


def _materialize_git_repo(
    git_url: str,
    git_branch: Optional[str],
    *,
    timeout: float = 300.0,
    dest_dir: Optional[str] = None,
) -> str:
    """浅克隆到目录,返回该目录路径。

    dest_dir 给定时克隆到该持久目录(git 源持久化:为增量 sync 保留源码,
    不进 cleanup_dirs);不给时落到临时目录(fallback,导入后清理)。

    通过 GIT_HTTP_LOW_SPEED_LIMIT/TIME + 外部 wall-clock 阻断长时间挂住的克隆,
    避免 "silently stuck" 出现在 gateway 侧。
    """
    from git import Repo  # lazy: gitpython 只在生产镜像里装

    if dest_dir:
        target_dir = dest_dir
        Path(target_dir).mkdir(parents=True, exist_ok=True)
    else:
        target_dir = tempfile.mkdtemp(prefix="code_rag_git_")
    try:
        clone_kwargs: Dict[str, Any] = {
            "depth": 1,
            "env": {
                # 60s 内低于 1KB/s 判为超慢连接,直接中止
                "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
                "GIT_HTTP_LOW_SPEED_TIME": "60",
                # 关闭密码提示,避免私仓卡在 stdin 等待
                "GIT_TERMINAL_PROMPT": "0",
            },
        }
        if git_branch:
            clone_kwargs["branch"] = git_branch

        def _clone() -> None:
            Repo.clone_from(git_url, target_dir, **clone_kwargs)

        import threading

        error: List[BaseException] = []

        def _target() -> None:
            try:
                _clone()
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            # 后台线程留在原地慢慢死;主流程按超时报错并清理
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError(f"git clone timed out after {timeout}s: {git_url}")
        if error:
            raise error[0]
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return target_dir


def _materialize_zip_upload(zip_bytes: bytes, max_total_mb: int) -> str:
    """安全解压 ZIP(zip-slip + 总大小上限 + 解压控制).

    不再依赖 zf.extractall() 自带 sanitizer（Python <3.12 不可靠）。
    改为逐成员校验 + 显式写入，同时限制每个文件的解压体积。
    """
    import io

    tmp_dir = tempfile.mkdtemp(prefix="code_rag_zip_")
    max_file_mb = 50  # 单文件最大解压体积
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            root = Path(tmp_dir).resolve()
            total_bytes = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                dest = (root / info.filename).resolve()
                if root not in dest.parents and dest != root:
                    raise HTTPException(
                        status_code=400,
                        detail=f"ZIP zip-slip 拦截: {info.filename}",
                    )
                total_bytes += info.file_size
                if total_bytes > max_total_mb * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="ZIP 解压后总体积超过上限",
                    )
                # 单文件解压上限（防 zip bomb 膨胀）
                if info.file_size > max_file_mb * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail=f"ZIP 单文件过大: {info.filename}",
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        dst.write(chunk)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"无效 ZIP: {exc}")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return tmp_dir


def _bytes_io(data: bytes) -> Any:
    import io

    return io.BytesIO(data)


def _ensure_workspace_symlink(graph_repo_path: str, real_source_dir: str) -> None:
    """把 real_source_dir 软链接成 {graph_repo_path}/src(managed 源持久化)。

    codegraph db 里 file_path 都带 src/ 前缀(graph_builder 把源码 symlink 成
    work_dir/src 后索引),所以 {graph_repo_path}/src 必须指向真实源码目录,
    codegraph sync(增量重扫)与 codegraph node(源码块)才能按 files.path 解析。

    已存在(非 symlink 或指向别处)时先删后建。失败抛 RuntimeError(导入失败)。
    """
    repo_dir = Path(graph_repo_path)
    repo_dir.mkdir(parents=True, exist_ok=True)
    link = repo_dir / "src"
    try:
        if link.is_symlink() or link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == Path(real_source_dir).resolve():
                return  # 已正确指向,幂等
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link, ignore_errors=True)
            else:
                link.unlink()
        link.symlink_to(Path(real_source_dir).resolve())
    except OSError as exc:
        raise RuntimeError(
            f"无法建立 workspace symlink {link} -> {real_source_dir}: {exc}"
        ) from exc


def _sanitize_relative_path(raw: str) -> str:
    """归一化前端传来的 webkitRelativePath,防止绝对路径 / .. 逃逸."""
    if not raw:
        return ""
    normalized = raw.replace("\\", "/").lstrip("/")
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _folder_source_label(files: List[UploadFile], relative_paths: List[str]) -> str:
    """优先使用上传目录根名作为 folder source_label,避免退化成首个文件名."""
    for raw in relative_paths:
        rel = _sanitize_relative_path(raw)
        if not rel:
            continue
        root = rel.split("/", 1)[0]
        if root:
            return f"folder://{root}"
    for upload in files:
        if upload.filename:
            return f"folder://{upload.filename}"
    return "folder://upload"


async def _materialize_folder_upload(
    files: List[UploadFile],
    relative_paths: List[str],
    max_file_size_mb: int,
    max_total_size_mb: int,
    max_file_count: int,
) -> str:
    """把 drag/drop 或 folder-picker 上传的多个文件落到临时目录,保留相对路径."""
    if not files:
        raise HTTPException(status_code=400, detail="folder 源必须至少上传一个文件")
    if len(files) > max_file_count:
        raise HTTPException(status_code=400, detail=f"文件数超过上限 {max_file_count}")

    tmp_dir = tempfile.mkdtemp(prefix="code_rag_folder_")
    total = 0
    try:
        root = Path(tmp_dir).resolve()
        for idx, upload in enumerate(files):
            rel = _sanitize_relative_path(
                relative_paths[idx] if idx < len(relative_paths) else upload.filename or f"file_{idx}"
            )
            if not rel:
                continue
            dest = (root / rel).resolve()
            if root not in dest.parents and dest != root:
                raise HTTPException(status_code=400, detail=f"上传路径逃逸: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = await upload.read()
            if len(data) > max_file_size_mb * 1024 * 1024:
                raise HTTPException(status_code=400, detail=f"文件超过单文件大小上限: {rel}")
            total += len(data)
            if total > max_total_size_mb * 1024 * 1024:
                raise HTTPException(status_code=400, detail="上传总体积超过上限")
            dest.write_bytes(data)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return tmp_dir


# ----------------------------------------------------------------------
# Background import task
# ----------------------------------------------------------------------


async def _run_code_import_task_with_deadline(
    *,
    deadline_seconds: int,
    app_state: Any,
    task_id: str,
    **kwargs: Any,
) -> None:
    """给整个导入任务加 wall-clock 上限,避免 "silently stuck"。"""
    try:
        await asyncio.wait_for(
            _run_code_import_task(
                app_state=app_state, task_id=task_id, **kwargs
            ),
            timeout=max(60, int(deadline_seconds)),
        )
    except asyncio.TimeoutError:
        logger.error(
            "code rag import task %s exceeded wall-clock deadline (%ss)",
            task_id,
            deadline_seconds,
        )
        await _write_task_state(
            app_state,
            task_id,
            {
                "status": "failed",
                "error": f"import task exceeded {deadline_seconds}s deadline",
            },
        )
    except asyncio.CancelledError:
        # 用户取消透传,由内层 _run_code_import_task 处理状态标记
        logger.info("code rag import task %s cancelled (deadline wrapper)", task_id)
        raise


async def _run_code_import_task(
    app_state: Any,
    task_id: str,
    document_id: str,
    source_dir: str,
    source_type: str,
    source_label: str,
    embedding_model: str,
    ignore_patterns: List[str],
    graph_repo_path: str,
    workspace_path: Optional[str],
    cleanup_dirs: List[str],
    git_branch: Optional[str] = None,
) -> None:
    """异步导入任务主体(见 spec: Async task flow).

    重构后:用 build_symbol_chunks(codegraph 行号切源码 + 结构描述)替代
    split_code_directory;embedding 嵌 embed_text(结构描述)而非 chunk_text(源码);
    build_code_graph 接收 graph_repo_path(目录),保留整个 .codegraph/ 目录。
    """
    from aigateway_core.pipelines.understanding.code_rag.embedding_router import (
        encode_texts,
        probe_embedding_dimension,
        resolve_collection_name,
    )
    from aigateway_core.pipelines.understanding.code_rag.graph_builder import build_code_graph
    from aigateway_core.pipelines.understanding.code_rag.splitter import build_symbol_chunks

    redis_mgr = getattr(app_state, "redis_manager", None)
    qdrant_mgr = getattr(app_state, "qdrant_manager", None)

    async def _mark(**fields: Any) -> None:
        await _write_task_state(app_state, task_id, fields)

    collection_name = resolve_collection_name(embedding_model)
    written_points = False

    try:
        # 1) build code graph (strict — 失败即整体失败)。
        # 必须先建图谱,build_symbol_chunks 要从 db 读符号节点。
        await _mark(status="building_graph", current_file=None, error=None)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: build_code_graph(source_dir, graph_repo_path)
        )

        # 2) split:用 codegraph 行号切源码 + 构造结构描述嵌入文本
        await _mark(status="splitting", done=0, total=0)
        # splitter 跑在 executor 线程,不能直接 await _mark;用 run_coroutine_threadsafe
        # 把 _mark(done,total,current_file) 调度回主 event loop 写 SQLite。
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
        # splitter 的 progress_cb 已按真实符号数回写 total;这里只在有 chunks 时
        # 把 total 校正为实际切出的 chunk 数(部分节点切源码失败时 chunks < 节点数)。
        # 空 chunks 不覆盖 total(保留回调写的值,让前端进度条停在真实节点数)。
        if chunks:
            await _mark(total=len(chunks), done=0)
        if not chunks:
            await _mark(status="completed", done=0)
            await _delete_task_key(app_state, task_id)
            return

        # 3) probe embedding dim + upsert collection
        await _mark(status="embedding")
        vector_dim = await loop.run_in_executor(
            None, lambda: probe_embedding_dimension(embedding_model)
        )
        if qdrant_mgr is None or qdrant_mgr._http is None:
            raise RuntimeError("Qdrant not connected")
        await qdrant_mgr.upsert_collection(
            name=collection_name, size=int(vector_dim), distance="COSINE"
        )

        # 4) encode + upsert（分批,避免 1000+ chunks 一次性 encode 卡死）
        # 嵌入的是 embed_text(结构描述:符号名/签名/callers/callees/docstring),
        # 而非 chunk_text(源码)。源码存 payload chunk_text,检索命中直接返回,
        # 源码改动不重算向量(增量友好)。
        batch_size = 64
        payloads: List[Dict[str, Any]] = []
        processed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch_chunks = chunks[batch_start : batch_start + batch_size]
            batch_texts = [c["embed_text"] for c in batch_chunks]
            batch_vectors = await loop.run_in_executor(
                None, lambda: encode_texts(embedding_model, batch_texts)
            )

            batch_points: List[Dict[str, Any]] = []
            for offset, chunk in enumerate(batch_chunks):
                global_idx = batch_start + offset
                chunk_type = (
                    "function" if chunk.get("function_name")
                    else "class" if chunk.get("class_name")
                    else "module"
                )
                payload = {
                    "document_id": document_id,
                    "filename": chunk.get("filename", ""),
                    "file_path": chunk.get("file_path", ""),
                    "language": chunk.get("language", ""),
                    "chunk_index": int(chunk.get("chunk_index", global_idx)),
                    "chunk_text": chunk.get("chunk_text", ""),
                    "chunk_type": chunk_type,
                    "function_name": chunk.get("function_name"),
                    "class_name": chunk.get("class_name"),
                    "start_line": int(chunk.get("start_line", 1)),
                    "end_line": int(chunk.get("end_line", 1)),
                    "callers": chunk.get("callers", []),
                    "callees": chunk.get("callees", []),
                    "imports": chunk.get("imports", []),
                    "signature": chunk.get("signature", ""),
                    "docstring": chunk.get("docstring", ""),
                    "embedding_model": embedding_model,
                }
                payloads.append(payload)
                batch_points.append(
                    {
                        "id": str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{document_id}:{payload['file_path']}:{payload['chunk_index']}",
                        )),
                        "vector": batch_vectors[offset],
                        "payload": payload,
                    }
                )

            # 每一批单独 upsert；任一批失败都按 document_id 回滚整份导入。
            try:
                resp = await qdrant_mgr._http.put(
                    f"/collections/{collection_name}/points",
                    json={"points": batch_points},
                )
                resp.raise_for_status()
                written_points = True
            except Exception:
                await qdrant_mgr.delete_by_filter(
                    collection_name,
                    {"must": [{"key": "document_id", "match": {"value": document_id}}]},
                )
                raise

            processed += len(batch_chunks)
            current_file = batch_chunks[-1].get("file_path") if batch_chunks else None
            await _mark(done=processed, current_file=current_file)

        await _mark(done=len(chunks))

        # 6) aggregate repository metadata
        repo_meta = {
            "document_id": document_id,
            "source_type": source_type,
            "source_label": source_label,
            "workspace_path": workspace_path,
            "graph_repo_path": graph_repo_path,
            "git_branch": git_branch,
            "embedding_model": embedding_model,
            "file_count": len({p["file_path"] for p in payloads if p["file_path"]}),
            "language_summary": sorted({p["language"] for p in payloads if p["language"]}),
            "function_count": sum(1 for p in payloads if p["chunk_type"] == "function"),
            "class_count": sum(1 for p in payloads if p["chunk_type"] == "class"),
            "chunk_count": len(payloads),
            "import_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        await _append_repository(redis_mgr, repo_meta)

        await _mark(status="completed", error=None)
        await _delete_task_key(app_state, task_id)

    except asyncio.CancelledError:
        # 用户主动取消,不记为 failed
        logger.info("code rag import task %s cancelled by user", task_id)
        await _mark(status=_CANCELLED, error=None)
        await _delete_task_key(app_state, task_id)
        raise  # 重新抛出让 done_callback 处理

    except Exception as exc:
        logger.exception("code rag import task %s failed: %s", task_id, exc)
        # Qdrant 点已写但流程中断 → 尝试清理
        if written_points and qdrant_mgr is not None and qdrant_mgr._http is not None:
            try:
                await qdrant_mgr.delete_by_filter(
                    collection_name,
                    {"must": [{"key": "document_id", "match": {"value": document_id}}]},
                )
            except Exception:
                logger.warning("回滚点位时二次异常,忽略")
        await _write_task_state(
            app_state,
            task_id,
            {"status": "failed", "error": str(exc)},
        )
        await _delete_task_key(app_state, task_id)
    finally:
        for cleanup in cleanup_dirs:
            shutil.rmtree(cleanup, ignore_errors=True)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def _load_code_rag_config(app_state: Any) -> Dict[str, Any]:
    config_manager = getattr(app_state, "config_manager", None)
    if config_manager is None:
        return {}
    cfg = config_manager.get("code_rag", {}) or {}
    return dict(cfg)


@router.post("/rag/code/import")
async def import_code_repository(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    """创建异步导入任务并立即返回 task_id.

    请求体两种形态:
    - JSON: {source_type: 'server_path'|'git', ...}
    - multipart/form-data: source_type=folder|zip + files/file + embedding_model
    """
    app_state = request.app.state
    code_cfg = _load_code_rag_config(app_state)
    if not code_cfg.get("enabled", True):
        raise HTTPException(status_code=403, detail="Code RAG 未启用")

    allowed_roots = list(code_cfg.get("allowed_server_paths") or [])
    ignore_patterns = list(code_cfg.get("ignore_patterns") or [])
    graph_db_dir = str(code_cfg.get("graph_db_dir") or "/data/code_graphs")
    max_file_size_mb = int(code_cfg.get("max_file_size_mb") or 5)
    max_total_size_mb = int(code_cfg.get("max_total_size_mb") or 200)
    max_file_count = int(code_cfg.get("max_file_count") or 5000)

    content_type = (request.headers.get("content-type") or "").lower()
    task_id = str(uuid.uuid4())
    document_id = f"code_{uuid.uuid4().hex[:12]}"
    cleanup_dirs: List[str] = []
    # graph_repo_path: 持久化目录 {graph_db_dir}/{document_id}/,存放 .codegraph/
    # 与 managed 源(git clone / server_path symlink)。folder/zip 不在此持久化源码。
    graph_repo_path = str(Path(graph_db_dir) / document_id)
    workspace_path: Optional[str] = None  # managed 源持久化路径(供 sync 判断)

    if "multipart/form-data" in content_type:
        form = await request.form()
        source_type = str(form.get("source_type") or "")
        embedding_model = str(form.get("embedding_model") or "Qwen/Qwen3-Embedding-0.6B")
        if source_type == "folder":
            uploads = form.getlist("files") if hasattr(form, "getlist") else form.getlist("files")  # type: ignore[attr-defined]
            relative_paths = [
                str(p) for p in (form.getlist("relative_paths") if hasattr(form, "getlist") else [])
            ]
            # FastAPI 0.139 / Starlette 1.3+ 里 fastapi.UploadFile 与 starlette 的
            # UploadFile 是两个不同的类, request.form() 返回 starlette 版本,
            # isinstance(u, fastapi.UploadFile) 会全部 False → folder 源恒报
            # "必须至少上传一个文件". 用 duck-typing (有 read 协程 + filename) 兼容两者.
            files: List[UploadFile] = [
                u for u in uploads
                if hasattr(u, "read") and hasattr(u, "filename")
            ]
            source_dir = await _materialize_folder_upload(
                files,
                relative_paths,
                max_file_size_mb,
                max_total_size_mb,
                max_file_count,
            )
            source_label = _folder_source_label(files, relative_paths)
        elif source_type == "zip":
            upload = form.get("file")
            # 同 folder 分支: 用 duck-typing 而非 isinstance(upload, UploadFile),
            # 否则 starlette/fastapi 双 UploadFile 类会让合法 ZIP 上传被判为缺字段.
            if not (hasattr(upload, "read") and hasattr(upload, "filename")):
                raise HTTPException(status_code=400, detail="ZIP 源需要 file 字段")
            data = await upload.read()
            if len(data) > max_total_size_mb * 1024 * 1024:
                raise HTTPException(status_code=400, detail="ZIP 体积超过上限")
            source_dir = _materialize_zip_upload(data, max_total_size_mb)
            source_label = f"zip://{upload.filename or 'upload.zip'}"
        else:
            raise HTTPException(status_code=400, detail=f"不支持的 multipart source_type: {source_type}")
        cleanup_dirs.append(source_dir)
    else:
        try:
            body = _CodeImportJsonBody(**(await request.json()))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"请求体无效: {exc}")
        source_type = body.source_type
        embedding_model = body.embedding_model
        if source_type == "server_path":
            resolved = _validate_server_path(body.server_path or "", allowed_roots)
            # managed 源:把原 server_path 目录 symlink 成 {graph_repo_path}/src,
            # 使 DB file_path(src/...前缀)可被 codegraph sync/node 解析。
            # source_dir 用 {graph_repo_path}/src（和 git 导入一致），
            # codegraph 通过 work_dir/src → {graph_repo_path}/src → resolved
            # 索引出 src/<files>；_resolve_source_file 剥掉 src/ 后再 join
            # 到 {graph_repo_path}/src，符号链接自动展开到 resolved。
            _ensure_workspace_symlink(graph_repo_path, str(resolved))
            source_dir = str(Path(graph_repo_path) / "src")
            source_label = f"server_path://{resolved}"
            workspace_path = source_dir
        elif source_type == "git":
            _validate_git_url(body.git_url or "")
            # managed 源:clone 到持久目录 {graph_repo_path}/src/(不进 cleanup_dirs),
            # 供增量 sync (git fetch + reset) 与 codegraph node(源码块)复用。
            git_src_dir = str(Path(graph_repo_path) / "src")
            source_dir = _materialize_git_repo(
                body.git_url or "", body.git_branch, dest_dir=git_src_dir
            )
            source_label = body.git_url or ""
            workspace_path = git_src_dir
        else:
            raise HTTPException(
                status_code=400,
                detail=f"JSON 请求只支持 server_path/git,收到 {source_type}",
            )

    await _write_task_state(
        app_state,
        task_id,
        {
            "status": "pending",
            "done": 0,
            "total": 0,
            "current_file": None,
            "error": None,
            "created_at": int(time.time()),
            "source_type": source_type,
            "source_label": source_label,
            "document_id": document_id,
            "embedding_model": embedding_model,
        },
    )

    _spawn_import_task(
        app_state,
        _run_code_import_task_with_deadline(
            deadline_seconds=int(code_cfg.get("import_timeout_seconds") or 3600),
            app_state=app_state,
            task_id=task_id,
            document_id=document_id,
            source_dir=source_dir,
            source_type=source_type,
            source_label=source_label,
            embedding_model=embedding_model,
            ignore_patterns=ignore_patterns,
            graph_repo_path=graph_repo_path,
            workspace_path=workspace_path,
            cleanup_dirs=cleanup_dirs,
            git_branch=body.git_branch if source_type == "git" else None,
        ),
        task_id=task_id,
    )

    return {"task_id": task_id, "status": "pending"}


@router.post("/rag/code/tasks/{task_id}/cancel")
async def cancel_code_task(
    task_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    """取消一个正在运行的导入任务。

    流程:
    1. 先读 SQLite 确认任务状态,不存在则 404,已是终态则直接返回(不覆盖)
    2. SQL 条件 UPDATE: 仅当当前非终态时才写 cancelled,防止与任务完成路径竞争
    3. 从 app.state.code_rag_active_tasks 中找到 asyncio.Task 并 cancel()
    4. asyncio 会在下一次 await 处抛出 CancelledError,
       _run_code_import_task 的 except 块捕获后走 finally 清理。
    """
    app_state = request.app.state
    sqlite_store = getattr(app_state, "sqlite_store", None)
    if sqlite_store is None:
        raise HTTPException(status_code=503, detail="task store unavailable")

    # 0) 检查当前状态,不存在则直接报 404,避免伪造 cancelled 任务
    existing = await _read_task_state(app_state, task_id)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Code import task '{task_id}' not found",
                }
            },
        )

    current_status = existing.get("status") or "pending"
    if current_status in ("completed", "failed", "cancelled"):
        logger.info("Code rag import task %s already terminal (status=%s), skipping cancel", task_id, current_status)
        return {"task_id": task_id, "status": current_status}

    # 1) 原子 CAS: 仅当当前非终态时才写 cancelled,防止与任务完成路径竞争
    now = int(time.time())
    cur = sqlite_store.conn.execute(
        "UPDATE code_rag_tasks SET status='cancelled', updated_at=? "
        "WHERE task_id=? AND status NOT IN ('completed','failed','cancelled')",
        (now, task_id),
    )
    sqlite_store.conn.commit()
    if cur.rowcount == 0:
        # 并发写入导致状态已变,重新读取真实状态
        updated = await _read_task_state(app_state, task_id)
        return {"task_id": task_id, "status": (updated or {}).get("status") or "cancelled"}

    # 2) 取消 asyncio.Task
    active = getattr(app_state, "code_rag_active_tasks", None)
    if active and task_id in active:
        active[task_id].cancel()
        logger.info("Cancelled code rag import task %s", task_id)
    else:
        # 任务已完成/从未创建,SQLite 标记已写,静默返回
        logger.info("Code rag import task %s not found in active tasks (already done?)", task_id)

    return {"task_id": task_id, "status": _CANCELLED}


@router.get("/rag/code/tasks")
async def list_code_tasks(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    """列出最近的任务(按 created_at 倒序,默认 50 条)。

    任务终态留表当历史,故 list 端能返回已完成/失败/取消的任务供前端恢复。
    """
    sqlite_store = getattr(request.app.state, "sqlite_store", None)
    if sqlite_store is None:
        return []
    rows = sqlite_store.list_code_rag_tasks(limit=limit, offset=offset)
    return [_shape_task_response(r["task_id"], r) for r in rows]


@router.get("/rag/code/tasks/{task_id}")
async def get_code_task(
    task_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    state = await _read_task_state(request.app.state, task_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found",
        )
    return _shape_task_response(task_id, state)


@router.get("/rag/code/repositories")
async def list_code_repositories(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    redis_mgr = getattr(request.app.state, "redis_manager", None)
    return await _list_repositories(redis_mgr)


@router.delete("/rag/code/repositories/{document_id}", status_code=204)
async def delete_code_repository(
    document_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> None:
    app_state = request.app.state
    code_cfg = _load_code_rag_config(app_state)
    graph_db_dir = str(code_cfg.get("graph_db_dir") or "/data/code_graphs")

    # 安全校验:document_id 必须是已注册的仓库。FastAPI path param 不含 '/',
    # 但 `..` 是合法单段——`Path(graph_db_dir) / ".."` 会解析成上级目录,
    # 而 shutil.rmtree 会递归删整个数据卷(Redis/Qdrant/code_graphs)。
    # 旧实现用 .unlink()(对目录抛 IsADirectoryError 无害);重构改 rmtree 后
    # 变成可利用,故此处必须先校验。
    redis_mgr = getattr(app_state, "redis_manager", None)
    repos = await _list_repositories(redis_mgr)
    if not any(r.get("document_id") == document_id for r in repos):
        raise HTTPException(status_code=404, detail=f"仓库 {document_id} 不存在")

    # defense-in-depth:解析后路径必须在 graph_db_dir 之内,挡住任何未来
    # 绕过注册校验的畸形 document_id(如 URL 编码 / 旁路注入)。
    graph_root = Path(graph_db_dir).resolve()
    graph_repo_dir = (graph_root / document_id).resolve()
    try:
        graph_repo_dir.relative_to(graph_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法 document_id")

    qdrant_mgr = getattr(app_state, "qdrant_manager", None)
    if qdrant_mgr is not None and qdrant_mgr._http is not None:
        try:
            resp = await qdrant_mgr._http.get("/collections/")
            resp.raise_for_status()
            collections = [
                c.get("name")
                for c in resp.json().get("result", {}).get("collections", []) or []
                if c.get("name") and c.get("name").startswith("rag_code_")
            ]
            for name in collections:
                try:
                    await qdrant_mgr.delete_by_filter(
                        name,
                        {"must": [{"key": "document_id", "match": {"value": document_id}}]},
                    )
                except Exception as exc:
                    logger.warning("删除 %s 上的 %s 点失败: %s", name, document_id, exc)
        except Exception as exc:
            logger.warning("枚举代码集合失败: %s", exc)

    # 删除整个 {graph_db_dir}/{document_id}/ 目录(重构后:含 .codegraph/ + src/
    # managed 源)。同时兜底删旧的 {document_id}.db 裸文件(存量数据,见 plan 风险项)。
    # graph_repo_dir 已在上方校验为 graph_db_dir 的合法子路径。
    if graph_repo_dir.exists():
        try:
            shutil.rmtree(graph_repo_dir, ignore_errors=True)
        except OSError as exc:
            logger.warning("删除图谱目录 %s 失败: %s", graph_repo_dir, exc)
    legacy_db = graph_root / f"{document_id}.db"
    if legacy_db.exists():
        try:
            legacy_db.unlink()
        except OSError as exc:
            logger.warning("删除旧图谱库 %s 失败: %s", legacy_db, exc)

    await _remove_repository(getattr(app_state, "redis_manager", None), document_id)


# ----------------------------------------------------------------------
# Repository sync (增量更新 — git/server_path managed 源)
# ----------------------------------------------------------------------


def _find_repo_meta(repos: List[Dict[str, Any]], document_id: str) -> Optional[Dict[str, Any]]:
    for repo in repos:
        if repo.get("document_id") == document_id:
            return repo
    return None


def _git_fetch_reset(
    workspace_path: str, *, git_branch: Optional[str] = None, timeout: float = 300.0
) -> None:
    """在持久化的 git 工作目录里 git fetch + reset --hard(不重新 clone)。

    供增量 sync 用:刷新源码到最新,再跑 codegraph sync 重索引变了的文件。

    git_branch:导入时用户指定的分支(持久化在 repo_meta)。给定时 reset 到
    `origin/<branch>`(与导入时的浅克隆分支一致);不给时退回 `origin/HEAD`
    (远端默认分支)。仅用 `origin/HEAD` 的坑:浅克隆 `--branch <name>` 后
    `origin/HEAD` 可能未设置或指向远端默认分支 → reset 失败或静默切到别的分支。
    """
    from git import Repo

    repo = Repo(workspace_path)
    # 拉取所有分支的远端更新,然后硬重置到指定分支的远端跟踪
    try:
        import threading

        error: List[BaseException] = []

        def _target() -> None:
            try:
                repo.remotes.origin.fetch()
                # 优先 reset 到 origin/<branch>(与导入分支对齐);
                # 无 branch 或 origin/<branch> 不存在时退回 origin/HEAD。
                ref = f"origin/{git_branch}" if git_branch else "origin/HEAD"
                try:
                    repo.git.reset("--hard", ref)
                except BaseException:  # noqa: BLE001
                    if not git_branch or ref == "origin/HEAD":
                        raise
                    # 指定分支的远端引用不存在(可能被删/改名)→ 退回默认分支
                    repo.git.reset("--hard", "origin/HEAD")
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise RuntimeError(f"git fetch+reset timed out after {timeout}s")
        if error:
            raise error[0]
    finally:
        repo.close()


@router.post("/rag/code/repositories/{document_id}/sync")
async def sync_code_repository(
    document_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    """增量同步代码仓库(git/server_path managed 源)。

    流程(plan 步骤 6):
    1. 校验 source_type ∈ {git, server_path};folder/zip 返回 400(快照源不支持 sync)。
    2. 刷新源码:git fetch+reset / server_path 重扫(源本就在场)。
    3. codegraph sync <graph_repo_path>(位置参数)增量重索引图谱。
    4. 对比 sync 前后 files.content_hash,找出变化文件。
    5. 对每个变化文件:Qdrant delete_by_filter 删旧 chunk → 重切符号 → 重算结构描述
       向量 → upsert 新 chunk。未变文件不动。
    6. 返回 {synced_files, refreshed_symbols}。
    """
    app_state = request.app.state
    code_cfg = _load_code_rag_config(app_state)
    graph_db_dir = str(code_cfg.get("graph_db_dir") or "/data/code_graphs")

    redis_mgr = getattr(app_state, "redis_manager", None)
    repos = await _list_repositories(redis_mgr)
    repo_meta = _find_repo_meta(repos, document_id)
    if repo_meta is None:
        raise HTTPException(status_code=404, detail=f"仓库 {document_id} 不存在")

    source_type = str(repo_meta.get("source_type") or "")
    if source_type not in ("git", "server_path"):
        raise HTTPException(
            status_code=400,
            detail="快照源(folder/zip)不支持 sync,请重新导入以更新",
        )

    graph_repo_path = str(repo_meta.get("graph_repo_path") or str(Path(graph_db_dir) / document_id))
    workspace_path = str(repo_meta.get("workspace_path") or "")
    git_branch = repo_meta.get("git_branch")  # 导入时持久化的分支(可空)
    embedding_model = str(repo_meta.get("embedding_model") or "Qwen/Qwen3-Embedding-0.6B")
    # 与导入路径一致地从配置取 ignore_patterns:sync 重切的符号也必须排除被忽略的文件,
    # 否则 codegraph sync 重索引的 node_modules/dist 等会回灌 Qdrant 污染检索结果。
    ignore_patterns = list(code_cfg.get("ignore_patterns") or [])

    if not Path(graph_repo_path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"图谱目录不存在: {graph_repo_path}(可能已删除或导入未完成)",
        )

    qdrant_mgr = getattr(app_state, "qdrant_manager", None)
    if qdrant_mgr is None or qdrant_mgr._http is None:
        raise HTTPException(status_code=503, detail="Qdrant not connected")

    # lazy import(避免 admin 路由启动阶段拉 code_rag 全包)
    from aigateway_core.pipelines.understanding.code_rag.embedding_router import (
        encode_texts,
        resolve_collection_name,
    )
    from aigateway_core.pipelines.understanding.code_rag.graph_query import (
        read_file_hashes,
        run_codegraph_sync,
    )
    from aigateway_core.pipelines.understanding.code_rag.splitter import build_symbol_chunks

    try:
        # 1) 刷新源码(managed 源才有)
        if source_type == "git":
            if not workspace_path or not Path(workspace_path).exists():
                raise HTTPException(
                    status_code=409,
                    detail="git 工作目录不存在(workspace_path 未持久化),无法增量 sync",
                )
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _git_fetch_reset(workspace_path, git_branch=git_branch)
            )
        # server_path: 源本就在场,workspace_path 是 src symlink → 原路径,无需操作

        # 2) sync 前快照 files.content_hash
        before = await asyncio.get_running_loop().run_in_executor(
            None, lambda: read_file_hashes(graph_repo_path)
        )

        # 3) codegraph sync 增量重索引
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: run_codegraph_sync(graph_repo_path)
        )

        # 4) sync 后快照,对比找出变化文件
        after = await asyncio.get_running_loop().run_in_executor(
            None, lambda: read_file_hashes(graph_repo_path)
        )
        changed_db_paths: List[str] = []
        for path, h in after.items():
            if before.get(path) != h:
                changed_db_paths.append(path)
        # 删除的文件:before 有 after 无
        deleted_db_paths = [p for p in before if p not in after]

        if not changed_db_paths and not deleted_db_paths:
            return {"document_id": document_id, "synced_files": 0, "refreshed_symbols": 0}

        collection_name = resolve_collection_name(embedding_model)
        # 5) 对每个变化文件:重切 → 重嵌 → (新 chunk 就绪后)删旧 chunk → upsert 新
        # 顺序很关键:先 build+encode,任一步抛异常则该文件整体跳过(旧 chunk 保留),
        # 不会出现「删了旧又没写进新」的静默数据丢失。仅当新 chunk 就绪后才删旧、
        # 再 upsert 新(delete 用 document_id+file_path 过滤,会清掉旧的同文件 chunk;
        # 新 chunk 紧随其后写入,upsert 成功即覆盖该文件的检索结果)。
        refreshed = 0
        for db_path in changed_db_paths:
            # db path 带 src/ 前缀,剥成相对源根(与 Qdrant payload file_path 对齐)
            rel = db_path
            if rel.startswith("src/"):
                rel = rel[len("src/"):]

            # 重切该文件的符号(source_dir = workspace_path 的真实源码)
            source_dir = workspace_path
            if source_type == "server_path":
                # workspace_path 是 src symlink → 原路径;取真实路径
                src_link = Path(graph_repo_path) / "src"
                source_dir = str(src_link.resolve()) if src_link.is_symlink() else workspace_path
            chunks = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda dp=db_path: build_symbol_chunks(
                    source_dir, graph_repo_path, ignore_patterns, only_files=[dp]
                ),
            )
            if not chunks:
                # 文件已无符号:删旧 chunk,不写新
                try:
                    await qdrant_mgr.delete_by_filter(
                        collection_name,
                        {"must": [
                            {"key": "document_id", "match": {"value": document_id}},
                            {"key": "file_path", "match": {"value": rel}},
                        ]},
                    )
                except Exception as exc:
                    logger.warning("sync: 删旧 chunk 失败 %s/%s: %s", document_id, rel, exc)
                continue
            batch_texts = [c["embed_text"] for c in chunks]
            vectors = await asyncio.get_running_loop().run_in_executor(
                None, lambda: encode_texts(embedding_model, batch_texts)
            )
            points: List[Dict[str, Any]] = []
            for offset, chunk in enumerate(chunks):
                payload = {
                    "document_id": document_id,
                    "filename": chunk.get("filename", ""),
                    "file_path": chunk.get("file_path", ""),
                    "language": chunk.get("language", ""),
                    "chunk_index": int(chunk.get("chunk_index", offset)),
                    "chunk_text": chunk.get("chunk_text", ""),
                    "chunk_type": (
                        "function" if chunk.get("function_name")
                        else "class" if chunk.get("class_name")
                        else "module"
                    ),
                    "function_name": chunk.get("function_name"),
                    "class_name": chunk.get("class_name"),
                    "start_line": int(chunk.get("start_line", 1)),
                    "end_line": int(chunk.get("end_line", 1)),
                    "callers": chunk.get("callers", []),
                    "callees": chunk.get("callees", []),
                    "imports": chunk.get("imports", []),
                    "signature": chunk.get("signature", ""),
                    "docstring": chunk.get("docstring", ""),
                    "embedding_model": embedding_model,
                }
                points.append({
                    "id": str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{document_id}:{payload['file_path']}:{payload['chunk_index']}",
                    )),
                    "vector": vectors[offset],
                    "payload": payload,
                })
            # 新 chunk 已就绪:先删旧同文件 chunk,再 upsert 新(失败仅告警,旧已删但
            # 新未写——该文件检索暂时缺失,下次 sync 若源再变会重试;权衡优于删后
            # build 抛异常导致永久丢失且无重试机会的旧实现)。
            try:
                await qdrant_mgr.delete_by_filter(
                    collection_name,
                    {"must": [
                        {"key": "document_id", "match": {"value": document_id}},
                        {"key": "file_path", "match": {"value": rel}},
                    ]},
                )
            except Exception as exc:
                logger.warning("sync: 删旧 chunk 失败 %s/%s: %s", document_id, rel, exc)
            try:
                resp = await qdrant_mgr._http.put(
                    f"/collections/{collection_name}/points",
                    json={"points": points},
                )
                resp.raise_for_status()
                refreshed += len(points)
            except Exception as exc:
                logger.warning("sync: upsert 失败 %s/%s: %s", document_id, rel, exc)

        # 删除的文件:清掉 Qdrant 里该文件的旧 chunk
        for db_path in deleted_db_paths:
            rel = db_path
            if rel.startswith("src/"):
                rel = rel[len("src/"):]
            try:
                await qdrant_mgr.delete_by_filter(
                    collection_name,
                    {"must": [
                        {"key": "document_id", "match": {"value": document_id}},
                        {"key": "file_path", "match": {"value": rel}},
                    ]},
                )
            except Exception as exc:
                logger.warning("sync: 删已移除文件 chunk 失败 %s/%s: %s", document_id, rel, exc)

        return {
            "document_id": document_id,
            "synced_files": len(changed_db_paths),
            "refreshed_symbols": refreshed,
            "deleted_files": len(deleted_db_paths),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("code rag sync %s failed: %s", document_id, exc)
        raise HTTPException(status_code=500, detail=f"sync 失败: {exc}")


# ----------------------------------------------------------------------
# Repository graph query (走 codegraph CLI,供 CLI 与 Control Panel 调用)
# ----------------------------------------------------------------------


def _resolve_graph_repo_path(request: Request, document_id: str) -> str:
    """从 document_id 推出 {graph_db_dir}/{document_id}/ 目录,校验存在。"""
    app_state = request.app.state
    code_cfg = _load_code_rag_config(app_state)
    graph_db_dir = str(code_cfg.get("graph_db_dir") or "/data/code_graphs")
    graph_repo_path = str(Path(graph_db_dir) / document_id)
    if not Path(graph_repo_path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"图谱目录不存在: {document_id}(可能已删除或导入未完成)",
        )
    return graph_repo_path


@router.get("/rag/code/repositories/{document_id}/query")
async def query_code_symbols(
    document_id: str,
    request: Request,
    symbol: str,
    kind: Optional[str] = None,
    limit: int = 10,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    """符号搜索(走 codegraph query --json)。"""
    from aigateway_core.pipelines.understanding.code_rag.graph_query import query_symbols

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: query_symbols(graph_repo_path, symbol, kind=kind, limit=limit),
        )
    except Exception as exc:
        logger.warning("code query 失败 %s/%s: %s", document_id, symbol, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")


@router.get("/rag/code/repositories/{document_id}/callers")
async def get_code_callers(
    document_id: str,
    request: Request,
    symbol: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_callers

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        callers = await loop.run_in_executor(
            None, lambda: get_callers(graph_repo_path, symbol)
        )
    except Exception as exc:
        logger.warning("code callers 失败 %s/%s: %s", document_id, symbol, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")
    return {"symbol": symbol, "callers": callers}


@router.get("/rag/code/repositories/{document_id}/callees")
async def get_code_callees(
    document_id: str,
    request: Request,
    symbol: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_callees

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        callees = await loop.run_in_executor(
            None, lambda: get_callees(graph_repo_path, symbol)
        )
    except Exception as exc:
        logger.warning("code callees 失败 %s/%s: %s", document_id, symbol, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")
    return {"symbol": symbol, "callees": callees}


@router.get("/rag/code/repositories/{document_id}/impact")
async def get_code_impact(
    document_id: str,
    request: Request,
    symbol: str,
    depth: int = 2,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_impact

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: get_impact(graph_repo_path, symbol, depth=depth)
        )
    except Exception as exc:
        logger.warning("code impact 失败 %s/%s: %s", document_id, symbol, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")


@router.get("/rag/code/repositories/{document_id}/node")
async def get_code_node(
    document_id: str,
    request: Request,
    symbol: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_node

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: get_node(graph_repo_path, symbol)
        )
    except Exception as exc:
        logger.warning("code node 失败 %s/%s: %s", document_id, symbol, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")


@router.get("/rag/code/repositories/{document_id}/files")
async def list_code_files(
    document_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import list_files

    graph_repo_path = _resolve_graph_repo_path(request, document_id)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: list_files(graph_repo_path)
        )
    except Exception as exc:
        logger.warning("code files 失败 %s: %s", document_id, exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")
