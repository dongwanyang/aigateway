#!/usr/bin/env python3
"""Render an edition-specific runtime config without mutating config.yaml."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

EDITIONS = {"lite", "knowledge", "studio", "full"}


def _plugin(config: dict[str, Any], name: str) -> dict[str, Any]:
    for item in config.get("plugins", []):
        if item.get("name") == name:
            return item
    raise ValueError(f"base config is missing plugin {name!r}")


@contextlib.contextmanager
def _config_write_lock(path: Path):
    """Coordinate host refreshes with API writes to a bind-mounted config."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as sibling_lock:
        fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_EX)
        try:
            if not path.exists():
                yield None
                return
            with path.open("r+", encoding="utf-8") as config_handle:
                fcntl.flock(config_handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield config_handle
                finally:
                    fcntl.flock(config_handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_UN)


def _write_locked_yaml(handle: Any, config: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    handle.seek(0)
    original = handle.read()
    try:
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        try:
            handle.seek(0)
            handle.write(original)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            pass
        raise


def render(
    source: Path,
    *,
    edition: str,
    accelerator: str,
    embedding_mode: str,
    comfyui_url: str,
    embedding_url: str,
    monitoring: bool,
    comfyui_mode: str = "remote",
    shared_gpu: bool = False,
) -> dict[str, Any]:
    if edition not in EDITIONS:
        raise ValueError(f"unsupported edition: {edition}")
    if comfyui_mode not in {"container", "native", "remote"}:
        raise ValueError(f"unsupported comfyui mode: {comfyui_mode}")
    with source.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    knowledge = edition in {"knowledge", "full"}
    studio = edition in {"studio", "full"}
    external_embedding = knowledge and embedding_mode in {"native", "remote"}
    local_comfyui_pool = bool(
        studio and accelerator == "cuda" and comfyui_mode == "container"
    )

    for name in ("rag_retriever", "prompt_compress", "conv_compressor"):
        _plugin(config, name)["enabled"] = knowledge
    _plugin(config, "semantic_cache")["enabled"] = knowledge and not external_embedding

    rag_config = _plugin(config, "rag_retriever").setdefault("config", {})
    rag_config["code_rag_enabled"] = knowledge
    rag_config["rerank_device"] = (
        "remote"
        if knowledge and external_embedding
        else "mps"
        if knowledge and accelerator == "mps"
        else "auto"
        if knowledge and accelerator == "cuda"
        else "cpu"
    )
    rag_config["rerank_backend"] = "remote" if external_embedding else "local"
    rag_config["rerank_api_base"] = (
        embedding_url.rstrip("/") if external_embedding else None
    )
    rag_config["rerank_api_key"] = (
        "${EMBEDDING_API_KEY:-local-mps}" if external_embedding else None
    )
    if external_embedding:
        rag_config.update(
            {
                "embedding_backend": "openai",
                "embedding_api_base": embedding_url.rstrip("/"),
                "embedding_api_key": "${EMBEDDING_API_KEY:-local-mps}",
                "embedding_device": "mps" if accelerator == "mps" else "remote",
            }
        )
    else:
        rag_config.update(
            {
                "embedding_backend": "local",
                "embedding_model": "/models/qwen3-embedding-0.6b",
                "embedding_api_base": None,
                "embedding_api_key": None,
                "embedding_device": (
                    "auto" if knowledge and accelerator == "cuda" else "cpu"
                ),
            }
        )

    embedding_config = config.setdefault("embedding", {})
    embedding_config["model"] = "/models/qwen3-embedding-0.6b"
    embedding_config["device"] = (
        "auto"
        if knowledge and accelerator == "cuda" and not external_embedding
        else "cpu"
    )
    embedding_config["idle_unload_seconds"] = 300
    prompt_config = _plugin(config, "prompt_compress").setdefault("config", {})
    prompt_config["device"] = (
        "auto" if accelerator == "cuda" else "cpu"
    )
    config.setdefault("code_rag", {})["enabled"] = knowledge
    config.setdefault("media_optimization", {})["enabled"] = studio
    config.setdefault("observability", {})["prometheus_enabled"] = monitoring

    generation = config.setdefault("generation_optimization", {})
    generation["enabled"] = True
    draft = generation.setdefault("draft_workflow", {})
    draft["enabled"] = studio
    comfy = draft.setdefault("comfyui", {})
    comfy["server_url"] = comfyui_url.rstrip("/")
    comfy["required"] = True
    comfy["scheduler_managed"] = local_comfyui_pool
    token = generation.setdefault("token_compressor", {})
    token.setdefault("clip", {})["device"] = (
        "auto" if studio and accelerator == "cuda" else "cpu"
    )

    scheduler = config.setdefault("gpu_scheduler", {})
    scheduler["enabled"] = accelerator == "cuda"
    scheduler.setdefault("policy", "auto")
    scheduler.setdefault("generation_priority", True)
    scheduler.setdefault("gateway_devices", "auto")
    scheduler.setdefault("comfyui_devices", "auto")
    scheduler.setdefault("gateway_fallback", "cpu")
    scheduler.setdefault("comfyui_dynamic_vram_enabled", False)

    # ``devices`` and ``workers`` are generated from the current host inventory
    # by render-gpu-topology.py. They must not survive a switch to CPU, MPS,
    # native ComfyUI, or a remote endpoint, otherwise a stale local UUID could be
    # advertised as runnable after an edition/topology change.
    if not local_comfyui_pool:
        scheduler.pop("devices", None)
        scheduler.pop("workers", None)
        scheduler.pop("inventory_source", None)

    config["deployment"] = {
        "edition": edition,
        "accelerator": accelerator,
        "embedding_mode": embedding_mode,
        "comfyui_mode": comfyui_mode,
        "comfyui_enabled": studio,
        "rag_enabled": knowledge,
        "shared_gpu": shared_gpu,
    }
    return config


def _atomic_dump(output: Path, config: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        dir=output.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edition", choices=sorted(EDITIONS), required=True)
    parser.add_argument("--accelerator", choices=("cpu", "cuda", "mps"), required=True)
    parser.add_argument(
        "--embedding-mode",
        choices=("container", "native", "remote"),
        required=True,
    )
    parser.add_argument(
        "--comfyui-mode",
        choices=("container", "native", "remote"),
        default="remote",
    )
    parser.add_argument("--comfyui-url", required=True)
    parser.add_argument("--embedding-url", default="")
    parser.add_argument("--monitoring", action="store_true")
    parser.add_argument("--shared-gpu", action="store_true")
    args = parser.parse_args()
    # Keep both the host sibling lock and the bind-mounted config inode lock
    # across source read, deployment mutation and persistence. When source ==
    # output this prevents an online control-panel save from being lost and
    # preserves the inode already mounted in the running Gateway container.
    with _config_write_lock(args.output) as config_handle:
        config = render(
            args.source,
            edition=args.edition,
            accelerator=args.accelerator,
            embedding_mode=args.embedding_mode,
            comfyui_mode=args.comfyui_mode,
            comfyui_url=args.comfyui_url,
            embedding_url=args.embedding_url,
            monitoring=args.monitoring,
            shared_gpu=args.shared_gpu,
        )
        if config_handle is None:
            _atomic_dump(args.output, config)
        else:
            _write_locked_yaml(config_handle, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
