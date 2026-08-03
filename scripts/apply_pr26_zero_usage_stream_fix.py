"""One-shot fix for zero-usage terminal stream settlement."""
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


api_dispatcher = "aigateway-api/src/aigateway_api/dispatcher.py"
replace_once(
    api_dispatcher,
    '''def _upstream_stream_error(usage: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": "upstream_stream_error",
            "message": "The upstream model stream terminated before completion.",
            "type": "upstream_error",
        }
    }
    if usage:
        payload["usage"] = dict(usage)
    return payload
''',
    '''def _upstream_stream_error(usage: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if isinstance(usage, dict):
        for key in normalized_usage:
            normalized_usage[key] = max(0, int(usage.get(key, 0) or 0))
    return {
        "error": {
            "code": "upstream_stream_error",
            "message": "The upstream model stream terminated before completion.",
            "type": "upstream_error",
        },
        # A non-empty zero usage object lets Core persist a failure ledger row.
        # Core separately releases the reservation when total_tokens remains 0.
        "usage": normalized_usage,
    }
''',
)
replace_once(
    api_dispatcher,
    '''                    if last_usage and not isinstance(chunk.get("usage"), dict):
                        chunk = dict(chunk)
                        chunk["usage"] = dict(last_usage)
''',
    '''                    if not isinstance(chunk.get("usage"), dict) or not chunk.get("usage"):
                        chunk = dict(chunk)
                        chunk["usage"] = _upstream_stream_error(last_usage)["usage"]
''',
)

core_dispatcher = "aigateway-core/src/aigateway_core/dispatch/dispatcher.py"
replace_once(
    core_dispatcher,
    '''        if not usage:
            await self._release_quota_reservation(request, key_store, key_hash)
            return

        # metrics — 优先用 bridge 末块真实成本(与非流式路径一致),否则内置表估算
''',
    '''        terminal_stream_failure = bool(
            getattr(request.state, "_upstream_stream_failed", False)
        )
        if not usage and not terminal_stream_failure:
            await self._release_quota_reservation(request, key_store, key_hash)
            return

        # metrics — 优先用 bridge 末块真实成本(与非流式路径一致),否则内置表估算
''',
)
replace_once(
    core_dispatcher,
    '''        # 配额扣减（修正点：原流式不扣）
        if key_hash and key_store and tt > 0:
''',
    '''        # A terminal provider failure with no trustworthy usage must still
        # produce a zero-cost ledger row, but its optimistic reservation cannot
        # remain charged indefinitely.
        if terminal_stream_failure and tt <= 0:
            await self._release_quota_reservation(request, key_store, key_hash)

        # 配额扣减（修正点：原流式不扣）
        if key_hash and key_store and tt > 0:
''',
)

test_path = Path("tests/unit/test_merge_readiness_followup.py")
test_text = test_path.read_text(encoding="utf-8")
if "test_provider_failure_without_usage_records_ledger_and_releases_quota" not in test_text:
    test_text += '''

@pytest.mark.asyncio
async def test_provider_failure_without_usage_records_ledger_and_releases_quota() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            trace_id="trace-zero",
            request_id="request-zero",
            _lua_quota_reserved=True,
            _lua_reserved_tokens=10,
            _lua_reserved_cost=0.0,
        )
    )
    key_store = _RecordingKeyStore()
    key_proxy = _RequestKeyStoreProxy(key_store, request)

    async def provider():
        if False:
            yield {}
        raise RuntimeError("provider failed before first token")

    dispatcher = RequestDispatcher({})
    settled = dispatcher._wrap_stream_full(
        _inspect_upstream_stream(provider(), request),
        None,
        None,
        key_proxy,
        request,
        "test-model",
        "user",
        "key-hash",
        None,
        None,
        time.time(),
        "group",
        "understanding",
        "group",
        "group",
    )
    chunks = [chunk async for chunk in SSEGenerator(settled).generate()]

    assert any("upstream_stream_error" in chunk for chunk in chunks)
    assert all("[DONE]" not in chunk for chunk in chunks)
    assert key_store.ledger_statuses == ["upstream_stream_error"]
    assert key_store.increment_calls == []
    assert len(key_store.release_calls) == 1
    assert request.state._lua_quota_reserved is False
'''
    test_path.write_text(test_text, encoding="utf-8")
