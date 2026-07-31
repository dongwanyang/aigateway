#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Gateway L3 model lifecycle
# ---------------------------------------------------------------------------
l3_path = "aigateway-core/src/aigateway_core/prefix/cache/l3_semantic.py"
replace_once(
    l3_path,
    '''import asyncio
import logging
import os
import threading
''',
    '''import asyncio
import gc
import logging
import os
import threading
''',
)
replace_once(
    l3_path,
    '''_l3_device: str = "auto"
_l3_model_name: str = "Qwen/Qwen3-Embedding-0.6B"
''',
    '''_l3_device: str = "auto"
_l3_model_name: str = "Qwen/Qwen3-Embedding-0.6B"
_l3_idle_unload_seconds: float = 300.0
_l3_idle_generation: int = 0
_l3_idle_task: asyncio.Task[None] | None = None
''',
)
replace_once(
    l3_path,
    '''def set_l3_model(model_name: str) -> None:
    """Set the configured L3 model path before the first model load."""
    global _l3_model_name
    if _l3_model_cache:
        raise RuntimeError("L3 embedding model is already loaded")
    _l3_model_name = model_name.strip() or "Qwen/Qwen3-Embedding-0.6B"


async def _compute_l3_vector''',
    '''def set_l3_model(model_name: str) -> None:
    """Set the configured L3 model path before the first model load."""
    global _l3_model_name
    if _l3_model_cache:
        raise RuntimeError("L3 embedding model is already loaded")
    _l3_model_name = model_name.strip() or "Qwen/Qwen3-Embedding-0.6B"


def set_l3_idle_unload_seconds(seconds: float) -> None:
    """Configure automatic model release after an idle interval.

    ``0`` disables automatic release.  The setting only affects future inference
    calls and never interrupts an active model invocation because release uses the
    same module lock as loading and inference.
    """
    global _l3_idle_unload_seconds
    value = float(seconds)
    if value < 0:
        raise ValueError("embedding.idle_unload_seconds must be >= 0")
    _l3_idle_unload_seconds = value


def release_l3_model() -> bool:
    """Release the cached L3 model and return whether anything was resident."""
    global _l3_idle_generation, _l3_idle_task
    _l3_idle_generation += 1
    task = _l3_idle_task
    _l3_idle_task = None
    if task is not None and not task.done():
        task.cancel()

    with _l3_model_lock:
        model = _l3_model_cache.pop("model", None)
        tokenizer = _l3_model_cache.pop("tokenizer", None)
        _l3_model_cache.pop("device", None)
        resident = model is not None or tokenizer is not None
        if model is not None:
            try:
                model.to("cpu")
            except Exception:
                pass
        del model, tokenizer

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return resident


def _schedule_idle_release() -> None:
    global _l3_idle_generation, _l3_idle_task
    if _l3_idle_unload_seconds <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _l3_idle_generation += 1
    generation = _l3_idle_generation
    previous = _l3_idle_task
    if previous is not None and not previous.done():
        previous.cancel()

    async def release_after_idle() -> None:
        try:
            await asyncio.sleep(_l3_idle_unload_seconds)
        except asyncio.CancelledError:
            return
        if generation != _l3_idle_generation:
            return
        await asyncio.to_thread(release_l3_model)
        logger.info("L3 embedding model released after %.0fs idle", _l3_idle_unload_seconds)

    _l3_idle_task = loop.create_task(release_after_idle())


async def _compute_l3_vector''',
)
replace_once(
    l3_path,
    '''    if not load_if_missing and "tokenizer" not in _l3_model_cache:
        return None
    return await asyncio.to_thread(
        _compute_l3_vector_sync,
        text,
        load_if_missing,
    )
''',
    '''    if not load_if_missing and "tokenizer" not in _l3_model_cache:
        return None
    result = await asyncio.to_thread(
        _compute_l3_vector_sync,
        text,
        load_if_missing,
    )
    if result is not None:
        _schedule_idle_release()
    return result
''',
)

# ---------------------------------------------------------------------------
# Startup configuration for the idle model lifecycle
# ---------------------------------------------------------------------------
main_path = "aigateway-api/src/aigateway_api/main.py"
replace_once(
    main_path,
    '''            set_l3_device,
            set_l3_model,
        )
''',
    '''            set_l3_device,
            set_l3_idle_unload_seconds,
            set_l3_model,
        )
''',
)
replace_once(
    main_path,
    '''        set_l3_model(config_manager.get("embedding.model", "Qwen/Qwen3-Embedding-0.6B"))
        set_l3_device(l3_dev)
''',
    '''        set_l3_model(config_manager.get("embedding.model", "Qwen/Qwen3-Embedding-0.6B"))
        set_l3_device(l3_dev)
        set_l3_idle_unload_seconds(
            float(config_manager.get("embedding.idle_unload_seconds", 300))
        )
''',
)

# ---------------------------------------------------------------------------
# GPU status/release routes
# ---------------------------------------------------------------------------
Path("aigateway-api/src/aigateway_api/gpu_routes.py").write_text(
    '''"""GPU memory diagnostics and explicit idle-memory release controls."""
from __future__ import annotations

import asyncio
import gc
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth_middleware import authenticate_admin

router = APIRouter()


def _comfy_config(request: Request) -> dict[str, Any]:
    manager = getattr(request.app.state, "config_manager", None)
    value = (
        manager.get("generation_optimization.draft_workflow.comfyui", {})
        if manager is not None
        else {}
    )
    return value if isinstance(value, dict) else {}


def _gateway_cuda_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "device": None,
        "name": None,
        "allocated_bytes": 0,
        "reserved_bytes": 0,
        "device_used_bytes": 0,
        "device_free_bytes": 0,
        "device_total_bytes": 0,
    }
    try:
        import torch
    except ImportError:
        result["error"] = "torch_unavailable"
        return result
    if not torch.cuda.is_available():
        result["error"] = "cuda_unavailable"
        return result
    device = torch.cuda.current_device()
    free_bytes, total_bytes = torch.cuda.mem_get_info(device)
    result.update(
        {
            "available": True,
            "device": device,
            "name": torch.cuda.get_device_name(device),
            "allocated_bytes": int(torch.cuda.memory_allocated(device)),
            "reserved_bytes": int(torch.cuda.memory_reserved(device)),
            "device_used_bytes": int(total_bytes - free_bytes),
            "device_free_bytes": int(free_bytes),
            "device_total_bytes": int(total_bytes),
        }
    )
    return result


def _integer(mapping: Any, *keys: str) -> int | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def _comfy_memory(gpu: Any) -> dict[str, Any] | None:
    if not isinstance(gpu, dict):
        return None
    total = _integer(gpu, "vram_total", "torch_vram_total")
    free = _integer(gpu, "vram_free", "torch_vram_free")
    return {
        "raw": gpu,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free if total is not None and free is not None else None,
    }


async def _probe(request: Request) -> dict[str, Any]:
    from .local_generation import probe_comfyui

    return await probe_comfyui(_comfy_config(request))


def _diagnosis(
    gateway: dict[str, Any],
    comfy_memory: dict[str, Any] | None,
    queue_idle: bool | None,
    shared_gpu: bool,
) -> list[str]:
    findings: list[str] = []
    if shared_gpu:
        findings.append("gateway_and_comfyui_share_one_gpu")
    if gateway.get("available"):
        allocated = int(gateway.get("allocated_bytes", 0) or 0)
        reserved = int(gateway.get("reserved_bytes", 0) or 0)
        if reserved > allocated:
            findings.append("gateway_pytorch_cache_reserved")
        if allocated > 0:
            findings.append("gateway_model_memory_resident")
    if queue_idle and comfy_memory and int(comfy_memory.get("used_bytes") or 0) > 0:
        findings.append("comfyui_idle_with_resident_models")
    return findings


@router.get("/gpu/status")
async def get_gpu_status(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    manager = getattr(request.app.state, "config_manager", None)
    deployment = manager.get("deployment", {}) if manager is not None else {}
    shared_gpu = bool(deployment.get("shared_gpu", False)) if isinstance(deployment, dict) else False
    gateway = await asyncio.to_thread(_gateway_cuda_status)
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    queue_idle = None
    if isinstance(queue, dict):
        queue_idle = int(queue.get("running", 0) or 0) == 0 and int(queue.get("pending", 0) or 0) == 0
    comfy_memory = _comfy_memory(comfy.get("gpu") if isinstance(comfy, dict) else None)
    return {
        "data": {
            "gateway": gateway,
            "comfyui": {
                "available": bool(comfy.get("available")) if isinstance(comfy, dict) else False,
                "memory": comfy_memory,
                "endpoint_errors": comfy.get("endpoint_errors", {}) if isinstance(comfy, dict) else {},
            },
            "queue": queue,
            "queue_idle": queue_idle,
            "shared_gpu": shared_gpu,
            "diagnosis": _diagnosis(gateway, comfy_memory, queue_idle, shared_gpu),
        },
        "message": "success",
    }


async def _release_gateway_models() -> dict[str, bool]:
    from aigateway_core.prefix.cache.l3_semantic import release_l3_model

    l3_released = await asyncio.to_thread(release_l3_model)
    from . import admin_routes

    local_model = admin_routes._embedding_model_cache.pop("model", None)
    local_released = local_model is not None
    if local_model is not None:
        try:
            local_model.to("cpu")
        except Exception:
            pass
        del local_model
    await asyncio.to_thread(gc.collect)
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return {
        "l3_embedding": bool(l3_released),
        "rag_embedding": local_released,
    }


@router.post("/gpu/release")
async def release_gpu_memory(
    request: Request,
    _auth: dict[str, Any] = Depends(authenticate_admin),
):
    comfy = await _probe(request)
    queue = comfy.get("queue") if isinstance(comfy, dict) else None
    if isinstance(queue, dict) and (
        int(queue.get("running", 0) or 0) > 0
        or int(queue.get("pending", 0) or 0) > 0
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "gpu_busy",
                    "message": "GPU memory cannot be released while ComfyUI has active or pending work",
                }
            },
        )

    released = await _release_gateway_models()
    comfy_release: dict[str, Any] = {"requested": False, "released": False}
    config = _comfy_config(request)
    server_url = str(config.get("server_url") or "").rstrip("/")
    if server_url and bool(comfy.get("available")):
        comfy_release["requested"] = True
        try:
            timeout = httpx.Timeout(5.0, read=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{server_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
            response.raise_for_status()
            comfy_release["released"] = True
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            comfy_release["error"] = type(exc).__name__

    return {
        "data": {
            "gateway_models": released,
            "comfyui": comfy_release,
            "gateway": await asyncio.to_thread(_gateway_cuda_status),
        },
        "message": "success",
    }


def install_gpu_routes(admin_router: APIRouter) -> None:
    marker = "_aigateway_gpu_routes_installed"
    if getattr(admin_router, marker, False):
        return
    new_routes = list(router.routes)
    paths = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set()) or set())))
        for route in new_routes
    }
    admin_router.routes[:] = [
        route
        for route in admin_router.routes
        if (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", set()) or set())),
        ) not in paths
    ]
    admin_router.routes[0:0] = new_routes
    setattr(admin_router, marker, True)


__all__ = ["install_gpu_routes", "router"]
''',
    encoding="utf-8",
)

init_path = "aigateway-api/src/aigateway_api/__init__.py"
replace_once(
    init_path,
    '''def _install_config_schema_parser() -> None:
''',
    '''def _install_gpu_routes() -> None:
    """Install authenticated GPU diagnostics and memory-release endpoints."""
    from . import admin_routes
    from .gpu_routes import install_gpu_routes

    install_gpu_routes(admin_routes.router)


def _install_config_schema_parser() -> None:
''',
)
replace_once(
    init_path,
    '''_install_admin_security_guards()
_install_config_schema_parser()
''',
    '''_install_admin_security_guards()
_install_gpu_routes()
_install_config_schema_parser()
''',
)

# ---------------------------------------------------------------------------
# Config defaults and deployment renderer
# ---------------------------------------------------------------------------
template_path = "config.yaml.template"
replace_once(
    template_path,
    '''  device: auto               # L3 向量计算设备：cpu | cuda | auto（auto=有CUDA用CUDA）
''',
    '''  device: auto               # L3 向量计算设备：cpu | cuda | auto（auto=有CUDA用CUDA）
  idle_unload_seconds: 300    # L3 模型空闲多久后卸载；0 表示常驻直到进程退出
''',
)

render_path = "scripts/render-deployment-config.py"
replace_once(
    render_path,
    '''    embedding_url: str,
    monitoring: bool,
) -> dict[str, Any]:
''',
    '''    embedding_url: str,
    monitoring: bool,
    shared_gpu: bool = False,
) -> dict[str, Any]:
''',
)
replace_once(
    render_path,
    '''        else "cuda"
        if knowledge and accelerator == "cuda"
        else "cpu"
''',
    '''        else "cpu"
        if shared_gpu
        else "cuda"
        if knowledge and accelerator == "cuda"
        else "cpu"
''',
)
replace_once(
    render_path,
    '''                "embedding_device": "cuda" if knowledge else "cpu",
''',
    '''                "embedding_device": (
                    "cpu" if shared_gpu else "cuda" if knowledge and accelerator == "cuda" else "cpu"
                ),
''',
)
replace_once(
    render_path,
    '''    embedding_config["device"] = (
        "cuda"
        if knowledge and accelerator == "cuda" and not external_embedding
        else "cpu"
    )
''',
    '''    embedding_config["device"] = (
        "cuda"
        if knowledge and accelerator == "cuda" and not external_embedding and not shared_gpu
        else "cpu"
    )
    embedding_config["idle_unload_seconds"] = 300
''',
)
replace_once(
    render_path,
    '''    prompt_config["device"] = "cuda" if accelerator == "cuda" else "cpu"
''',
    '''    prompt_config["device"] = (
        "cuda" if accelerator == "cuda" and not shared_gpu else "cpu"
    )
''',
)
replace_once(
    render_path,
    '''        "cuda" if studio and accelerator == "cuda" else "cpu"
''',
    '''        "cuda" if studio and accelerator == "cuda" and not shared_gpu else "cpu"
''',
)
replace_once(
    render_path,
    '''        "rag_enabled": knowledge,
    }
''',
    '''        "rag_enabled": knowledge,
        "shared_gpu": shared_gpu,
    }
''',
)
replace_once(
    render_path,
    '''    parser.add_argument("--monitoring", action="store_true")
''',
    '''    parser.add_argument("--monitoring", action="store_true")
    parser.add_argument("--shared-gpu", action="store_true")
''',
)
replace_once(
    render_path,
    '''        embedding_url=args.embedding_url,
        monitoring=args.monitoring,
    )
''',
    '''        embedding_url=args.embedding_url,
        monitoring=args.monitoring,
        shared_gpu=args.shared_gpu,
    )
''',
)

# ---------------------------------------------------------------------------
# Installer GPU detection and CPU fallback
# ---------------------------------------------------------------------------
quick_path = "scripts/quickstart.sh"
replace_once(
    quick_path,
    '''elif [[ "$os_name" == "Linux" ]]; then
  if grep -qi microsoft /proc/version 2>/dev/null; then
    platform="windows-wsl2"
  fi
  if [[ "$edition" != "lite" ]]; then
    accelerator="cuda"
  fi
else
''',
    '''elif [[ "$os_name" == "Linux" ]]; then
  if grep -qi microsoft /proc/version 2>/dev/null; then
    platform="windows-wsl2"
  fi
  if [[ "$edition" != "lite" ]] \
      && command -v nvidia-smi >/dev/null 2>&1 \
      && nvidia-smi -L >/dev/null 2>&1; then
    accelerator="cuda"
  else
    accelerator="cpu"
  fi
else
''',
)
replace_once(
    quick_path,
    '''case "$edition" in
  knowledge) needs_knowledge="true" ;;
  studio) needs_studio="true" ;;
  full) needs_knowledge="true"; needs_studio="true" ;;
esac
''',
    '''case "$edition" in
  knowledge) needs_knowledge="true" ;;
  studio) needs_studio="true" ;;
  full) needs_knowledge="true"; needs_studio="true" ;;
esac
shared_gpu="false"
if [[ "$accelerator" == "cuda" && "$gpu_count" == "1" \
      && "$needs_knowledge" == "true" && "$needs_studio" == "true" ]]; then
  shared_gpu="true"
  gateway_memory_fraction="0.20"
fi
''',
)
replace_once(
    quick_path,
    '''if [[ "$platform" != "apple" && "$comfyui_mode" == "native" ]]; then
  fail "native ComfyUI 当前仅支持 Apple Silicon"
fi
''',
    '''if [[ "$platform" != "apple" && "$comfyui_mode" == "native" ]]; then
  fail "native ComfyUI 当前仅支持 Apple Silicon"
fi
if [[ "$needs_studio" == "true" && "$platform" != "apple" \
      && "$accelerator" != "cuda" && "$comfyui_mode" == "container" ]]; then
  fail "本机未检测到可用 NVIDIA GPU，不能启动容器版 ComfyUI；请使用 --comfyui remote --comfyui-url URL"
fi
''',
)
replace_once(
    quick_path,
    '''case "$edition:$platform" in
  lite:*) target="gateway-runtime"; image_suffix="lite" ;;
  knowledge:apple) target="gateway-rag-cpu"; image_suffix="knowledge" ;;
  knowledge:*) target="gateway-rag"; image_suffix="knowledge-cuda" ;;
  studio:apple) target="gateway-vision-cpu"; image_suffix="studio-arm64" ;;
  studio:*) target="gateway-vision"; image_suffix="studio-cuda" ;;
  full:apple) target="gateway-full-cpu"; image_suffix="full-arm64" ;;
  full:*) target="gateway-full"; image_suffix="full-cuda" ;;
esac
''',
    '''case "$edition:$platform:$accelerator" in
  lite:*:*) target="gateway-runtime"; image_suffix="lite" ;;
  knowledge:apple:*) target="gateway-rag-cpu"; image_suffix="knowledge" ;;
  knowledge:*:cuda) target="gateway-rag"; image_suffix="knowledge-cuda" ;;
  knowledge:*:cpu) target="gateway-rag-cpu"; image_suffix="knowledge" ;;
  studio:apple:*) target="gateway-vision-cpu"; image_suffix="studio-arm64" ;;
  studio:*:cuda) target="gateway-vision"; image_suffix="studio-cuda" ;;
  studio:*:cpu) target="gateway-vision-cpu"; image_suffix="studio-cpu" ;;
  full:apple:*) target="gateway-full-cpu"; image_suffix="full-arm64" ;;
  full:*:cuda) target="gateway-full"; image_suffix="full-cuda" ;;
  full:*:cpu) target="gateway-full-cpu"; image_suffix="full-cpu" ;;
esac
''',
)
replace_once(
    quick_path,
    '''  printf '  Embedding    : %s\\n' "$embedding_mode"
  printf '  Profiles     : %s\\n' "${compose_profiles:-core}"
''',
    '''  printf '  Embedding    : %s\\n' "$embedding_mode"
  printf '  Shared GPU   : %s\\n' "$shared_gpu"
  printf '  Profiles     : %s\\n' "${compose_profiles:-core}"
''',
)
replace_once(
    quick_path,
    '''  echo "GATEWAY_CUDA_MEMORY_FRACTION=$gateway_memory_fraction"
  echo "COMFYUI_VRAM_FLAG=$comfyui_vram_flag"
''',
    '''  echo "GATEWAY_CUDA_MEMORY_FRACTION=$gateway_memory_fraction"
  echo "AIGATEWAY_SHARED_GPU=$shared_gpu"
  echo "COMFYUI_VRAM_FLAG=$comfyui_vram_flag"
''',
)
replace_once(
    quick_path,
    '''[[ "$monitoring" == "true" ]] && render_args+=(--monitoring)
python3 "${render_args[@]}"
''',
    '''[[ "$monitoring" == "true" ]] && render_args+=(--monitoring)
[[ "$shared_gpu" == "true" ]] && render_args+=(--shared-gpu)
python3 "${render_args[@]}"
''',
)

# Publish Linux CPU tags used by the corrected installer.
images_path = ".github/workflows/docker-images.yml"
replace_once(
    images_path,
    '''          - target: gateway-vision-cpu
            suffix: studio-arm64
            platforms: linux/arm64
''',
    '''          - target: gateway-vision-cpu
            suffix: studio-arm64
            platforms: linux/arm64
          - target: gateway-vision-cpu
            suffix: studio-cpu
            platforms: linux/amd64
''',
)
replace_once(
    images_path,
    '''          - target: gateway-full-cpu
            suffix: full-arm64
            platforms: linux/arm64
''',
    '''          - target: gateway-full-cpu
            suffix: full-arm64
            platforms: linux/arm64
          - target: gateway-full-cpu
            suffix: full-cpu
            platforms: linux/amd64
''',
)

# ---------------------------------------------------------------------------
# Control-panel GPU diagnostics
# ---------------------------------------------------------------------------
client_path = "control-panel/src/api/client.ts"
replace_once(
    client_path,
    '''export async function getComfyUIStatus(): Promise<ApiResponse<ComfyUIStatus>> { return fetchJson<ComfyUIStatus>('/admin/comfyui/status') }
''',
    '''export async function getComfyUIStatus(): Promise<ApiResponse<ComfyUIStatus>> { return fetchJson<ComfyUIStatus>('/admin/comfyui/status') }
export interface GpuStatusData {
  gateway: { available: boolean; name?: string | null; allocated_bytes: number; reserved_bytes: number; device_used_bytes: number; device_free_bytes: number; device_total_bytes: number }
  comfyui: { available: boolean; memory: { total_bytes: number | null; free_bytes: number | null; used_bytes: number | null } | null; endpoint_errors?: Record<string, string> }
  queue: { running?: number; pending?: number } | null
  queue_idle: boolean | null
  shared_gpu: boolean
  diagnosis: string[]
}
export async function getGpuStatus(): Promise<ApiResponse<GpuStatusData>> { return fetchJson<GpuStatusData>('/admin/gpu/status') }
export async function releaseGpuMemory(): Promise<ApiResponse<{ gateway_models: Record<string, boolean>; comfyui: Record<string, unknown>; gateway: GpuStatusData['gateway'] }>> { return fetchJson('/admin/gpu/release', { method: 'POST', body: JSON.stringify({}) }) }
''',
)

config_path = "control-panel/src/pages/Config.tsx"
replace_once(
    config_path,
    '''import { getComfyUIStatus, getGenerationPresets } from '@/api/client'
''',
    '''import { getComfyUIStatus, getGenerationPresets, getGpuStatus, releaseGpuMemory } from '@/api/client'
''',
)
replace_once(
    config_path,
    '''function stringList(value: unknown): string[] {
''',
    '''function formatBytes(value: unknown): string {
  const bytes = typeof value === 'number' && Number.isFinite(value) ? value : 0
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index >= 3 ? 2 : 0)} ${units[index]}`
}

function stringList(value: unknown): string[] {
''',
)
replace_once(
    config_path,
    '''  const presetsQuery = useQuery({
    queryKey: ['generation-presets'],
    queryFn: async () => (await getGenerationPresets()).data,
  })
  const saveMutation = useMutation({ mutationFn: updateTableConfig })
''',
    '''  const presetsQuery = useQuery({
    queryKey: ['generation-presets'],
    queryFn: async () => (await getGenerationPresets()).data,
  })
  const gpuQuery = useQuery({
    queryKey: ['gpu', 'status'],
    queryFn: async () => (await getGpuStatus()).data,
    refetchInterval: 30_000,
  })
  const releaseGpuMutation = useMutation({
    mutationFn: releaseGpuMemory,
    onSuccess: async () => {
      await Promise.all([gpuQuery.refetch(), comfyQuery.refetch()])
    },
  })
  const saveMutation = useMutation({ mutationFn: updateTableConfig })
''',
)
replace_once(
    config_path,
    '''      comfyQuery.refetch(),
      presetsQuery.refetch(),
''',
    '''      comfyQuery.refetch(),
      presetsQuery.refetch(),
      gpuQuery.refetch(),
''',
)
replace_once(
    config_path,
    '''          {comfyStatus?.queue && (
            <span className="text-sm">队列：{comfyStatus.queue.running ?? 0} 运行 / {comfyStatus.queue.pending ?? 0} 等待</span>
          )}
        </div>

        <div className="space-y-2">
''',
    '''          {comfyStatus?.queue && (
            <span className="text-sm">队列：{comfyStatus.queue.running ?? 0} 运行 / {comfyStatus.queue.pending ?? 0} 等待</span>
          )}
          <button
            className="btn btn-secondary"
            disabled={gpuQuery.data?.queue_idle !== true || releaseGpuMutation.isPending}
            onClick={() => releaseGpuMutation.mutate()}
            title={gpuQuery.data?.queue_idle === true ? '卸载空闲模型并清理 PyTorch 缓存' : '队列非空时不能释放显存'}
          >
            <RefreshCw size={14} /> {releaseGpuMutation.isPending ? '释放中...' : '释放空闲显存'}
          </button>
        </div>

        {gpuQuery.data && (
          <div className="grid grid-cols-1 gap-3 mb-4 md:grid-cols-2">
            <div className="rounded-lg border p-3" style={{ borderColor: 'var(--color-border)' }}>
              <div className="text-xs font-semibold mb-2">Gateway / PyTorch</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                allocated {formatBytes(gpuQuery.data.gateway.allocated_bytes)} · reserved {formatBytes(gpuQuery.data.gateway.reserved_bytes)}
              </div>
            </div>
            <div className="rounded-lg border p-3" style={{ borderColor: 'var(--color-border)' }}>
              <div className="text-xs font-semibold mb-2">ComfyUI / Device</div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                used {formatBytes(gpuQuery.data.comfyui.memory?.used_bytes)} · free {formatBytes(gpuQuery.data.comfyui.memory?.free_bytes)}
              </div>
            </div>
            <div className="md:col-span-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              队列为空只表示没有执行任务；模型权重和 PyTorch reserved cache 仍可能常驻显存。
              {gpuQuery.data.shared_gpu ? ' 当前 Gateway 与 ComfyUI 共用同一块 GPU。' : ''}
            </div>
          </div>
        )}

        {releaseGpuMutation.error instanceof Error && (
          <div role="alert" className="text-xs mb-4" style={{ color: 'var(--color-danger)' }}>
            显存释放失败：{releaseGpuMutation.error.message}
          </div>
        )}

        <div className="space-y-2">
''',
)

# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------
Path("tests/unit/test_gpu_resource_policy.py").write_text(
    '''from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigateway_api import gpu_routes
from aigateway_core.prefix.cache import l3_semantic


def load_renderer():
    path = Path(__file__).resolve().parents[2] / "scripts" / "render-deployment-config.py"
    spec = importlib.util.spec_from_file_location("render_deployment_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_gpu_renderer_moves_gateway_models_to_cpu(tmp_path: Path) -> None:
    renderer = load_renderer()
    source = Path(__file__).resolve().parents[2] / "config.yaml.template"
    config = renderer.render(
        source,
        edition="full",
        accelerator="cuda",
        embedding_mode="container",
        comfyui_url="http://comfyui:8188",
        embedding_url="",
        monitoring=False,
        shared_gpu=True,
    )
    assert config["embedding"]["device"] == "cpu"
    assert config["deployment"]["shared_gpu"] is True
    assert config["generation_optimization"]["token_compressor"]["clip"]["device"] == "cpu"


def test_release_l3_model_clears_cached_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self) -> None:
            self.devices: list[str] = []

        def to(self, device: str) -> "Model":
            self.devices.append(device)
            return self

    model = Model()
    l3_semantic._l3_model_cache.update({"model": model, "tokenizer": object(), "device": "cuda"})
    monkeypatch.setattr(l3_semantic.gc, "collect", lambda: 0)
    assert l3_semantic.release_l3_model() is True
    assert l3_semantic._l3_model_cache == {}
    assert model.devices == ["cpu"]


@pytest.mark.asyncio
async def test_gpu_release_rejects_active_comfyui_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gpu_routes,
        "_probe",
        lambda _request: asyncio.sleep(0, result={"available": True, "queue": {"running": 1, "pending": 0}}),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_manager=None)))
    with pytest.raises(Exception) as exc_info:
        await gpu_routes.release_gpu_memory(request, {})
    assert getattr(exc_info.value, "status_code", None) == 409
''',
    encoding="utf-8",
)

config_test = "control-panel/src/pages/Config.versioning.test.tsx"
replace_once(
    config_test,
    '''  getGenerationPresets: vi.fn(async () => ({
    data: [],
    message: 'success',
  })),
''',
    '''  getGenerationPresets: vi.fn(async () => ({
    data: [],
    message: 'success',
  })),
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { available: false, allocated_bytes: 0, reserved_bytes: 0, device_used_bytes: 0, device_free_bytes: 0, device_total_bytes: 0 },
      comfyui: { available: false, memory: null },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: false,
      diagnosis: [],
    },
    message: 'success',
  })),
  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),
''',
)

Path("control-panel/src/pages/GpuStatus.regression.test.tsx").write_text(
    '''import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import Config from './Config'

const api = vi.hoisted(() => ({
  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),
  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),
  getGpuStatus: vi.fn(async () => ({
    data: {
      gateway: { available: true, name: 'GPU', allocated_bytes: 1024, reserved_bytes: 2048, device_used_bytes: 4096, device_free_bytes: 4096, device_total_bytes: 8192 },
      comfyui: { available: true, memory: { total_bytes: 8192, free_bytes: 4096, used_bytes: 4096 } },
      queue: { running: 0, pending: 0 },
      queue_idle: true,
      shared_gpu: true,
      diagnosis: ['gateway_and_comfyui_share_one_gpu'],
    },
    message: 'success',
  })),
  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),
}))
vi.mock('@/api/client', () => api)

it('explains resident memory and releases it only while the queue is idle', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })
    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })
    throw new Error(`unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const user = userEvent.setup()
  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)
  expect(await screen.findByText(/队列为空只表示没有执行任务/)).toBeInTheDocument()
  expect(screen.getByText(/共用同一块 GPU/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '释放空闲显存' }))
  expect(api.releaseGpuMemory).toHaveBeenCalledTimes(1)
  vi.unstubAllGlobals()
})
''',
    encoding="utf-8",
)
