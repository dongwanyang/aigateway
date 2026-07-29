"""Runtime capability discovery for split AI Gateway images."""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Callable
from functools import lru_cache
from typing import Any


def _package_installed(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


@lru_cache(maxsize=1)
def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except (ImportError, RuntimeError):
        return False


def _config_value(state: Any, key: str, default: Any = None) -> Any:
    manager = getattr(state, "config_manager", None)
    if manager is None:
        return default
    try:
        return manager.get(key, default)
    except Exception:
        return default


def detect_runtime_capabilities(
    state: Any,
    *,
    package_probe: Callable[[str], bool] | None = None,
    executable_probe: Callable[[str], str | None] | None = None,
    cuda_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Return installed/configured/available state for optional image features."""
    has_package = package_probe or _package_installed
    find_executable = executable_probe or shutil.which
    has_cuda = cuda_probe or _cuda_available

    profile = os.environ.get("AI_GATEWAY_IMAGE_PROFILE", "development")
    cache_manager = getattr(state, "cache_manager", None)
    qdrant_ready = bool(
        cache_manager is not None
        and getattr(cache_manager, "_qdrant_client", None) is not None
    )
    code_rag_cfg = _config_value(state, "code_rag", {}) or {}
    code_rag_enabled = bool(
        isinstance(code_rag_cfg, dict) and code_rag_cfg.get("enabled", True)
    )

    rag_installed = all(
        has_package(module)
        for module in (
            "sentence_transformers",
            "llama_index",
            "qdrant_client",
            "langchain_classic",
        )
    )
    rag_available = rag_installed and qdrant_ready

    code_rag_installed = (
        rag_installed
        and has_package("langchain_text_splitters")
        and has_package("git")
        and find_executable("git") is not None
        and find_executable("codegraph") is not None
    )
    code_rag_available = code_rag_installed and qdrant_ready and code_rag_enabled

    vision_installed = all(
        has_package(module) for module in ("PIL", "cv2", "numpy", "pytesseract")
    )
    vision_available = vision_installed and find_executable("ffmpeg") is not None

    upscaling_installed = (
        vision_installed
        and has_package("realesrgan")
        and has_package("basicsr")
        and has_package("torch")
    )
    weights_path = os.environ.get(
        "AI_GATEWAY_REALESRGAN_WEIGHTS",
        "/app/weights/RealESRGAN_x4plus.pth",
    )
    weights_available = os.path.isfile(weights_path)
    upscaling_available = upscaling_installed and weights_available

    torch_installed = has_package("torch")
    cuda_available = torch_installed and has_cuda()

    return {
        "profile": profile,
        "capabilities": {
            "core": {
                "installed": True,
                "configured": True,
                "available": True,
                "install_command": None,
                "reason": None,
            },
            "rag": {
                "installed": rag_installed,
                "configured": qdrant_ready,
                "available": rag_available,
                "install_command": "bash scripts/quickstart.sh --edition knowledge",
                "reason": (
                    None
                    if rag_available
                    else "RAG 依赖未安装"
                    if not rag_installed
                    else "Qdrant 未连接"
                ),
            },
            "code_rag": {
                "installed": code_rag_installed,
                "configured": qdrant_ready and code_rag_enabled,
                "available": code_rag_available,
                "install_command": "bash scripts/quickstart.sh --edition knowledge",
                "reason": (
                    None
                    if code_rag_available
                    else "Code RAG、Git 或 CodeGraph 依赖未安装"
                    if not code_rag_installed
                    else "Code RAG 未启用或 Qdrant 未连接"
                ),
            },
            "vision": {
                "installed": vision_installed,
                "configured": True,
                "available": vision_available,
                "install_command": "bash scripts/quickstart.sh --edition studio",
                "reason": (
                    None
                    if vision_available
                    else "视觉依赖未安装"
                    if not vision_installed
                    else "FFmpeg 不可用"
                ),
            },
            "upscaling": {
                "installed": upscaling_installed,
                "configured": weights_available,
                "available": upscaling_available,
                "install_command": "bash scripts/quickstart.sh --edition studio",
                "reason": (
                    None
                    if upscaling_available
                    else "RealESRGAN 依赖未安装"
                    if not upscaling_installed
                    else "RealESRGAN 权重不存在"
                ),
            },
            "gpu": {
                "installed": torch_installed,
                "configured": profile in {"gpu", "rag", "vision", "full"},
                "available": cuda_available,
                "install_command": "bash scripts/quickstart.sh --edition full",
                "reason": (
                    None
                    if cuda_available
                    else "PyTorch 未安装"
                    if not torch_installed
                    else "未检测到可用 CUDA 设备"
                ),
            },
        },
    }
