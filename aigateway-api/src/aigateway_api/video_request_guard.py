"""Compatibility helpers for the superseded API-level video request guard.

PR #47 moved reference-image validation into the core ``RequestDispatcher``.
This module intentionally does not wrap dispatcher execution; it only preserves
small helper contracts still used by request-lifecycle code and regression tests.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aigateway_core.pipelines.generation._common.image_reference import (
    latest_user_text,
    missing_required_image_reference,
)

from .generation_request_state import terminal_request_status

_PENDING_REQUEST_TTL_SECONDS = 300


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _generation_options(body: Any) -> Mapping[str, Any]:
    raw = _value(body, "generation_options", {})
    if isinstance(raw, Mapping):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        value = dump(exclude_none=True)
        return value if isinstance(value, Mapping) else {}
    return {}


def _reference_image_urls(body: Any) -> list[str]:
    messages = _value(body, "messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return []
    for message in reversed(messages):
        if _value(message, "role") != "user":
            continue
        content = _value(message, "content", [])
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            return []
        urls: list[str] = []
        for part in content:
            if _value(part, "type") != "image_url":
                continue
            image = _value(part, "image_url", None)
            url = _value(image, "url", image)
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
        return urls
    return []


def reference_image_required(body: Any) -> bool:
    """Evaluate PR #47's core fail-closed image-reference rule."""
    messages = _value(body, "messages", [])
    if not isinstance(messages, list):
        messages = list(messages) if isinstance(messages, Sequence) else []
    options = _generation_options(body)
    source_draft_id = options.get("source_draft_id")
    return missing_required_image_reference(
        pipeline_kind="generation:video",
        prompt_text=latest_user_text(messages),
        reference_image_urls=_reference_image_urls(body),
        source_draft_id=(
            source_draft_id.strip()
            if isinstance(source_draft_id, str)
            else None
        ),
    )


def _request_app(request: Any) -> Any | None:
    scope = getattr(request, "scope", None)
    if isinstance(scope, Mapping):
        app = scope.get("app")
        if app is not None:
            return app
    try:
        return request.app
    except (AttributeError, KeyError):
        return None


def _draft_strategy(request: Any) -> Any | None:
    app = _request_app(request)
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return None
    strategy = getattr(state, "draft_strategy", None)
    if strategy is None:
        strategy = getattr(state, "draft_generator_strategy", None)
    return strategy


def _request_identity(body: Any, request: Any) -> tuple[str, str, str | None, str | None]:
    request_state = getattr(request, "state", None)
    request_id = str(getattr(request_state, "request_id", "") or "")
    session_id = str(_value(body, "chat_session_id", "") or "")
    owner = getattr(request_state, "draft_owner", None)
    owner = owner if isinstance(owner, Mapping) else {}
    user_id = str(owner.get("user_id") or "") or None
    group_id = str(owner.get("group_id") or "") or None
    return request_id, session_id, user_id, group_id


async def _register_request_record(
    body: Any,
    request: Any,
    draft_id: str,
    *,
    overwrite_terminal: bool = False,
) -> None:
    """Persist an owner-scoped request marker without replacing a real draft."""
    strategy = _draft_strategy(request)
    register = getattr(strategy, "register_request_draft", None)
    if not callable(register):
        return

    request_id, session_id, user_id, group_id = _request_identity(body, request)
    if not request_id or not session_id or (not user_id and not group_id):
        return

    resolver = getattr(strategy, "resolve_request", None)
    if callable(resolver):
        existing_draft, record = await resolver(request_id)
        if existing_draft is not None:
            return
        marker = str(record.get("draft_id") or "") if isinstance(record, Mapping) else ""
        terminal = terminal_request_status(record)
        if marker and not terminal:
            return
        if terminal and not overwrite_terminal:
            return

    await register(
        request_id,
        draft_id,
        user_id=user_id,
        group_id=group_id,
        session_id=session_id,
        ttl_seconds=_PENDING_REQUEST_TTL_SECONDS,
    )


async def _mark_request_terminal(body: Any, request: Any, marker: str) -> None:
    """Replace only a pending request record with a terminal marker."""
    await _register_request_record(
        body,
        request,
        marker,
        overwrite_terminal=True,
    )


def install_video_request_guard() -> None:
    """Compatibility no-op; PR #47 performs validation inside core dispatch."""


__all__ = ["install_video_request_guard", "reference_image_required"]
