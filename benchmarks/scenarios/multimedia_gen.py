"""Multimedia generation scenario adapter.

Handles image and video generation via OpenAI-compatible APIs:
- Image: POST /v1/images/generations (synchronous)
- Video: POST /v1/videos (async) + GET /v1/videos/{id} polling

Design decisions:
- Video polling has max_attempts + timeout -> status="video_timeout"
- Image 4xx/5xx -> status="error" with details
- Default opt-out: only runs with --with-media flag
- Thin wrapper around LiteLLMBridge public methods (per design doc)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


async def generate_image(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: Dict[str, str],
    prompt: str,
    model: str,
) -> Dict[str, Any]:
    """Generate image via POST /v1/images/generations."""
    url = f"{base_url}/v1/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
    }

    start = time.perf_counter()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            elapsed_ms = (time.perf_counter() - start) * 1000

            if resp.status >= 400:
                text = await resp.text()
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status}: {text[:200]}",
                    "latency_ms": elapsed_ms,
                    "media_type": "image",
                }

            body = await resp.json()
            data = body.get("data", [])
            url_result = data[0].get("url", "") if data else ""

            return {
                "ok": True,
                "url": url_result,
                "latency_ms": elapsed_ms,
                "media_type": "image",
            }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)[:200],
            "latency_ms": (time.perf_counter() - start) * 1000,
            "media_type": "image",
        }


async def generate_video(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: Dict[str, str],
    prompt: str,
    model: str,
    max_attempts: int = 30,
    poll_interval: float = 10.0,
) -> Dict[str, Any]:
    """Generate video via async API: POST /v1/videos then poll GET /v1/videos/{id}.

    Args:
        session: aiohttp session for making requests.
        max_attempts: Maximum polling attempts before timeout.
        poll_interval: Seconds between polls.

    Returns dict with keys: ok, task_id, final_status, error, latency_ms.
    """
    submit_url = f"{base_url}/v1/videos"
    submit_payload = {"model": model, "prompt": prompt}

    start = time.perf_counter()

    try:
        # Submit video generation request
        async with session.post(submit_url, headers=headers, json=submit_payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                return {
                    "ok": False,
                    "error": f"Submit failed HTTP {resp.status}: {text[:200]}",
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "media_type": "video",
                    "status": "video_submit_error",
                }

            body = await resp.json()
            task_id = body.get("task_id") or body.get("id")
            if not task_id:
                return {
                    "ok": False,
                    "error": "No task_id in response",
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "media_type": "video",
                    "status": "video_no_task_id",
                }

        # Poll for completion
        for attempt in range(max_attempts):
            poll_url = f"{base_url}/v1/videos/{task_id}"
            try:
                async with session.get(poll_url, headers=headers) as resp:
                    if resp.status >= 400:
                        logger.warning(f"Video poll {attempt + 1}/{max_attempts} failed: {resp.status}")
                        await asyncio.sleep(poll_interval)
                        continue

                    poll_body = await resp.json()
                    status = poll_body.get("status", "unknown")

                    if status == "completed":
                        return {
                            "ok": True,
                            "task_id": task_id,
                            "final_status": status,
                            "latency_ms": (time.perf_counter() - start) * 1000,
                            "media_type": "video",
                            "status": status,
                        }
                    elif status in ("failed", "error"):
                        return {
                            "ok": False,
                            "task_id": task_id,
                            "final_status": status,
                            "error": poll_body.get("error", "Video generation failed"),
                            "latency_ms": (time.perf_counter() - start) * 1000,
                            "media_type": "video",
                            "status": status,
                        }
                    else:
                        logger.debug(f"Video {task_id} status={status}, waiting...")
                        await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"Video poll exception {attempt + 1}/{max_attempts}: {e}")
                await asyncio.sleep(poll_interval)

        # Timeout
        return {
            "ok": False,
            "task_id": task_id,
            "final_status": "timeout",
            "error": f"Video generation timed out after {max_attempts} attempts",
            "latency_ms": (time.perf_counter() - start) * 1000,
            "media_type": "video",
            "status": "video_timeout",
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)[:200],
            "latency_ms": (time.perf_counter() - start) * 1000,
            "media_type": "video",
            "status": "video_exception",
        }


async def run_multimedia(
    base_url: str,
    model: str,
    concurrency: int,
    prices: Optional[Dict[str, Tuple[float, float]]] = None,
    do_judge: bool = False,
    judge_callback: Optional[Callable] = None,
    mode: str = "baseline",
) -> List[Any]:
    """Run multimedia benchmark scenario."""
    from benchmarks.engine import Sample, _get_admin_headers

    prompts = [
        "A serene mountain landscape at sunset",
        "A futuristic cityscape with flying cars",
        "A cute cat sitting on a windowsill",
    ]

    results: List[Any] = []

    async with aiohttp.ClientSession() as session:
        headers = _get_admin_headers()

        for i, prompt in enumerate(prompts):
            image_result = await generate_image(
                session=session,
                base_url=base_url,
                headers=headers,
                prompt=prompt,
                model=model,
            )

            sample = Sample(
                prompt=prompt,
                ok=image_result["ok"],
                status="ok" if image_result["ok"] else "error",
                latency_ms=image_result["latency_ms"],
                error=image_result.get("error"),
                model=model,
            )
            results.append(sample)

            # Generate video (only for first 2 prompts to keep runtime reasonable)
            if i < 2:
                video_result = await generate_video(
                    session=session,
                    base_url=base_url,
                    headers=headers,
                    prompt=prompt,
                    model=model,
                    max_attempts=10,
                    poll_interval=5.0,
                )

                video_sample = Sample(
                    prompt=f"[video] {prompt}",
                    ok=video_result["ok"],
                    status="ok" if video_result["ok"] else video_result.get("status", "error"),
                    latency_ms=video_result["latency_ms"],
                    error=video_result.get("error"),
                    model=model,
                )
                results.append(video_sample)

    return results
