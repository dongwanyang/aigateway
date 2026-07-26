"""Unit tests for aigateway_core.shared.tracing.TracingManager.

Covers:
- TracingManager.initialize() (success, ImportError, other exceptions, disabled)
- ensure_initialized() (no-op when already initialized)
- create_request_span() (disabled tracer, active tracer, custom trace_id)
- inject_trace_context / extract_trace_context (W3C parsing, fallback headers)
- get_trace_info()
- get_tracing_manager() (singleton, env var paths)
- create_plugin_span, set_span_attribute, add_span_event, mark_span_error
"""

from __future__ import annotations

import os
import sys
import uuid as _real_uuid
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Ensure the core package is importable regardless of cwd.
sys.path.insert(0, "aigateway-core/src")

_tracing_mod = None


def _get_mod():
    global _tracing_mod
    if _tracing_mod is None:
        import aigateway_core.shared.tracing as mod

        _tracing_mod = mod
    return _tracing_mod


def _reset_global_instance() -> None:
    """Force re-read of env vars by clearing the module-level singleton."""
    m = _get_mod()
    m._tracing_instance = None


# ---------------------------------------------------------------------------
# 1. TracingManager.__init__
# ---------------------------------------------------------------------------


class TestTracingManagerInit:
    def test_default_values(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        assert mgr.enabled is True
        assert mgr.service_name == "ai-gateway"
        assert mgr.sample_rate == 0.1
        assert mgr._tracer is None
        assert mgr._initialized is False

    def test_custom_values(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(enabled=False, service_name="test-svc", sample_rate=0.5)
        assert mgr.enabled is False
        assert mgr.service_name == "test-svc"
        assert mgr.sample_rate == 0.5

    def test_sample_rate_clamped_to_zero(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(sample_rate=-5.0)
        assert mgr.sample_rate == 0.0

    def test_sample_rate_clamped_to_one(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(sample_rate=99.0)
        assert mgr.sample_rate == 1.0


# ---------------------------------------------------------------------------
# 2. TracingManager.initialize() — success path
# ---------------------------------------------------------------------------


class TestInitializeSuccess:
    def test_initializes_with_all_components(self):
        """Verify initialize() wires up Resource, Provider, sampler, processor, tracer."""
        from aigateway_core.shared.tracing import TracingManager

        mock_resource = MagicMock()
        mock_provider = MagicMock()
        mock_tracer = MagicMock()
        mock_trace_mod = MagicMock()
        mock_trace_mod.set_tracer_provider = MagicMock()
        mock_trace_mod.get_tracer.return_value = mock_tracer

        # Replace the inner-import names via sys.modules / __dict__
        mod = _get_mod()
        orig_modules = {}
        otel_keys = [
            "opentelemetry",
            "opentelemetry.trace",
            "opentelemetry.sdk.resources",
            "opentelemetry.sdk.trace",
            "opentelemetry.sdk.trace.export",
            "opentelemetry.trace.sampling",
        ]
        for k in otel_keys:
            orig_modules[k] = sys.modules.pop(k, None)

        try:
            # Make 'from opentelemetry import trace' resolve to mock_trace_mod
            otel_top = MagicMock()
            otel_top.trace = mock_trace_mod
            sys.modules["opentelemetry"] = otel_top
            sys.modules["opentelemetry.trace"] = mock_trace_mod
            res_mod = MagicMock()
            mock_resource_cls = MagicMock()
            mock_resource_cls.create.return_value = mock_resource
            res_mod.Resource = mock_resource_cls
            sys.modules["opentelemetry.sdk.resources"] = res_mod
            sys.modules["opentelemetry.sdk.trace"] = MagicMock(
                TracerProvider=MagicMock(return_value=mock_provider)
            )
            sys.modules["opentelemetry.sdk.trace.export"] = MagicMock(
                BatchSpanProcessor=MagicMock(),
                ConsoleSpanExporter=MagicMock(),
            )
            sys.modules["opentelemetry.trace.sampling"] = MagicMock(
                ParentBasedTraceIdRatio=MagicMock()
            )

            mgr = TracingManager(service_name="my-svc", sample_rate=0.25)
            mgr.initialize()

            assert mock_resource_cls.create.called
            assert mock_provider.set_sampler.called
            assert mock_provider.add_span_processor.called
            mock_trace_mod.set_tracer_provider.assert_called_once_with(mock_provider)
            mock_trace_mod.get_tracer.assert_called_once()
            assert mgr._initialized is True
            assert mgr._tracer is not None
        finally:
            for k, v in orig_modules.items():
                if v is not None:
                    sys.modules[k] = v
                else:
                    sys.modules.pop(k, None)

    @patch("aigateway_core.shared.tracing.logger")
    def test_disabled_skip_initialization(self, mock_logger):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(enabled=False)
        mgr.initialize()

        assert mock_logger.info.called
        assert not mock_logger.warning.called
        assert not mock_logger.error.called

    @patch("aigateway_core.shared.tracing.logger")
    def test_already_initialized_returns_early(self, mock_logger):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        mgr._initialized = True
        mgr.initialize()

        assert mock_logger.warning.call_count == 0
        assert mock_logger.error.call_count == 0

    @patch("aigateway_core.shared.tracing.logger")
    def test_import_error_handled_gracefully(self, mock_logger):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        with patch.dict("sys.modules", {"opentelemetry": None}):
            mgr.initialize()

        assert mgr._tracer is None
        mock_logger.warning.assert_called()
        warning_msg = str(mock_logger.warning.call_args)
        assert "opentelemetry-sdk" in warning_msg or "未安装" in warning_msg

    @patch("aigateway_core.shared.tracing.logger")
    def test_generic_exception_during_init(self, mock_logger):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()

        def _raise(*_args, **_kwargs):
            raise RuntimeError("boom")

        mod = _get_mod()
        orig_modules = {}
        otel_keys = [
            "opentelemetry",
            "opentelemetry.trace",
            "opentelemetry.sdk.resources",
            "opentelemetry.sdk.trace",
            "opentelemetry.sdk.trace.export",
            "opentelemetry.trace.sampling",
        ]
        for k in otel_keys:
            orig_modules[k] = sys.modules.pop(k, None)

        try:
            # Build a self-referential mock so that
            #   from opentelemetry import trace  => mock_trace_mod
            #   from opentelemetry.trace import ... => also mock_trace_mod
            mock_trace_mod = MagicMock()
            mock_trace_mod.set_tracer_provider.side_effect = _raise
            mock_trace_mod.trace = mock_trace_mod  # 'from opentelemetry import trace'

            sys.modules["opentelemetry"] = mock_trace_mod
            sys.modules["opentelemetry.trace"] = mock_trace_mod
            sys.modules["opentelemetry.sdk.resources"] = MagicMock()
            sys.modules["opentelemetry.sdk.trace"] = MagicMock()
            sys.modules["opentelemetry.sdk.trace.export"] = MagicMock()
            sys.modules["opentelemetry.trace.sampling"] = MagicMock()

            mgr.initialize()
        finally:
            for k, v in orig_modules.items():
                if v is not None:
                    sys.modules[k] = v
                else:
                    sys.modules.pop(k, None)

        # The except block sets _tracer = None on any Exception
        assert mgr._tracer is None
        mock_logger.error.assert_called_once()
        assert "boom" in str(mock_logger.error.call_args)


# ---------------------------------------------------------------------------
# 3. ensure_initialized()
# ---------------------------------------------------------------------------


class TestEnsureInitialized:
    @patch("aigateway_core.shared.tracing.TracingManager.initialize")
    def test_calls_initialize_when_not_initialized(self, mock_init):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        mgr.ensure_initialized()
        mock_init.assert_called_once()

    @patch("aigateway_core.shared.tracing.TracingManager.initialize")
    def test_noop_when_already_initialized(self, mock_init):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        mgr._initialized = True
        mgr.ensure_initialized()
        mock_init.assert_not_called()


# ---------------------------------------------------------------------------
# 4. create_request_span()
# ---------------------------------------------------------------------------


class TestCreateRequestSpan:
    def test_returns_empty_dict_when_disabled(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(enabled=False)
        result = mgr.create_request_span("req-1")
        assert result == {}

    def test_returns_empty_dict_when_no_tracer(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        mgr._tracer = None
        mgr._initialized = True
        result = mgr.create_request_span("req-1")
        assert result == {}

    def test_active_tracer_returns_context(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_time_val = 1700000000.0

        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.span_id = 0xABCDEF1234567890
        mock_span.context = mock_context

        mock_tracer = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_span)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_cm

        mgr = TracingManager()
        mgr._tracer = mock_tracer
        mgr._initialized = True

        mod = _get_mod()
        orig_span_kind = getattr(mod, "SpanKind", None)
        orig_time = getattr(mod, "time", None)
        orig_uuid = getattr(mod, "uuid", None)
        try:
            mod.SpanKind = MagicMock(SERVER="SERVER")
            mod.time = MagicMock(time=MagicMock(return_value=mock_time_val))

            # Fake uuid module in sys.modules so local 'import uuid' picks it up
            fake_uuid_mod = MagicMock()
            fake_uuid_mod.uuid4.return_value.hex = "generated-uuid-hex"
            saved = sys.modules.get("uuid")
            sys.modules["uuid"] = fake_uuid_mod

            result = mgr.create_request_span("req-test", operation="custom_op")
        finally:
            if orig_span_kind is not None:
                mod.SpanKind = orig_span_kind
            elif hasattr(mod, "SpanKind"):
                del mod.SpanKind
            if orig_time is not None:
                mod.time = orig_time
            elif hasattr(mod, "time"):
                del mod.time
            if orig_uuid is not None:
                mod.uuid = orig_uuid
            elif hasattr(mod, "uuid"):
                del mod.uuid
            if saved is not None:
                sys.modules["uuid"] = saved
            else:
                sys.modules.pop("uuid", None)

        assert result["trace_id"] == "generated-uuid-hex"
        assert result["span_id"] == "abcdef1234567890"
        assert result["started_at"] == mock_time_val
        assert result["span"] is mock_span

        mock_tracer.start_as_current_span.assert_called_once()
        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        assert call_kwargs["name"] == "custom_op.req-test"

    def test_uses_provided_trace_id(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.span_id = 0x1111111111111111
        mock_span.context = mock_context

        mock_tracer = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_span)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_cm

        mgr = TracingManager()
        mgr._tracer = mock_tracer
        mgr._initialized = True

        mod = _get_mod()
        orig_span_kind = getattr(mod, "SpanKind", None)
        orig_uuid = getattr(mod, "uuid", None)
        try:
            mod.SpanKind = MagicMock(SERVER="SERVER")

            fake_uuid_mod = MagicMock()
            saved = sys.modules.get("uuid")
            sys.modules["uuid"] = fake_uuid_mod

            result = mgr.create_request_span("req-1", trace_id="my-custom-trace-id")
        finally:
            if orig_span_kind is not None:
                mod.SpanKind = orig_span_kind
            elif hasattr(mod, "SpanKind"):
                del mod.SpanKind
            if orig_uuid is not None:
                mod.uuid = orig_uuid
            elif hasattr(mod, "uuid"):
                del mod.uuid
            if saved is not None:
                sys.modules["uuid"] = saved
            else:
                sys.modules.pop("uuid", None)

        assert result["trace_id"] == "my-custom-trace-id"
        fake_uuid_mod.uuid4.assert_not_called()

    def test_sets_span_attributes(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.span_id = 0xAAAA
        mock_span.context = mock_context

        mock_tracer = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_span)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_cm

        mgr = TracingManager(service_name="svc-x")
        mgr._tracer = mock_tracer
        mgr._initialized = True

        mod = _get_mod()
        orig_span_kind = getattr(mod, "SpanKind", None)
        orig_uuid = getattr(mod, "uuid", None)
        try:
            mod.SpanKind = MagicMock(SERVER="SERVER")

            fake_uuid_mod = MagicMock()
            fake_uuid_mod.uuid4.return_value.hex = "tid123"
            saved = sys.modules.get("uuid")
            sys.modules["uuid"] = fake_uuid_mod

            mgr.create_request_span("req-abc")
        finally:
            if orig_span_kind is not None:
                mod.SpanKind = orig_span_kind
            elif hasattr(mod, "SpanKind"):
                del mod.SpanKind
            if orig_uuid is not None:
                mod.uuid = orig_uuid
            elif hasattr(mod, "uuid"):
                del mod.uuid
            if saved is not None:
                sys.modules["uuid"] = saved
            else:
                sys.modules.pop("uuid", None)

        calls = [c for c in mock_span.set_attribute.call_args_list]
        attrs = {c[0][0]: c[0][1] for c in calls}
        assert attrs["request.id"] == "req-abc"
        assert attrs["trace.id"] == "tid123"
        assert attrs["service.name"] == "svc-x"


# ---------------------------------------------------------------------------
# 5. create_plugin_span()
# ---------------------------------------------------------------------------


class TestCreatePluginSpan:
    def test_returns_empty_when_no_span_context(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        result = mgr.create_plugin_span(None, "plugin-a", "req-1")
        assert result == {}

    def test_returns_empty_when_disabled(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(enabled=False)
        result = mgr.create_plugin_span({"trace_id": "t1"}, "plugin-a", "req-1")
        assert result == {}

    def test_creates_plugin_span_dict(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        mgr._initialized = True

        ctx = {"trace_id": "trace-abc"}
        result = mgr.create_plugin_span(ctx, "rag_retriever", "req-1")

        assert result["plugin_name"] == "rag_retriever"
        assert result["attributes"]["plugin.name"] == "rag_retriever"
        assert result["attributes"]["request.id"] == "req-1"
        assert result["attributes"]["trace.id"] == "trace-abc"
        assert "started_at" in result


# ---------------------------------------------------------------------------
# 6. inject_trace_context()
# ---------------------------------------------------------------------------


class TestInjectTraceContext:
    def test_injects_w3c_traceparent_and_x_headers(self):
        from aigateway_core.shared.tracing import TracingManager

        headers: Dict[str, str] = {}
        TracingManager.inject_trace_context(headers, "aaaabbbbccccdddd", "1111222233334444")

        assert headers == {
            "traceparent": "00-aaaabbbbccccdddd-1111222233334444-01",
            "X-Trace-ID": "aaaabbbbccccdddd",
            "X-Span-ID": "1111222233334444",
        }

    def test_overwrites_existing_headers(self):
        from aigateway_core.shared.tracing import TracingManager

        headers = {"traceparent": "old", "X-Trace-ID": "old", "X-Span-ID": "old"}
        TracingManager.inject_trace_context(headers, "new-tid", "new-sid")

        assert headers["traceparent"] == "00-new-tid-new-sid-01"
        assert headers["X-Trace-ID"] == "new-tid"
        assert headers["X-Span-ID"] == "new-sid"


# ---------------------------------------------------------------------------
# 7. extract_trace_context()
# ---------------------------------------------------------------------------


class TestExtractTraceContext:
    def test_parses_valid_traceparent(self):
        from aigateway_core.shared.tracing import TracingManager

        headers = {"traceparent": "00-abc123-def456-01"}
        result = TracingManager.extract_trace_context(headers)
        assert result == {"trace_id": "abc123", "span_id": "def456"}

    def test_prefers_traceparent_over_x_headers(self):
        from aigateway_core.shared.tracing import TracingManager

        # W3C format: version-traceId-spanId-flags
        # "00-from-parent-span-from-parent" split by '-' =>
        # ["00","from","parent","span","from","parent"]
        # parts[1]="from", parts[2]="parent"
        headers = {
            "traceparent": "00-from-parent-span-from-parent",
            "X-Trace-ID": "from-x-header",
            "X-Span-ID": "from-x-span",
        }
        result = TracingManager.extract_trace_context(headers)
        assert result["trace_id"] == "from"
        assert result["span_id"] == "parent"

    def test_fallback_to_x_headers_without_traceparent(self):
        from aigateway_core.shared.tracing import TracingManager

        headers = {"X-Trace-ID": "x-trace", "X-Span-ID": "x-span"}
        result = TracingManager.extract_trace_context(headers)
        assert result == {"trace_id": "x-trace", "span_id": "x-span"}

    def test_empty_traceparent_ignored(self):
        from aigateway_core.shared.tracing import TracingManager

        headers = {"traceparent": ""}
        result = TracingManager.extract_trace_context(headers)
        assert result == {"trace_id": "", "span_id": ""}

    def test_invalid_traceparent_format_short_circuits(self):
        from aigateway_core.shared.tracing import TracingManager

        # Only one dash-separated part => len(parts) < 3
        headers = {"traceparent": "invalid"}
        result = TracingManager.extract_trace_context(headers)
        assert result == {"trace_id": "", "span_id": ""}

    def test_traceparent_with_extra_parts(self):
        from aigateway_core.shared.tracing import TracingManager

        headers = {"traceparent": "00-tid-sid-01-extra"}
        result = TracingManager.extract_trace_context(headers)
        assert result == {"trace_id": "tid", "span_id": "sid"}

    def test_no_headers_returns_empty(self):
        from aigateway_core.shared.tracing import TracingManager

        result = TracingManager.extract_trace_context({})
        assert result == {"trace_id": "", "span_id": ""}


# ---------------------------------------------------------------------------
# 8. get_trace_info()
# ---------------------------------------------------------------------------


class TestGetTraceInfo:
    def test_returns_config_summary(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager(enabled=True, service_name="info-svc", sample_rate=0.75)
        info = mgr.get_trace_info()

        assert info == {
            "enabled": True,
            "service_name": "info-svc",
            "sample_rate": 0.75,
            "initialized": False,
        }

    def test_initialized_flag_reflects_state(self):
        from aigateway_core.shared.tracing import TracingManager

        mgr = TracingManager()
        assert mgr.get_trace_info()["initialized"] is False

        mgr._initialized = True
        assert mgr.get_trace_info()["initialized"] is True


# ---------------------------------------------------------------------------
# 9. get_tracing_manager() — singleton + env vars
# ---------------------------------------------------------------------------


class TestGetTracingManagerSingleton:
    def setup_method(self, method):
        _reset_global_instance()

    def teardown_method(self, method):
        _reset_global_instance()

    def _clear_env(self):
        for key in (
            "AI_GATEWAY_OPENTELEMETRY_ENABLED",
            "AI_GATEWAY_OTEL_SERVICE_NAME",
            "AI_GATEWAY_OTEL_SAMPLE_RATE",
        ):
            os.environ.pop(key, None)

    def test_returns_same_instance(self):
        from aigateway_core.shared.tracing import get_tracing_manager

        m1 = get_tracing_manager()
        m2 = get_tracing_manager()
        assert m1 is m2

    def test_default_enabled_true(self):
        self._clear_env()
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.enabled is True
        assert mgr.service_name == "ai-gateway"
        assert mgr.sample_rate == 0.1

    def test_disabled_via_env_var_false(self):
        self._clear_env()
        from aigateway_core.shared.tracing import get_tracing_manager

        os.environ["AI_GATEWAY_OPENTELEMETRY_ENABLED"] = "false"
        mgr = get_tracing_manager()
        assert mgr.enabled is False

    @pytest.mark.parametrize("value", ["1", "yes", "TRUE", "True", "YES"])
    def test_enabled_values(self, value):
        self._clear_env()
        os.environ["AI_GATEWAY_OPENTELEMETRY_ENABLED"] = value
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.enabled is True

    @pytest.mark.parametrize("value", ["0", "no", "False", "", "disabled"])
    def test_disabled_values(self, value):
        self._clear_env()
        os.environ["AI_GATEWAY_OPENTELEMETRY_ENABLED"] = value
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.enabled is False

    def test_custom_service_name_from_env(self):
        self._clear_env()
        os.environ["AI_GATEWAY_OTEL_SERVICE_NAME"] = "custom-gw"
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.service_name == "custom-gw"

    def test_custom_sample_rate_from_env(self):
        self._clear_env()
        os.environ["AI_GATEWAY_OTEL_SAMPLE_RATE"] = "0.5"
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.sample_rate == 0.5

    def test_invalid_sample_rate_falls_back_to_default(self):
        self._clear_env()
        os.environ["AI_GATEWAY_OTEL_SAMPLE_RATE"] = "not-a-number"
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr = get_tracing_manager()
        assert mgr.sample_rate == 0.1

    def test_singleton_cached_across_calls(self):
        self._clear_env()
        from aigateway_core.shared.tracing import get_tracing_manager

        mgr_a = get_tracing_manager()
        mgr_b = get_tracing_manager()
        assert mgr_a is mgr_b


# ---------------------------------------------------------------------------
# 10. set_span_attribute() — static method
# ---------------------------------------------------------------------------


class TestSetSpanAttribute:
    def test_calls_set_attribute(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        TracingManager.set_span_attribute(mock_span, "key", "value")
        mock_span.set_attribute.assert_called_once_with("key", "value")

    def test_noop_for_none_span(self):
        from aigateway_core.shared.tracing import TracingManager

        TracingManager.set_span_attribute(None, "key", "value")

    def test_exception_logged_debug(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_span.set_attribute.side_effect = RuntimeError("fail")
        TracingManager.set_span_attribute(mock_span, "key", "value")


# ---------------------------------------------------------------------------
# 11. add_span_event() — static method
# ---------------------------------------------------------------------------


class TestAddSpanEvent:
    def test_calls_add_event(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        TracingManager.add_span_event(mock_span, "event-name", {"k": "v"})
        mock_span.add_event.assert_called_once_with("event-name", {"k": "v"})

    def test_empty_attributes_by_default(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        TracingManager.add_span_event(mock_span, "event-name")
        mock_span.add_event.assert_called_once_with("event-name", {})

    def test_noop_for_none_span(self):
        from aigateway_core.shared.tracing import TracingManager

        TracingManager.add_span_event(None, "event-name")

    def test_exception_logged_debug(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_span.add_event.side_effect = RuntimeError("boom")
        TracingManager.add_span_event(mock_span, "event-name")


# ---------------------------------------------------------------------------
# 12. mark_span_error() — static method
# ---------------------------------------------------------------------------


class TestMarkSpanError:
    def test_sets_status_and_records_exception(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_error = ValueError("something broke")

        # Status and StatusCode are imported inside initialize().
        mock_status = MagicMock()
        with patch.dict(
            _get_mod().__dict__,
            {"Status": MagicMock(return_value=mock_status), "StatusCode": MagicMock()},
        ):
            TracingManager.mark_span_error(mock_span, mock_error)

        mock_span.set_status.assert_called_once()
        mock_span.record_exception.assert_called_once_with(mock_error)

    def test_noop_for_none_span(self):
        from aigateway_core.shared.tracing import TracingManager

        TracingManager.mark_span_error(None, ValueError("ignored"))

    def test_exception_during_marking_logged_debug(self):
        from aigateway_core.shared.tracing import TracingManager

        mock_span = MagicMock()
        mock_span.set_status.side_effect = RuntimeError("mark failed")
        mock_error = Exception("original")

        mock_status = MagicMock()
        with patch.dict(
            _get_mod().__dict__,
            {"Status": MagicMock(return_value=mock_status), "StatusCode": MagicMock()},
        ):
            TracingManager.mark_span_error(mock_span, mock_error)


# ---------------------------------------------------------------------------
# 13. End-to-end: full lifecycle with mocked OTel
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_full_lifecycle(self):
        """Create manager -> initialize -> create span -> inject/extract -> info."""
        from aigateway_core.shared.tracing import TracingManager

        mock_resource = MagicMock()
        mock_provider = MagicMock()
        mock_tracer = MagicMock()
        mock_status_cls = MagicMock()
        mock_status_code = MagicMock()
        mock_status_code.ERROR = "ERROR"

        mock_span = MagicMock()
        mock_span.context.span_id = 0xDEADBEEF  # real int so format(..., "016x") works

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_span)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_cm

        # Set up sys.modules for OTel imports inside initialize()
        mod = _get_mod()
        mock_trace_mod = MagicMock()
        mock_trace_mod.set_tracer_provider = MagicMock()
        mock_trace_mod.get_tracer.return_value = mock_tracer

        orig_modules = {}
        otel_keys = [
            "opentelemetry",
            "opentelemetry.trace",
            "opentelemetry.sdk.resources",
            "opentelemetry.sdk.trace",
            "opentelemetry.sdk.trace.export",
            "opentelemetry.trace.sampling",
        ]
        for k in otel_keys:
            orig_modules[k] = sys.modules.pop(k, None)

        saved_uuid = sys.modules.get("uuid")
        orig_span_kind = getattr(mod, "SpanKind", None)
        orig_time = getattr(mod, "time", None)
        orig_uuid = getattr(mod, "uuid", None)

        fake_uuid_mod = MagicMock()
        fake_uuid_mod.uuid4.return_value.hex = "e2egeneratedid0001"

        try:
            # Make 'from opentelemetry import trace' resolve to mock_trace_mod
            otel_top = MagicMock()
            otel_top.trace = mock_trace_mod
            sys.modules["opentelemetry"] = otel_top
            sys.modules["opentelemetry.trace"] = mock_trace_mod
            res_mod = MagicMock()
            mock_resource_cls = MagicMock()
            mock_resource_cls.create.return_value = mock_resource
            res_mod.Resource = mock_resource_cls
            sys.modules["opentelemetry.sdk.resources"] = res_mod
            sys.modules["opentelemetry.sdk.trace"] = MagicMock(
                TracerProvider=MagicMock(return_value=mock_provider)
            )
            sys.modules["opentelemetry.sdk.trace.export"] = MagicMock(
                BatchSpanProcessor=MagicMock(),
                ConsoleSpanExporter=MagicMock(),
            )
            sys.modules["opentelemetry.trace.sampling"] = MagicMock(
                ParentBasedTraceIdRatio=MagicMock()
            )
            sys.modules["uuid"] = fake_uuid_mod

            mod.SpanKind = MagicMock(SERVER="SERVER")
            mod.Status = mock_status_cls
            mod.StatusCode = mock_status_code
            mod.time = MagicMock(time=MagicMock(return_value=1700000000.0))

            mgr = TracingManager(service_name="lifecycle-svc", sample_rate=0.5)
            mgr.initialize()

            assert mgr._initialized is True
            assert mgr._tracer is not None

            span_info = mgr.create_request_span("e2e-req-1", operation="e2e_test")
            assert span_info["trace_id"] == "e2egeneratedid0001"
            assert span_info["span_id"] == "00000000deadbeef"

            plugin_ctx = mgr.create_plugin_span(span_info, "test_plugin", "e2e-req-1")
            assert plugin_ctx["plugin_name"] == "test_plugin"

            TracingManager.set_span_attribute(mock_span, "pi", 3.14)
            mock_span.set_attribute.assert_called_with("pi", 3.14)

            TracingManager.add_span_event(mock_span, "tick")
            mock_span.add_event.assert_called_with("tick", {})

            TracingManager.mark_span_error(mock_span, RuntimeError("oops"))
            assert mock_span.record_exception.call_count == 1

            info = mgr.get_trace_info()
            assert info["enabled"] is True
            assert info["service_name"] == "lifecycle-svc"
            assert info["sample_rate"] == 0.5
            assert info["initialized"] is True

            headers: Dict[str, str] = {}
            TracingManager.inject_trace_context(
                headers, span_info["trace_id"], span_info["span_id"]
            )
            assert "traceparent" in headers

            extracted = TracingManager.extract_trace_context(headers)
            assert extracted["trace_id"] == span_info["trace_id"]
        finally:
            for k, v in orig_modules.items():
                if v is not None:
                    sys.modules[k] = v
                else:
                    sys.modules.pop(k, None)
            if saved_uuid is not None:
                sys.modules["uuid"] = saved_uuid
            else:
                sys.modules.pop("uuid", None)
            if orig_span_kind is not None:
                mod.SpanKind = orig_span_kind
            elif hasattr(mod, "SpanKind"):
                del mod.SpanKind
            if orig_time is not None:
                mod.time = orig_time
            elif hasattr(mod, "time"):
                del mod.time
            if orig_uuid is not None:
                mod.uuid = orig_uuid
            elif hasattr(mod, "uuid"):
                del mod.uuid
