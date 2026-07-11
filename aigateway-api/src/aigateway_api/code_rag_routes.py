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
# Redis key naming
# ----------------------------------------------------------------------

_TASK_KEY_TMPL = "aigateway:rag:code:tasks:{task_id}"
_REPO_LIST_KEY = "aigateway:rag:code:documents"
_TASK_TTL_SECONDS = 24 * 3600
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
# Task state helpers (Redis-backed)
# ----------------------------------------------------------------------


def _task_key(task_id: str) -> str:
    return _TASK_KEY_TMPL.format(task_id=task_id)


async def _write_task_state(
    redis_mgr: Any,
    task_id: str,
    fields: Dict[str, Any],
) -> None:
    """把任务状态字段合并写入 Redis Hash + 刷新 TTL.

    值类型: 字符串直接存,dict/list 走 json.dumps。
    """
    if redis_mgr is None or redis_mgr.redis is None:
        return
    now = int(time.time())
    payload: Dict[str, str] = {"updated_at": str(now)}
    for k, v in fields.items():
        if v is None:
            payload[k] = ""
        elif isinstance(v, (dict, list)):
            payload[k] = json.dumps(v, ensure_ascii=False)
        else:
            payload[k] = str(v)
    await redis_mgr.redis.hset(_task_key(task_id), mapping=payload)
    await redis_mgr.redis.expire(_task_key(task_id), _TASK_TTL_SECONDS)


async def _read_task_state(redis_mgr: Any, task_id: str) -> Optional[Dict[str, Any]]:
    if redis_mgr is None or redis_mgr.redis is None:
        return None
    raw = await redis_mgr.redis.hgetall(_task_key(task_id))
    if not raw:
        return None
    decoded: Dict[str, Any] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        decoded[key] = val
    return decoded


def _shape_task_response(task_id: str, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 Redis Hash 里的字段规范化为 API 输出结构.

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
) -> str:
    """浅克隆到临时目录,返回临时目录路径。

    通过 GIT_HTTP_LOW_SPEED_LIMIT/TIME + 外部 wall-clock 阻断长时间挂住的克隆,
    避免 "silently stuck" 出现在 gateway 侧。
    """
    from git import Repo  # lazy: gitpython 只在生产镜像里装

    tmp_dir = tempfile.mkdtemp(prefix="code_rag_git_")
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
            Repo.clone_from(git_url, tmp_dir, **clone_kwargs)

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
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError(f"git clone timed out after {timeout}s: {git_url}")
        if error:
            raise error[0]
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return tmp_dir


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
    redis_mgr = getattr(app_state, "redis_manager", None)
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
            redis_mgr,
            task_id,
            {
                "status": "failed",
                "error": f"import task exceeded {deadline_seconds}s deadline",
            },
        )
    except asyncio.CancelledError:
        # 用户取消透传,由内层 _run_code_import_task 处理 Redis 标记
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
    graph_db_dir: str,
    cleanup_dirs: List[str],
) -> None:
    """异步导入任务主体(见 spec: Async task flow)."""
    from aigateway_core.pipelines.understanding.code_rag.embedding_router import (
        encode_texts,
        probe_embedding_dimension,
        resolve_collection_name,
    )
    from aigateway_core.pipelines.understanding.code_rag.graph_builder import build_code_graph
    from aigateway_core.pipelines.understanding.code_rag.graph_query import lookup_symbol_metadata_strict
    from aigateway_core.pipelines.understanding.code_rag.splitter import split_code_directory

    redis_mgr = getattr(app_state, "redis_manager", None)
    qdrant_mgr = getattr(app_state, "qdrant_manager", None)

    async def _mark(**fields: Any) -> None:
        await _write_task_state(redis_mgr, task_id, fields)

    collection_name = resolve_collection_name(embedding_model)
    graph_db_path = str(Path(graph_db_dir) / f"{document_id}.db")
    written_points = False

    try:
        # 1) split
        await _mark(status="splitting", current_file=None, error=None)
        loop = asyncio.get_running_loop()
        chunks: List[Dict[str, Any]] = await loop.run_in_executor(
            None, lambda: split_code_directory(source_dir, ignore_patterns)
        )
        await _mark(total=len(chunks))
        if not chunks:
            await _mark(status="completed", done=0)
            return

        # 2) build code graph (strict — 失败即整体失败)
        await _mark(status="building_graph")
        await loop.run_in_executor(None, lambda: build_code_graph(source_dir, graph_db_path))

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

        # 4) encode + enrich + upsert（分批,避免 1000+ chunks 一次性 encode 卡死）
        # 大仓库（例如 click / 本仓库）切出来的 chunk 数可能轻松上千；若一次性
        # 调 sentence-transformers.encode(texts) 会出现：
        #   - 长时间无进度更新，看起来像任务“卡在 embedding”
        #   - 内存峰值偏高
        # 因此改为固定批次编码 + 固定批次 upsert；每批后刷新 done/current_file。
        batch_size = 64
        payloads: List[Dict[str, Any]] = []
        processed = 0

        for batch_start in range(0, len(chunks), batch_size):
            batch_chunks = chunks[batch_start : batch_start + batch_size]
            batch_texts = [c["chunk_text"] for c in batch_chunks]
            batch_vectors = await loop.run_in_executor(
                None, lambda: encode_texts(embedding_model, batch_texts)
            )

            batch_points: List[Dict[str, Any]] = []
            for offset, chunk in enumerate(batch_chunks):
                global_idx = batch_start + offset
                symbol_name = (
                    chunk.get("function_name")
                    or chunk.get("class_name")
                    or None
                )
                graph_meta = lookup_symbol_metadata_strict(
                    graph_db_path,
                    chunk.get("file_path") or "",
                    symbol_name,
                    chunk.get("chunk_text") or "",
                )
                payload = {
                    "document_id": document_id,
                    "filename": chunk.get("filename", ""),
                    "file_path": chunk.get("file_path", ""),
                    "language": chunk.get("language", ""),
                    "chunk_index": int(chunk.get("chunk_index", global_idx)),
                    "chunk_text": chunk.get("chunk_text", ""),
                    "chunk_type": graph_meta.get("chunk_type", "module"),
                    "function_name": graph_meta.get("function_name"),
                    "class_name": graph_meta.get("class_name"),
                    "start_line": int(chunk.get("start_line", 1)),
                    "end_line": int(chunk.get("end_line", 1)),
                    "callers": graph_meta.get("callers", []),
                    "callees": graph_meta.get("callees", []),
                    "imports": graph_meta.get("imports", []),
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
            "file_count": len({p["file_path"] for p in payloads if p["file_path"]}),
            "language_summary": sorted({p["language"] for p in payloads if p["language"]}),
            "function_count": sum(1 for p in payloads if p["chunk_type"] == "function"),
            "class_count": sum(1 for p in payloads if p["chunk_type"] == "class"),
            "chunk_count": len(payloads),
            "embedding_model": embedding_model,
            "import_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        await _append_repository(redis_mgr, repo_meta)

        await _mark(status="completed", error=None)

    except asyncio.CancelledError:
        # 用户主动取消,不记为 failed
        logger.info("code rag import task %s cancelled by user", task_id)
        await _mark(status=_CANCELLED, error=None)
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
            redis_mgr,
            task_id,
            {"status": "failed", "error": str(exc)},
        )
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

    if "multipart/form-data" in content_type:
        form = await request.form()
        source_type = str(form.get("source_type") or "")
        embedding_model = str(form.get("embedding_model") or "Qwen/Qwen3-Embedding-0.6B")
        if source_type == "folder":
            uploads = form.getlist("files") if hasattr(form, "getlist") else form.getlist("files")  # type: ignore[attr-defined]
            relative_paths = [
                str(p) for p in (form.getlist("relative_paths") if hasattr(form, "getlist") else [])
            ]
            files: List[UploadFile] = [u for u in uploads if isinstance(u, UploadFile)]
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
            if not isinstance(upload, UploadFile):
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
            source_dir = str(resolved)
            source_label = f"server_path://{resolved}"
        elif source_type == "git":
            _validate_git_url(body.git_url or "")
            source_dir = _materialize_git_repo(body.git_url or "", body.git_branch)
            source_label = f"git://{body.git_url}"
            cleanup_dirs.append(source_dir)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"JSON 请求只支持 server_path/git,收到 {source_type}",
            )

    redis_mgr = getattr(app_state, "redis_manager", None)
    await _write_task_state(
        redis_mgr,
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
            graph_db_dir=graph_db_dir,
            cleanup_dirs=cleanup_dirs,
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
    1. 先读 Redis 确认任务状态,已是终态则直接返回(不覆盖)
    2. 标记 Redis 状态为 cancelled
    3. 从 app.state.code_rag_active_tasks 中找到 asyncio.Task 并 cancel()
    4. asyncio 会在下一次 await 处抛出 CancelledError,
       _run_code_import_task 的 except 块捕获后走 finally 清理。
    """
    app_state = request.app.state
    redis_mgr = getattr(app_state, "redis_manager", None)

    # 0) 检查当前状态,不存在则直接报 404,避免伪造 cancelled 任务
    existing = await _read_task_state(redis_mgr, task_id)
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

    # 1) 写 Redis 终态
    await _write_task_state(redis_mgr, task_id, {"status": _CANCELLED})

    # 2) 取消 asyncio.Task
    active = getattr(app_state, "code_rag_active_tasks", None)
    if active and task_id in active:
        active[task_id].cancel()
        logger.info("Cancelled code rag import task %s", task_id)
    else:
        # 任务已完成/从未创建,Redis 标记已写,静默返回
        logger.info("Code rag import task %s not found in active tasks (already done?)", task_id)

    return {"task_id": task_id, "status": _CANCELLED}


@router.get("/rag/code/tasks")
async def list_code_tasks(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> List[Dict[str, Any]]:
    """列出所有任务(含终态),附带 source_label 供前端展示。

    前端页面刷新后可用此接口恢复正在运行的任务。
    """
    redis_mgr = getattr(request.app.state, "redis_manager", None)
    if redis_mgr is None or redis_mgr.redis is None:
        return []

    # 使用 SCAN 替代 KEYS,避免阻塞 Redis 单线程事件循环
    pattern = _TASK_KEY_TMPL.format(task_id="*")
    cursor = 0
    all_keys: List[str] = []
    while True:
        cursor, keys = await redis_mgr.redis.scan(cursor, match=pattern, count=100)
        all_keys.extend(keys)
        if cursor == 0:
            break
    active: List[Dict[str, Any]] = []
    for key in all_keys:
        k = key.decode() if isinstance(key, bytes) else key
        tid = k.rsplit(":", 1)[-1]
        raw = await redis_mgr.redis.hgetall(key)
        if not raw:
            continue
        active.append(_shape_task_response(tid, raw))
    # 按 created_at 倒序(最新的在前)
    active.sort(key=lambda t: int(t.get("created_at") or 0), reverse=True)
    return active


@router.get("/rag/code/tasks/{task_id}")
async def get_code_task(
    task_id: str,
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
) -> Dict[str, Any]:
    redis_mgr = getattr(request.app.state, "redis_manager", None)
    state = await _read_task_state(redis_mgr, task_id)
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

    graph_db_path = Path(graph_db_dir) / f"{document_id}.db"
    if graph_db_path.exists():
        try:
            graph_db_path.unlink()
        except OSError as exc:
            logger.warning("删除图谱库 %s 失败: %s", graph_db_path, exc)

    await _remove_repository(getattr(app_state, "redis_manager", None), document_id)
