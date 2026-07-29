#!/usr/bin/env python3
"""Run the product draft and same-seed refinement workflows against ComfyUI."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time

from aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig
from aigateway_core.pipelines.generation._common.models import GenerationRequest
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from aigateway_core.shared.integration_configs import ComfyUIConfig
from PIL import Image


def _image_info(data: bytes) -> dict[str, int | str]:
    with Image.open(io.BytesIO(data)) as image:
        return {
            "format": image.format or "unknown",
            "width": image.width,
            "height": image.height,
            "bytes": len(data),
        }


def _video_info(data: bytes) -> dict[str, int | str | bool]:
    return {
        "format": "mp4" if len(data) >= 12 and data[4:8] == b"ftyp" else "unknown",
        "bytes": len(data),
        "has_ftyp_header": len(data) >= 12 and data[4:8] == b"ftyp",
    }


async def _run(args: argparse.Namespace) -> None:
    seed = args.seed
    comfy_config = ComfyUIConfig(
        server_url=args.server_url.rstrip("/"),
        checkpoint_name=args.checkpoint,
        allowed_checkpoints=[args.checkpoint],
        models_path="/comfyui/models",
        output_path="/comfyui/output",
        min_free_gb=30,
        model_budget_gb=30,
        output_budget_gb=10,
        execution_timeout=args.timeout,
        video_enabled=args.video,
        video_width=args.video_width,
        video_height=args.video_height,
        video_frames=args.video_frames,
        video_fps=args.video_fps,
        video_execution_timeout=args.timeout,
    )
    draft_config = DraftWorkflowConfig(
        draft_resolution=(args.draft_size, args.draft_size),
        store_dir="/tmp/aigateway-comfy-smoke",
    )
    strategy = DraftGeneratorStrategy(
        draft_config,
        comfyui_config=comfy_config,
        store_dir="/tmp/aigateway-comfy-smoke",
    )
    request = GenerationRequest(
        prompt=args.prompt,
        request_id=f"gpu-smoke-{int(time.time())}",
    )

    started = time.monotonic()
    draft = await strategy._generate_image_preview_with_comfyui(
        request,
        draft_config,
        seed=seed,
    )
    draft_seconds = time.monotonic() - started

    input_name = await strategy._upload_image(draft, "gpu-smoke-draft.png")
    if args.video:
        video_workflow = strategy._build_video_workflow(
            input_name=input_name,
            prompt=args.prompt,
            seed=seed,
            draft_id=request.request_id,
        )
        video_started = time.monotonic()
        video_prompt_id = await strategy._submit_workflow(video_workflow)
        video = await strategy._poll_result(video_prompt_id, timeout=args.timeout)
        video_seconds = time.monotonic() - video_started
        info = _video_info(video)
        if not info["has_ftyp_header"]:
            raise RuntimeError("ComfyUI video smoke output is not an MP4")
        print(json.dumps({
            "seed": seed,
            "workflow_version": comfy_config.video_workflow_version,
            "diffusion_model": comfy_config.video_diffusion_model,
            "keyframe": _image_info(draft),
            "keyframe_seconds": round(draft_seconds, 3),
            "video": info,
            "video_seconds": round(video_seconds, 3),
            "video_prompt_id": video_prompt_id,
        }, ensure_ascii=False))
        return

    refine_workflow = strategy._build_refine_workflow(
        input_name=input_name,
        prompt=args.prompt,
        seed=seed,
        target_resolution=(args.refine_size, args.refine_size),
    )
    refine_started = time.monotonic()
    refine_prompt_id = await strategy._submit_workflow(refine_workflow)
    refined = await strategy._poll_result(refine_prompt_id, timeout=args.timeout)
    refine_seconds = time.monotonic() - refine_started

    print(json.dumps({
        "seed": seed,
        "workflow_version": comfy_config.workflow_version,
        "checkpoint": args.checkpoint,
        "draft": _image_info(draft),
        "draft_seconds": round(draft_seconds, 3),
        "refined": _image_info(refined),
        "refine_seconds": round(refine_seconds, 3),
        "refine_prompt_id": refine_prompt_id,
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", default="http://comfyui:8188")
    parser.add_argument(
        "--checkpoint",
        default="sd_xl_base_1.0.safetensors",
    )
    parser.add_argument(
        "--prompt",
        default="a small red sailboat on a calm blue lake, clean composition",
    )
    parser.add_argument("--seed", type=int, default=424242)
    parser.add_argument("--draft-size", type=int, default=512)
    parser.add_argument("--refine-size", type=int, default=1024)
    parser.add_argument("--video", action="store_true")
    parser.add_argument("--video-width", type=int, default=512)
    parser.add_argument("--video-height", type=int, default=288)
    parser.add_argument("--video-frames", type=int, default=9)
    parser.add_argument("--video-fps", type=float, default=8)
    parser.add_argument("--timeout", type=int, default=300)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
