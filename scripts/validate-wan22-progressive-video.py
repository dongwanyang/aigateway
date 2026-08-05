#!/usr/bin/env python3
"""Live acceptance checks for the Wan2.2 progressive-video workflow.

The script is intentionally inert unless ``--execute`` is supplied because the
checks can submit and optionally confirm real GPU work.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

EXPECTED_FRAMES = {3: 25, 5: 41, 8: 65}


class AcceptanceError(RuntimeError):
    pass


def _data_url_bytes(value: str) -> bytes:
    if not value.startswith("data:") or "," not in value:
        raise AcceptanceError("response did not contain a data URL")
    metadata, encoded = value.split(",", 1)
    if ";base64" not in metadata:
        raise AcceptanceError("only base64 data URLs are supported")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AcceptanceError("invalid base64 data URL") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "unknown_error", response.text[:500]
    error = body.get("error") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(error, dict) and isinstance(detail, dict):
        error = detail.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "unknown_error"), str(
            error.get("message") or response.text[:500]
        )
    return "unknown_error", str(detail or response.text[:500])


def _require_status(
    response: httpx.Response,
    expected: int | set[int],
    context: str,
) -> None:
    allowed = {expected} if isinstance(expected, int) else expected
    if response.status_code in allowed:
        return
    code, message = _error_details(response)
    raise AcceptanceError(
        f"{context}: HTTP {response.status_code}, code={code}, message={message}"
    )


def _request_id(prefix: str) -> str:
    return f"wan22-{prefix}-{uuid.uuid4().hex}"


async def _poll_request(
    client: httpx.AsyncClient,
    request_id: str,
    session_id: str,
    *,
    timeout: float,
    terminal_status: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(
            f"/admin/generation/requests/{request_id}",
            params={"chat_session_id": session_id},
        )
        if response.status_code == 202:
            last = response.json()
            retry_ms = float(last.get("retry_after_ms", 250))
            await asyncio.sleep(max(0.1, retry_ms / 1000))
            continue
        _require_status(response, 200, "poll generation request")
        last = response.json()
        if terminal_status is None and last.get("draft_id"):
            return last
        if terminal_status is not None and last.get("status") == terminal_status:
            return last
        await asyncio.sleep(0.25)
    raise AcceptanceError(
        f"request {request_id} did not reach "
        f"{terminal_status or 'draft registration'} within {timeout}s; last={last}"
    )


async def _preview_bytes(
    client: httpx.AsyncClient,
    draft_id: str,
    *,
    timeout: float,
) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/admin/draft/{draft_id}/preview")
        if response.status_code == 202:
            await asyncio.sleep(0.25)
            continue
        _require_status(response, 200, "load draft preview")
        value = response.json().get("preview_data_url")
        if not isinstance(value, str):
            raise AcceptanceError("preview response omitted preview_data_url")
        return _data_url_bytes(value)
    raise AcceptanceError(f"preview {draft_id} did not become ready")


async def _result_bytes(client: httpx.AsyncClient, draft_id: str) -> bytes:
    response = await client.get(f"/admin/draft/{draft_id}/result")
    _require_status(response, 200, "load draft result")
    value = response.json().get("result_data_url")
    if not isinstance(value, str):
        raise AcceptanceError("result response omitted result_data_url")
    return _data_url_bytes(value)


def _video_duration_seconds(data: bytes) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="wan22-acceptance-") as directory:
        path = Path(directory) / "result.mp4"
        path.write_bytes(data)
        process = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            raise AcceptanceError(f"ffprobe failed: {process.stderr.strip()}")
        return float(process.stdout.strip())


async def _check_health(
    client: httpx.AsyncClient,
    expected_commit: str | None,
) -> None:
    response = await client.get("/health")
    _require_status(response, 200, "health")
    data = response.json().get("data", {})
    commit = str(data.get("commit_sha") or "")
    if not commit or commit == "unknown":
        raise AcceptanceError(
            "/health does not expose a deployed commit SHA; set AIGATEWAY_COMMIT_SHA"
        )
    if expected_commit and not commit.startswith(expected_commit):
        raise AcceptanceError(
            f"deployed commit {commit} does not match expected {expected_commit}"
        )
    print(f"[PASS] runtime identity: commit={commit}, image={data.get('image_version')}")


async def _check_missing_reference(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    response = await client.post(
        "/admin/console/chat/completions",
        headers={"X-Request-ID": _request_id("missing-ref")},
        json={
            "model": "auto",
            "stream": False,
            "chat_session_id": session_id,
            "messages": [{"role": "user", "content": "以此图片生成5秒视频"}],
            "generation_options": {
                "backend": "local",
                "duration_seconds": 5,
                "fps": 8,
            },
        },
    )
    _require_status(response, 400, "missing image reference")
    code, _ = _error_details(response)
    if code != "reference_image_required":
        raise AcceptanceError(f"expected reference_image_required, got {code}")
    print("[PASS] missing image reference fails closed")


async def _check_response_loss_and_cancel(
    client: httpx.AsyncClient,
    session_id: str,
    timeout: float,
    disconnect_after: float,
) -> None:
    request_id = _request_id("disconnect")
    request_task = asyncio.create_task(
        client.post(
            "/admin/console/chat/completions",
            headers={"X-Request-ID": request_id},
            json={
                "model": "auto",
                "stream": True,
                "chat_session_id": session_id,
                "messages": [
                    {
                        "role": "user",
                        "content": "生成一张纯黑色方形测试图片，画面中不要出现文字",
                    }
                ],
                "generation_options": {"backend": "local"},
            },
        ),
        name=f"response-loss-{request_id}",
    )
    await asyncio.sleep(disconnect_after)
    request_task.cancel()
    await asyncio.gather(request_task, return_exceptions=True)

    state = await _poll_request(
        client,
        request_id,
        session_id,
        timeout=timeout,
    )
    draft_id = str(state["draft_id"])
    cancel = await client.delete(
        f"/admin/generation/requests/{request_id}",
        params={"chat_session_id": session_id},
    )
    _require_status(cancel, {200, 202}, "cancel response-lost generation")
    cancelled = await _poll_request(
        client,
        request_id,
        session_id,
        timeout=timeout,
        terminal_status="cancelled",
    )
    if cancelled.get("draft_id") != draft_id:
        raise AcceptanceError("request recovery changed draft identity")
    print(f"[PASS] response-loss recovery and real cancellation: draft={draft_id}")


async def _create_source_video_draft(
    client: httpx.AsyncClient,
    *,
    source_draft_id: str,
    session_id: str,
    duration: int,
) -> tuple[str, str, str]:
    request_id = _request_id(f"source-{duration}s")
    response = await client.post(
        "/admin/console/chat/completions",
        headers={"X-Request-ID": request_id},
        json={
            "model": "auto",
            "stream": False,
            "chat_session_id": session_id,
            "messages": [
                {
                    "role": "user",
                    "content": "主体缓慢向镜头移动，保持场景一致",
                }
            ],
            "generation_options": {
                "backend": "local",
                "source_draft_id": source_draft_id,
                "duration_seconds": duration,
                "fps": 8,
            },
        },
    )
    _require_status(response, 200, f"create {duration}s source video draft")
    data = response.json().get("data", {})
    params = data.get("generation_params", {})
    draft_id = str(data.get("draft_id") or "")
    source_hash = str(params.get("source_image_sha256") or "")
    if not draft_id or not source_hash:
        raise AcceptanceError("source video response omitted draft_id/source hash")
    expected_frames = EXPECTED_FRAMES[duration]
    if int(params.get("frame_count") or 0) != expected_frames:
        raise AcceptanceError(
            f"{duration}s expected {expected_frames} frames, got {params.get('frame_count')}"
        )
    if params.get("source_draft_id") != source_draft_id:
        raise AcceptanceError("source_draft_id was not frozen in generation_params")
    if params.get("source_kind") != "draft_result":
        raise AcceptanceError("source_kind is not draft_result")
    return request_id, draft_id, source_hash


async def _check_source_durations(
    client: httpx.AsyncClient,
    source_draft_id: str,
    session_id: str,
    timeout: float,
    confirm_videos: bool,
) -> None:
    source_bytes = await _result_bytes(client, source_draft_id)
    source_hash = _sha256(source_bytes)
    for duration in EXPECTED_FRAMES:
        request_id, draft_id, frozen_hash = await _create_source_video_draft(
            client,
            source_draft_id=source_draft_id,
            session_id=session_id,
            duration=duration,
        )
        preview = await _preview_bytes(client, draft_id, timeout=timeout)
        if _sha256(preview) != source_hash or frozen_hash != source_hash:
            raise AcceptanceError(
                f"{duration}s source preview/hash differs from original result"
            )
        if confirm_videos:
            response = await client.post(f"/admin/draft/{draft_id}/confirm")
            _require_status(response, 200, f"confirm {duration}s video")
            video = await _result_bytes(client, draft_id)
            actual = _video_duration_seconds(video)
            if actual is not None and abs(actual - duration) > 1.25:
                raise AcceptanceError(f"{duration}s video duration is {actual:.2f}s")
            print(
                f"[PASS] source image {duration}s confirmed; "
                f"frames={EXPECTED_FRAMES[duration]}, actual={actual}"
            )
        else:
            response = await client.delete(
                f"/admin/generation/requests/{request_id}",
                params={"chat_session_id": session_id},
            )
            _require_status(response, 200, f"cleanup {duration}s source draft")
            print(
                f"[PASS] source image {duration}s draft: "
                f"SHA preserved, frames={EXPECTED_FRAMES[duration]}"
            )


async def _run(args: argparse.Namespace) -> None:
    if not args.execute:
        print("No requests sent. Re-run with --execute after reviewing the target URL.")
        print(
            "Checks: health identity, missing-reference fail-closed, "
            "response-loss recovery/cancel"
        )
        if args.source_draft_id:
            print("Additional checks: source image SHA and 3/5/8-second frame snapshots")
        if args.confirm_videos:
            print("WARNING: --confirm-videos will run three real Wan2.2 GPU jobs")
        return
    if not args.cookie:
        raise AcceptanceError("--cookie is required for authenticated admin endpoints")

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers={
            "Cookie": args.cookie,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=args.timeout,
        follow_redirects=True,
        verify=not args.insecure,
    ) as client:
        await _check_health(client, args.expected_commit)
        await _check_missing_reference(client, args.session_id)
        await _check_response_loss_and_cancel(
            client,
            args.session_id,
            args.timeout,
            args.disconnect_after,
        )
        if args.source_draft_id:
            await _check_source_durations(
                client,
                args.source_draft_id,
                args.session_id,
                args.timeout,
                args.confirm_videos,
            )
    print("All requested Wan2.2 live acceptance checks passed.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cookie", help="Raw authenticated Cookie header; never logged")
    parser.add_argument(
        "--session-id",
        default=f"wan22-acceptance-{uuid.uuid4().hex[:12]}",
    )
    parser.add_argument(
        "--source-draft-id",
        help="Confirmed/completed image draft owned by this session",
    )
    parser.add_argument("--expected-commit")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--disconnect-after", type=float, default=1.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-videos", action="store_true")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.confirm_videos and not args.source_draft_id:
        raise SystemExit("--confirm-videos requires --source-draft-id")
    try:
        asyncio.run(_run(args))
    except (AcceptanceError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
