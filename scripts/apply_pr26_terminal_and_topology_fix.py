"""One-shot finalization-order and managed-topology fixes for PR #26."""
from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Buffer terminal SSE events until inner settlement has completed.
# ---------------------------------------------------------------------------
sse_path = Path("aigateway-core/src/aigateway_core/route/streaming/sse.py")
sse_text = sse_path.read_text(encoding="utf-8")
method_start = sse_text.index("    async def generate(self) -> AsyncIterator[str]:")
new_method = '''    async def generate(self) -> AsyncIterator[str]:
        """Generate SSE data and emit a terminal outcome after producer cleanup.

        Normal chunks are streamed immediately. An error chunk is buffered while
        the inner producer is drained so quota, request-log and ledger settlement
        can finish before the client receives the terminal event. No data after
        the first error is exposed and a failed stream never emits ``[DONE]``.
        """
        emit_done = True
        terminal_error_event: str | None = None
        try:
            async for chunk in self.completion_gen:
                if terminal_error_event is not None:
                    # Drain the producer without exposing post-terminal data.
                    continue
                event = "data: " + json.dumps(chunk, ensure_ascii=False) + "\\n\\n"
                if isinstance(chunk, dict) and isinstance(chunk.get("error"), dict):
                    terminal_error_event = event
                    emit_done = False
                    continue
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            emit_done = False
            raise
        except Exception as exc:
            emit_done = False
            logger.error("SSE stream generation error: %s", type(exc).__name__)
            terminal_error_event = "data: " + json.dumps(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "The response stream terminated unexpectedly.",
                    }
                },
                ensure_ascii=False,
            ) + "\\n\\n"
        finally:
            close = getattr(self.completion_gen, "aclose", None)
            if callable(close):
                try:
                    await close()
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed to close upstream SSE generator: %s",
                        type(exc).__name__,
                    )

        if terminal_error_event is not None:
            yield terminal_error_event
        elif emit_done:
            yield "data: [DONE]\\n\\n"
'''
sse_path.write_text(sse_text[:method_start] + new_method, encoding="utf-8")

api_dispatcher = Path("aigateway-api/src/aigateway_api/dispatcher.py")
api_text = api_dispatcher.read_text(encoding="utf-8")
func_start = api_text.index("async def _guard_sse_output(")
func_end = api_text.index("\n\nasync def _call_llm_nonstream_with_guard(", func_start)
new_guard = '''async def _guard_sse_output(
    iterator: AsyncIterator[str | bytes],
    *,
    max_tokens: int | None,
    request: Any | None = None,
    metrics_collector: Any = None,
    started_at: float | None = None,
) -> AsyncIterator[str | bytes]:
    """Emit one terminal SSE outcome only after inner cleanup and metrics."""
    saw_content = False
    terminal_reasons: list[str] = []
    saw_error = False
    completion_tokens = 0
    done_chunk: str | bytes | None = None
    error_chunk: str | bytes | None = None
    emitted_bytes = False

    async for raw in iterator:
        emitted_bytes = emitted_bytes or isinstance(raw, bytes)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if text.strip() == "data: [DONE]":
            done_chunk = raw
            continue
        if saw_error:
            # Defensive drain: never expose data after a terminal error.
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

    # Terminal events are deliberately emitted after the iterator has completed,
    # so a client disconnect after receiving them cannot interrupt settlement.
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
api_dispatcher.write_text(api_text[:func_start] + new_guard + api_text[func_end:], encoding="utf-8")


# ---------------------------------------------------------------------------
# Persist whether ComfyUI is a scheduler-managed local container.
# ---------------------------------------------------------------------------
render_path = "scripts/render-deployment-config.py"
replace_once(
    render_path,
    '''    embedding_mode: str,
    comfyui_url: str,
''',
    '''    embedding_mode: str,
    comfyui_mode: str = "remote",
    comfyui_url: str,
''',
)
replace_once(
    render_path,
    '''    if edition not in EDITIONS:
        raise ValueError(f"unsupported edition: {edition}")
''',
    '''    if edition not in EDITIONS:
        raise ValueError(f"unsupported edition: {edition}")
    if comfyui_mode not in {"container", "native", "remote"}:
        raise ValueError(f"unsupported comfyui mode: {comfyui_mode}")
''',
)
replace_once(
    render_path,
    '''    comfy["server_url"] = comfyui_url.rstrip("/")
    comfy["required"] = True
''',
    '''    comfy["server_url"] = comfyui_url.rstrip("/")
    comfy["required"] = True
    comfy["scheduler_managed"] = bool(
        studio and accelerator == "cuda" and comfyui_mode == "container"
    )
''',
)
replace_once(
    render_path,
    '''        "embedding_mode": embedding_mode,
        "comfyui_enabled": studio,
''',
    '''        "embedding_mode": embedding_mode,
        "comfyui_mode": comfyui_mode,
        "comfyui_enabled": studio,
''',
)
replace_once(
    render_path,
    '''    parser.add_argument("--comfyui-url", required=True)
''',
    '''    parser.add_argument(
        "--comfyui-mode",
        choices=("container", "native", "remote"),
        default="remote",
    )
    parser.add_argument("--comfyui-url", required=True)
''',
)
replace_once(
    render_path,
    '''        embedding_mode=args.embedding_mode,
        comfyui_url=args.comfyui_url,
''',
    '''        embedding_mode=args.embedding_mode,
        comfyui_mode=args.comfyui_mode,
        comfyui_url=args.comfyui_url,
''',
)

replace_once(
    "scripts/quickstart.sh",
    '''  --embedding-mode "$embedding_mode"
  --comfyui-url "${comfyui_url:-http://comfyui.invalid}"
''',
    '''  --embedding-mode "$embedding_mode"
  --comfyui-mode "$comfyui_mode"
  --comfyui-url "${comfyui_url:-http://comfyui.invalid}"
''',
)

replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/registration.py",
    '''        required=comfyui_dict.get("required", ComfyUIConfig.required),
        workflow_version=comfyui_dict.get(
''',
    '''        required=comfyui_dict.get("required", ComfyUIConfig.required),
        scheduler_managed=comfyui_dict.get(
            "scheduler_managed", ComfyUIConfig.scheduler_managed
        ),
        workflow_version=comfyui_dict.get(
''',
)

replace_once(
    "aigateway-api/src/aigateway_api/gpu_routes.py",
    '''    scheduler_config = (
        manager.get("gpu_scheduler", {}) if manager is not None else {}
    )
''',
    '''    scheduler_config = (
        manager.get("gpu_scheduler", {}) if manager is not None else {}
    )
    comfy_config = _comfy_config(request)
''',
)
replace_once(
    "aigateway-api/src/aigateway_api/gpu_routes.py",
    '''    pool_expected = bool(scheduler.get("enabled")) and (
        shared_gpu or bool(scheduler.get("workers"))
    )
''',
    '''    scheduler_managed = bool(comfy_config.get("scheduler_managed", False))
    pool_expected = bool(scheduler.get("enabled")) and (
        scheduler_managed or shared_gpu or bool(scheduler.get("workers"))
    )
''',
)
replace_once(
    "aigateway-api/src/aigateway_api/gpu_routes.py",
    '''            "shared_gpu": shared_gpu,
            "pool_expected": pool_expected,
''',
    '''            "shared_gpu": shared_gpu,
            "scheduler_managed": scheduler_managed,
            "pool_expected": pool_expected,
''',
)

for config_path, comment in (
    ("config.yaml", ""),
    ("config.yaml.template", " # 仅本地 CUDA 容器 Worker 设为 true；远程端点不得占用本机 GPU 租约"),
):
    replace_once(
        config_path,
        "      required: true" + ("" if config_path == "config.yaml" else "         # 不可用时 fail-closed，不静默改走外部媒体 API") + "\n",
        "      required: true" + ("" if config_path == "config.yaml" else "         # 不可用时 fail-closed，不静默改走外部媒体 API") + "\n"
        + "      scheduler_managed: false" + comment + "\n",
    )


# ---------------------------------------------------------------------------
# Regression coverage.
# ---------------------------------------------------------------------------
followup = Path("tests/unit/test_merge_readiness_followup.py")
followup_text = followup.read_text(encoding="utf-8")
if "test_terminal_error_is_emitted_only_after_producer_cleanup" not in followup_text:
    followup_text += '''

@pytest.mark.asyncio
async def test_terminal_error_is_emitted_only_after_producer_cleanup() -> None:
    cleanup_ran = False

    async def producer():
        nonlocal cleanup_ran
        try:
            yield {"error": {"code": "upstream_error", "message": "failed"}}
        finally:
            cleanup_ran = True

    stream = SSEGenerator(producer()).generate()
    first = await anext(stream)

    assert cleanup_ran is True
    assert "upstream_error" in first
    assert "[DONE]" not in first
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_outer_sse_guard_records_metrics_before_terminal_error() -> None:
    cleanup_ran = False
    metrics = RecordingMetrics()

    async def upstream():
        nonlocal cleanup_ran
        try:
            yield 'data: {"error":{"code":"upstream_error","message":"failed"}}\\n\\n'
        finally:
            cleanup_ran = True

    stream = _guard_sse_output(
        upstream(),
        max_tokens=64,
        metrics_collector=metrics,
        started_at=time.monotonic(),
    )
    first = await anext(stream)

    assert cleanup_ran is True
    assert metrics.requests == [("POST", "/v1/chat/completions", "502")]
    assert "upstream_error" in first


def test_registration_propagates_scheduler_managed_flag(tmp_path: Path) -> None:
    from aigateway_core.pipelines.generation.registration import (
        register_generation_optimization_plugins,
    )

    class ConfigManager:
        def get(self, key: str, default: Any = None) -> Any:
            values = {
                "generation_optimization": {
                    "enabled": True,
                    "draft_workflow": {
                        "enabled": True,
                        "store_dir": str(tmp_path),
                        "comfyui": {
                            "server_url": "http://comfyui:8188",
                            "scheduler_managed": True,
                        },
                    },
                },
                "providers": {},
                "auth": {},
            }
            return values.get(key, default)

    registry = PluginRegistry()
    register_generation_optimization_plugins(
        registry=registry,
        config_manager=ConfigManager(),
    )
    registration = registry.get("draft_generator")
    assert registration is not None
    strategy = registration.config["strategy"]
    assert strategy._comfyui_config.scheduler_managed is True
'''
    followup.write_text(followup_text, encoding="utf-8")

resource_test = Path("tests/unit/test_gpu_resource_policy.py")
resource_text = resource_test.read_text(encoding="utf-8")
resource_text = resource_text.replace(
    '''        embedding_mode="container",
        comfyui_url="http://comfyui:8188",
''',
    '''        embedding_mode="container",
        comfyui_mode="container",
        comfyui_url="http://comfyui:8188",
''',
    1,
)
resource_text = resource_text.replace(
    '''    assert config["deployment"]["shared_gpu"] is True
''',
    '''    assert config["deployment"]["shared_gpu"] is True
    assert config["deployment"]["comfyui_mode"] == "container"
    assert config["generation_optimization"]["draft_workflow"]["comfyui"]["scheduler_managed"] is True
''',
    1,
)
if "test_remote_comfyui_is_not_scheduler_managed" not in resource_text:
    insert_at = resource_text.index("\n\ndef test_nvidia_smi_status_selects_visible_device")
    remote_test = '''


def test_remote_comfyui_is_not_scheduler_managed() -> None:
    renderer = load_renderer()
    source = Path(__file__).resolve().parents[2] / "config.yaml.template"
    config = renderer.render(
        source,
        edition="full",
        accelerator="cuda",
        embedding_mode="container",
        comfyui_mode="remote",
        comfyui_url="https://remote-comfy.example",
        embedding_url="",
        monitoring=False,
        shared_gpu=False,
    )
    assert config["deployment"]["comfyui_mode"] == "remote"
    assert config["generation_optimization"]["draft_workflow"]["comfyui"]["scheduler_managed"] is False
'''
    resource_text = resource_text[:insert_at] + remote_test + resource_text[insert_at:]
resource_test.write_text(resource_text, encoding="utf-8")
