"""Settle cancelled streams as 499 without skipping quota or ledger cleanup."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


api_path = Path("aigateway-api/src/aigateway_api/dispatcher.py")
api_text = api_path.read_text(encoding="utf-8")
api_text = api_text.replace(
    '''def _is_upstream_stream_failed(request: Any) -> bool:
    return bool(getattr(_request_state(request), "_upstream_stream_failed", False))


def _terminal_status_code(request: Any) -> int | None:
''',
    '''def _is_upstream_stream_failed(request: Any) -> bool:
    return bool(getattr(_request_state(request), "_upstream_stream_failed", False))


def _mark_client_disconnected(request: Any) -> None:
    _request_state(request)._client_disconnected = True


def _is_client_disconnected(request: Any) -> bool:
    return bool(getattr(_request_state(request), "_client_disconnected", False))


def _terminal_status_code(request: Any) -> int | None:
''',
    1,
)
api_text = api_text.replace(
    '''    if _is_upstream_stream_failed(request):
        return 502
    return None
''',
    '''    if _is_upstream_stream_failed(request):
        return 502
    if _is_client_disconnected(request):
        return 499
    return None
''',
    1,
)
api_text = api_text.replace(
    '''    if _is_upstream_stream_failed(request):
        return "upstream_stream_error"
    return None
''',
    '''    if _is_upstream_stream_failed(request):
        return "upstream_stream_error"
    if _is_client_disconnected(request):
        return "client_disconnected"
    return None
''',
    1,
)

func_start = api_text.index("async def _guard_sse_output(")
func_end = api_text.index("\n\nasync def _call_llm_nonstream_with_guard(", func_start)
old_func = api_text[func_start:func_end]
# Preserve the existing parsing body, but make cancellation an explicit final
# outcome and close the wrapped iterator before propagating cancellation.
new_func = '''async def _guard_sse_output(
    iterator: AsyncIterator[str | bytes],
    *,
    max_tokens: int | None,
    request: Any | None = None,
    metrics_collector: Any = None,
    started_at: float | None = None,
) -> AsyncIterator[str | bytes]:
    """Emit one final SSE outcome and settle consumer cancellation as 499."""
    saw_content = False
    terminal_reasons: list[str] = []
    saw_error = False
    completion_tokens = 0
    done_chunk: str | bytes | None = None
    error_chunk: str | bytes | None = None
    emitted_bytes = False

    try:
        async for raw in iterator:
            emitted_bytes = emitted_bytes or isinstance(raw, bytes)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if text.strip() == "data: [DONE]":
                done_chunk = raw
                continue
            if saw_error:
                continue

            payload = _parse_sse_payload(raw)
            if payload is not None:
                if isinstance(payload.get("error"), dict):
                    saw_error = True
                    error_chunk = raw
                    continue
                usage = payload.get("usage") or {}
                if isinstance(usage, dict):
                    completion_tokens = max(
                        completion_tokens,
                        int(usage.get("completion_tokens", 0) or 0),
                    )
                for choice in payload.get("choices", []) or []:
                    if not isinstance(choice, dict):
                        continue
                    if _choice_has_output(choice):
                        saw_content = True
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        terminal_reasons.append(finish_reason)
            yield raw
    except (asyncio.CancelledError, GeneratorExit):
        if request is not None:
            _mark_client_disconnected(request)
        status_code = (
            _terminal_status_code(request)
            if request is not None
            else 499
        ) or 499
        if started_at is not None:
            _record_final_metrics(
                metrics_collector,
                status_code=status_code,
                started_at=started_at,
            )
        close = getattr(iterator, "aclose", None)
        if callable(close):
            try:
                await close()
            except (asyncio.CancelledError, GeneratorExit):
                pass
            except Exception as exc:
                logger.debug(
                    "Cancelled stream close failed: %s",
                    type(exc).__name__,
                )
        raise

    exhausted = bool(
        _is_output_budget_exhausted(request)
        if request is not None
        else False
    ) or bool(
        terminal_reasons
        and all(reason == "length" for reason in terminal_reasons)
        and not saw_content
    )
    if exhausted and request is not None:
        _mark_output_budget_exhausted(request, completion_tokens)

    terminal_status = _terminal_status_code(request) if request is not None else None
    status_code = terminal_status or (502 if saw_error else 200)
    if started_at is not None:
        _record_final_metrics(
            metrics_collector,
            status_code=status_code,
            started_at=started_at,
        )

    if saw_error:
        if error_chunk is not None:
            yield error_chunk
        return
    if exhausted:
        error_event = "data: " + json.dumps(
            _output_budget_error(max_tokens, completion_tokens),
            ensure_ascii=False,
        ) + "\\n\\n"
        use_bytes = isinstance(done_chunk, bytes) or emitted_bytes
        yield error_event.encode("utf-8") if use_bytes else error_event
        return
    if done_chunk is not None:
        yield done_chunk
'''
api_text = api_text[:func_start] + new_func + api_text[func_end:]
api_path.write_text(api_text, encoding="utf-8")

core_path = "aigateway-core/src/aigateway_core/dispatch/dispatcher.py"
replace_once(
    core_path,
    '''        except asyncio.CancelledError:
            # Client disconnected. Explicitly close the upstream generator so
''',
    '''        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected. Explicitly close the upstream generator so
''',
)
replace_once(
    core_path,
    '''            client_disconnected = True
            aclose = getattr(gen, "aclose", None)
''',
    '''            client_disconnected = True
            request.state._client_disconnected = True
            aclose = getattr(gen, "aclose", None)
''',
)
replace_once(
    core_path,
    '''        terminal_stream_failure = bool(
            getattr(request.state, "_upstream_stream_failed", False)
        )
        if not usage and not terminal_stream_failure:
''',
    '''        terminal_stream_failure = bool(
            getattr(request.state, "_upstream_stream_failed", False)
        )
        terminal_stream_outcome = terminal_stream_failure or bool(
            getattr(request.state, "_client_disconnected", False)
        )
        if not usage and not terminal_stream_outcome:
''',
)
replace_once(
    core_path,
    '''        if terminal_stream_failure and tt <= 0:
            await self._release_quota_reservation(request, key_store, key_hash)
''',
    '''        if terminal_stream_outcome and tt <= 0:
            await self._release_quota_reservation(request, key_store, key_hash)
''',
)

test_path = Path("tests/unit/test_merge_readiness_followup.py")
test_text = test_path.read_text(encoding="utf-8")
if "test_outer_stream_close_records_499_and_closes_upstream" not in test_text:
    test_text += '''

@pytest.mark.asyncio
async def test_outer_stream_close_records_499_and_closes_upstream() -> None:
    request = SimpleNamespace(state=SimpleNamespace())
    metrics = RecordingMetrics()
    upstream_closed = False

    async def upstream():
        nonlocal upstream_closed
        try:
            yield 'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\\n\\n'
            yield "data: [DONE]\\n\\n"
        finally:
            upstream_closed = True

    stream = _guard_sse_output(
        upstream(),
        max_tokens=64,
        request=request,
        metrics_collector=metrics,
        started_at=time.monotonic(),
    )
    assert "partial" in await anext(stream)
    await stream.aclose()

    assert upstream_closed is True
    assert request.state._client_disconnected is True
    assert metrics.requests == [("POST", "/v1/chat/completions", "499")]


@pytest.mark.asyncio
async def test_core_generator_close_releases_reservation_and_records_499(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            trace_id="trace-disconnect",
            request_id="request-disconnect",
            _lua_quota_reserved=True,
            _lua_reserved_tokens=10,
            _lua_reserved_cost=0.0,
        )
    )
    key_store = _RecordingKeyStore()
    key_proxy = _RequestKeyStoreProxy(key_store, request)
    logged_statuses: list[int] = []

    async def record_log(**kwargs: Any) -> None:
        logged_statuses.append(int(kwargs["status_code"]))

    monkeypatch.delattr(openai_compat, _LOG_ORIGINAL_ATTR, raising=False)
    monkeypatch.setattr(openai_compat, "_record_request_log", record_log)
    _install_request_log_guard()

    async def provider():
        yield {
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "partial"},
                    "finish_reason": None,
                }
            ],
        }
        yield {"choices": [], "usage": {}}

    dispatcher = RequestDispatcher({})
    settled = dispatcher._wrap_stream_full(
        provider(),
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
    first = await anext(settled)
    assert first["choices"][0]["delta"]["content"] == "partial"
    await settled.aclose()

    assert request.state._client_disconnected is True
    assert logged_statuses == [499]
    assert key_store.ledger_statuses == ["client_disconnected"]
    assert len(key_store.release_calls) == 1
    assert key_store.increment_calls == []
    assert request.state._lua_quota_reserved is False
'''
    test_path.write_text(test_text, encoding="utf-8")
