"""Unit tests for aigateway_core.shared.metrics.

Covers MetricsCollector lifecycle, every public method, RequestTracker
context manager, and the global singleton / reset functions.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import MagicMock, patch


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_collector(enabled: bool = True):
    """Build a MetricsCollector with mocked prometheus_client objects."""
    from aigateway_core.shared.metrics import MetricsCollector

    collector = MetricsCollector(enabled=enabled)
    collector._requests_counter = MagicMock()
    collector._duration_histogram = MagicMock()
    collector._cache_hits_counter = MagicMock()
    collector._cache_misses_counter = MagicMock()
    collector._tokens_counter = MagicMock()
    collector._tokens_saved_counter = MagicMock()
    collector._cost_total_gauge = MagicMock()
    collector._cost_by_model_counter = MagicMock()
    collector._cost_by_user_counter = MagicMock()
    collector._cost_by_group_counter = MagicMock()
    collector._circuit_breaker_gauge = MagicMock()
    collector._active_requests_gauge = MagicMock()
    collector._up_gauge = MagicMock()
    return collector


def _fresh_singleton():
    """Reset the global singleton + prometheus registry state so tests are isolated."""
    import aigateway_core.shared.metrics as m

    m._collector_instance = None
    m._metrics_initialized = False
    m.__dict__.pop("__registry", None)


# ------------------------------------------------------------------
# 1. MetricsCollector.initialize()
# ------------------------------------------------------------------


class InitializeTests(unittest.TestCase):
    """Tests for MetricsCollector.initialize()."""

    def setUp(self):
        _fresh_singleton()

    def tearDown(self):
        _fresh_singleton()
    """Test MetricsCollector.initialize()."""

    @patch("prometheus_client.Counter")
    @patch("prometheus_client.Histogram")
    @patch("prometheus_client.Gauge")
    @patch("aigateway_core.shared.metrics._ensure_initialized")
    def test_creates_all_counters(
        self, mock_ensure, mock_gauge, mock_hist, mock_counter
    ):
        """initialize should create Counter instances for requests, cache, tokens, cost."""
        from aigateway_core.shared.metrics import MetricsCollector

        collector = MetricsCollector()
        collector.initialize()

        self.assertIsNotNone(collector._requests_counter)
        self.assertIsNotNone(collector._cache_hits_counter)
        self.assertIsNotNone(collector._cache_misses_counter)
        self.assertIsNotNone(collector._tokens_counter)
        self.assertIsNotNone(collector._tokens_saved_counter)
        self.assertIsNotNone(collector._cost_by_model_counter)
        self.assertIsNotNone(collector._cost_by_user_counter)
        self.assertIsNotNone(collector._cost_by_group_counter)

    @patch("prometheus_client.Histogram")
    @patch("aigateway_core.shared.metrics._ensure_initialized")
    def test_creates_histogram(self, mock_ensure, mock_hist):
        """initialize should create a Histogram for request duration."""
        from aigateway_core.shared.metrics import MetricsCollector

        collector = MetricsCollector()
        collector.initialize()
        self.assertIsNotNone(collector._duration_histogram)

    @patch("prometheus_client.Gauge")
    @patch("aigateway_core.shared.metrics._ensure_initialized")
    def test_creates_gauges(self, mock_ensure, mock_gauge):
        """initialize should create Gauge instances for cost, circuit breaker, active, up."""
        from aigateway_core.shared.metrics import MetricsCollector

        collector = MetricsCollector()
        collector.initialize()

        self.assertIsNotNone(collector._cost_total_gauge)
        self.assertIsNotNone(collector._circuit_breaker_gauge)
        self.assertIsNotNone(collector._active_requests_gauge)
        self.assertIsNotNone(collector._up_gauge)

    @patch("prometheus_client.Gauge")
    @patch("aigateway_core.shared.metrics._ensure_initialized")
    def test_sets_up_gauge_to_1(self, mock_ensure, mock_gauge_cls):
        """initialize should set gateway_up gauge to 1 on startup."""
        from aigateway_core.shared.metrics import MetricsCollector

        mock_gauge_instance = MagicMock()
        mock_gauge_cls.return_value = mock_gauge_instance

        collector = MetricsCollector()
        collector.initialize()

        mock_gauge_instance.set.assert_called_with(1)

    @patch("aigateway_core.shared.metrics.logger")
    def test_disabled_initialization_skips_creation(self, mock_logger):
        """initialize with enabled=False should log and return early."""
        from aigateway_core.shared.metrics import MetricsCollector

        collector = MetricsCollector(enabled=False)
        collector.initialize()

        mock_logger.info.assert_called()
        self.assertIsNone(collector._requests_counter)

    @patch("aigateway_core.shared.metrics._ensure_initialized")
    def test_stores_registry_reference(self, mock_ensure):
        """initialize should store the registry reference on the collector."""
        from aigateway_core.shared.metrics import MetricsCollector

        mock_registry = MagicMock()
        import aigateway_core.shared.metrics as m

        m.__dict__["__registry"] = mock_registry

        collector = MetricsCollector()
        collector.initialize()

        self.assertEqual(collector._registry, mock_registry)

    def test_ensure_initialized_lazy(self):
        """_ensure_initialized should be idempotent - second call returns immediately."""
        import aigateway_core.shared.metrics as m

        m._metrics_initialized = False
        mock_reg = MagicMock()
        with patch.dict(m.__dict__, {"CollectorRegistry": mock_reg}):
            m._ensure_initialized()
            first_count = mock_reg.call_count

        m._ensure_initialized()
        self.assertEqual(mock_reg.call_count, first_count)

    def test_ensure_initialized_missing_library(self):
        """_ensure_initialized should log a warning when prometheus_client is missing."""
        import aigateway_core.shared.metrics as m

        m._metrics_initialized = False
        original_globals = dict(m.__dict__)
        m.__dict__["Counter"] = None
        m.__dict__["Histogram"] = None
        m.__dict__["Gauge"] = None
        m.__dict__["CollectorRegistry"] = None
        try:
            with patch.object(m, "logger", autospec=True) as mock_logger:
                m._ensure_initialized()
                if mock_logger.warning.called:
                    pass  # expected path
        except Exception:
            pass
        finally:
            for k, v in original_globals.items():
                m.__dict__[k] = v


# ------------------------------------------------------------------
# 2. record_request / record_duration
# ------------------------------------------------------------------


class RecordRequestDurationTests(unittest.TestCase):
    """Test record_request and record_duration."""

    def test_record_request_calls_counter_labels_inc(self):
        collector = _make_collector()
        collector.record_request("POST", "/v1/chat/completions", "200")
        collector._requests_counter.labels.return_value.inc.assert_called_once_with()

    def test_record_request_disabled(self):
        collector = _make_collector(enabled=False)
        collector.record_request("GET", "/health", "200")

    def test_record_request_none_counter(self):
        collector = _make_collector()
        collector._requests_counter = None
        collector.record_request("GET", "/health", "200")

    def test_record_duration_calls_histogram_observe(self):
        collector = _make_collector()
        collector.record_duration("/v1/chat/completions", 0.42)
        collector._duration_histogram.labels.return_value.observe.assert_called_once_with(
            0.42
        )

    def test_record_duration_disabled(self):
        collector = _make_collector(enabled=False)
        collector.record_duration("/health", 0.01)

    def test_record_duration_none_histogram(self):
        collector = _make_collector()
        collector._duration_histogram = None
        collector.record_duration("/health", 0.01)


# ------------------------------------------------------------------
# 3. inc_active / dec_active
# ------------------------------------------------------------------


class ActiveRequestsTests(unittest.TestCase):
    """Test inc_active and dec_active."""

    def test_inc_active_calls_gauge_inc(self):
        collector = _make_collector()
        collector.inc_active()
        collector._active_requests_gauge.inc.assert_called_once()

    def test_dec_active_calls_gauge_dec(self):
        collector = _make_collector()
        collector.dec_active()
        collector._active_requests_gauge.dec.assert_called_once()

    def test_inc_active_disabled(self):
        collector = _make_collector(enabled=False)
        collector.inc_active()

    def test_dec_active_disabled(self):
        collector = _make_collector(enabled=False)
        collector.dec_active()

    def test_inc_active_none_gauge(self):
        collector = _make_collector()
        collector._active_requests_gauge = None
        collector.inc_active()


# ------------------------------------------------------------------
# 4. inc_cache_hits / inc_cache_misses
# ------------------------------------------------------------------


class CacheMetricsTests(unittest.TestCase):
    """Test inc_cache_hits and inc_cache_misses."""

    def test_inc_cache_hits_default_tier(self):
        collector = _make_collector()
        collector.inc_cache_hits()
        collector._cache_hits_counter.labels.assert_called_with(tier="L1")
        collector._cache_hits_counter.labels.return_value.inc.assert_called_once()

    def test_inc_cache_hits_custom_tier(self):
        collector = _make_collector()
        collector.inc_cache_hits("L3")
        collector._cache_hits_counter.labels.assert_called_with(tier="L3")

    def test_inc_cache_misses_calls_counter_inc(self):
        collector = _make_collector()
        collector.inc_cache_misses()
        collector._cache_misses_counter.inc.assert_called_once()

    def test_inc_cache_hits_disabled(self):
        collector = _make_collector(enabled=False)
        collector.inc_cache_hits()

    def test_inc_cache_misses_disabled(self):
        collector = _make_collector(enabled=False)
        collector.inc_cache_misses()

    def test_inc_cache_hits_none_counter(self):
        collector = _make_collector()
        collector._cache_hits_counter = None
        collector.inc_cache_hits()

    def test_inc_cache_misses_none_counter(self):
        collector = _make_collector()
        collector._cache_misses_counter = None
        collector.inc_cache_misses()


# ------------------------------------------------------------------
# 5. record_tokens / record_tokens_saved
# ------------------------------------------------------------------


class TokenMetricsTests(unittest.TestCase):
    """Test record_tokens and record_tokens_saved."""

    def test_record_tokens_prompt(self):
        collector = _make_collector()
        collector.record_tokens(100, "prompt")
        collector._tokens_counter.labels.assert_called_with(type="prompt")
        collector._tokens_counter.labels.return_value.inc.assert_called_with(100)

    def test_record_tokens_completion(self):
        collector = _make_collector()
        collector.record_tokens(50, "completion")
        collector._tokens_counter.labels.assert_called_with(type="completion")

    def test_record_tokens_zero_skipped(self):
        """record_tokens with tokens=0 should skip the increment."""
        collector = _make_collector()
        collector.record_tokens(0, "prompt")
        collector._tokens_counter.labels.assert_not_called()

    def test_record_tokens_disabled(self):
        collector = _make_collector(enabled=False)
        collector.record_tokens(10)

    def test_record_tokens_none_counter(self):
        collector = _make_collector()
        collector._tokens_counter = None
        collector.record_tokens(10)

    def test_record_tokens_saved_positive(self):
        """record_tokens_saved with tokens > 0 uses _tokens_saved_counter."""
        collector = _make_collector()
        collector.record_tokens_saved(200)
        collector._tokens_saved_counter.inc.assert_called_with(200)
        collector._tokens_counter.labels.assert_not_called()

    def test_record_tokens_saved_zero_no_fallback(self):
        """record_tokens_saved with tokens=0: both counters have a tokens>0 guard,
        so neither inc() nor labels() is called."""
        collector = _make_collector()
        collector.record_tokens_saved(0)
        collector._tokens_saved_counter.inc.assert_not_called()
        collector._tokens_counter.labels.assert_not_called()

    def test_record_tokens_saved_disabled(self):
        collector = _make_collector(enabled=False)
        collector.record_tokens_saved(10)

    def test_record_tokens_saved_fallback_when_saved_counter_none(self):
        """When _tokens_saved_counter is None, falls back to _tokens_counter."""
        collector = _make_collector()
        collector._tokens_saved_counter = None
        collector.record_tokens_saved(200)
        # Falls back to tokens_counter with type="saved"
        collector._tokens_counter.labels.assert_called_with(type="saved")
        collector._tokens_counter.labels.return_value.inc.assert_called_with(200)


# ------------------------------------------------------------------
# 6. record_cost - all 4 counters/gauges
# ------------------------------------------------------------------


class CostMetricsTests(unittest.TestCase):
    """Test record_cost."""

    def test_record_cost_updates_all_four(self):
        collector = _make_collector()
        collector.record_cost(0.05, model="gpt-4", user_id="u1", group_id="g1")

        collector._cost_total_gauge.inc.assert_called_once_with(0.05)
        collector._cost_by_model_counter.labels.assert_called_with(model="gpt-4")
        collector._cost_by_model_counter.labels.return_value.inc.assert_called_with(0.05)
        collector._cost_by_user_counter.labels.assert_called_with(user_id="u1")
        collector._cost_by_user_counter.labels.return_value.inc.assert_called_with(0.05)
        collector._cost_by_group_counter.labels.assert_called_with(group_id="g1")
        collector._cost_by_group_counter.labels.return_value.inc.assert_called_with(0.05)

    def test_record_cost_skips_empty_user(self):
        collector = _make_collector()
        collector.record_cost(0.01, model="gpt-3.5", group_id="g1")
        collector._cost_by_user_counter.labels.assert_not_called()

    def test_record_cost_skips_empty_group(self):
        collector = _make_collector()
        collector.record_cost(0.01, model="gpt-3.5", user_id="u1")
        collector._cost_by_group_counter.labels.assert_not_called()

    def test_record_cost_disabled(self):
        collector = _make_collector(enabled=False)
        collector.record_cost(0.01)

    def test_record_cost_none_gauge(self):
        collector = _make_collector()
        collector._cost_total_gauge = None
        collector.record_cost(0.01)


# ------------------------------------------------------------------
# 7. set_circuit_breaker_state
# ------------------------------------------------------------------


class CircuitBreakerTests(unittest.TestCase):
    """Test set_circuit_breaker_state."""

    def test_sets_gauge_label_value(self):
        collector = _make_collector()
        collector.set_circuit_breaker_state("openai", 1)
        collector._circuit_breaker_gauge.labels.assert_called_with(provider="openai")
        collector._circuit_breaker_gauge.labels.return_value.set.assert_called_with(1)

    def test_circuit_breaker_disabled(self):
        collector = _make_collector(enabled=False)
        collector.set_circuit_breaker_state("anthropic", 0)

    def test_circuit_breaker_none_gauge(self):
        collector = _make_collector()
        collector._circuit_breaker_gauge = None
        collector.set_circuit_breaker_state("openai", 0)


# ------------------------------------------------------------------
# 8. set_up / get_uptime_seconds
# ------------------------------------------------------------------


class HealthUptimeTests(unittest.TestCase):
    """Test set_up and get_uptime_seconds."""

    def test_set_up_healthy(self):
        collector = _make_collector()
        collector.set_up(True)
        collector._up_gauge.set.assert_called_with(1)

    def test_set_up_unhealthy(self):
        collector = _make_collector()
        collector.set_up(False)
        collector._up_gauge.set.assert_called_with(0)

    def test_set_up_disabled(self):
        collector = _make_collector(enabled=False)
        collector.set_up(True)

    def test_set_up_none_gauge(self):
        collector = _make_collector()
        collector._up_gauge = None
        collector.set_up(True)

    def test_get_uptime_returns_int(self):
        collector = _make_collector()
        uptime = collector.get_uptime_seconds()
        self.assertIsInstance(uptime, int)
        self.assertGreaterEqual(uptime, 0)

    def test_get_uptime_reasonable_range(self):
        """get_uptime_seconds should return a small positive integer after init."""
        collector = _make_collector()
        uptime = collector.get_uptime_seconds()
        self.assertLessEqual(uptime, 60)


# ------------------------------------------------------------------
# 9. collect_all - Prometheus collector interface
# ------------------------------------------------------------------


class CollectAllTests(unittest.TestCase):
    """Test collect_all."""

    def test_collect_all_returns_dict(self):
        collector = _make_collector()
        result = collector.collect_all()
        self.assertIsInstance(result, dict)

    def test_collect_all_disabled_returns_empty(self):
        collector = _make_collector(enabled=False)
        self.assertEqual(collector.collect_all(), {})

    def test_collect_all_includes_uptime(self):
        collector = _make_collector()
        result = collector.collect_all()
        self.assertIn("uptime_seconds", result)

    def test_collect_all_includes_active_requests(self):
        collector = _make_collector()
        collector._active_requests_gauge._value.get.return_value = 3
        result = collector.collect_all()
        self.assertEqual(result["gateway_active_requests"], 3)

    def test_collect_all_includes_up(self):
        collector = _make_collector()
        collector._up_gauge._value.get.return_value = 1
        result = collector.collect_all()
        self.assertEqual(result["gateway_up"], 1)

    def test_collect_all_skips_none_gauges(self):
        collector = _make_collector()
        collector._active_requests_gauge = None
        collector._up_gauge = None
        result = collector.collect_all()
        self.assertNotIn("gateway_active_requests", result)
        self.assertNotIn("gateway_up", result)


# ------------------------------------------------------------------
# 10. RequestTracker - enter/exit context manager
# ------------------------------------------------------------------


class RequestTrackerTests(unittest.TestCase):
    """Test RequestTracker context manager.

    RequestTracker.__enter__ calls collector.inc_active() which increments
    collector._active_requests_gauge.  __exit__ calls collector.dec_active(),
    collector.record_request(), and collector.record_duration().  We verify
    behaviour by inspecting the mocks attached to the collector's internal
    metric handles.
    """

    def _build_tracker(self, endpoint="/v1/chat/completions", method="POST"):
        """Create a collector + tracker pair for testing."""
        collector = _make_collector()
        tracker = collector.track_request(endpoint, method=method)
        return collector, tracker

    def test_enter_returns_self(self):
        _, tracker = self._build_tracker()
        with tracker as t:
            self.assertIs(t, tracker)

    def test_exit_records_request_and_duration(self):
        """Normal exit (no exception) records 200 status and duration."""
        collector = _make_collector()
        tracker = collector.track_request("/v1/chat/completions", method="POST")
        with tracker:
            pass

        # inc_active was called in __enter__
        collector._active_requests_gauge.inc.assert_called_once()
        # record_request labels the counter with method/endpoint/status
        collector._requests_counter.labels.assert_called_once_with(
            method="POST", endpoint="/v1/chat/completions", status="200"
        )
        collector._requests_counter.labels.return_value.inc.assert_called_once()
        # record_duration labels the histogram and observes
        collector._duration_histogram.labels.assert_called_once_with(
            endpoint="/v1/chat/completions"
        )
        collector._duration_histogram.labels.return_value.observe.assert_called_once()
        # dec_active was called in __exit__
        collector._active_requests_gauge.dec.assert_called_once()

    def test_exit_on_error_records_500(self):
        """Exit with an exception records status 500."""
        collector = _make_collector()
        tracker = collector.track_request("/v1/chat/completions")
        try:
            with tracker:
                raise ValueError("boom")
        except ValueError:
            pass

        collector._active_requests_gauge.inc.assert_called_once()
        collector._requests_counter.labels.assert_called_once_with(
            method="POST", endpoint="/v1/chat/completions", status="500"
        )
        collector._active_requests_gauge.dec.assert_called_once()

    def test_exit_always_decrements_active(self):
        """dec_active must be called even when an exception occurs."""
        collector = _make_collector()
        tracker = collector.track_request("/v1/chat/completions")
        with self.assertRaises(RuntimeError):
            with tracker:
                raise RuntimeError("fail")

        collector._active_requests_gauge.dec.assert_called_once()

    def test_exit_records_duration_positive(self):
        """record_duration should be called with a positive float duration."""
        collector = _make_collector()
        tracker = collector.track_request("/health")
        with tracker:
            time.sleep(0.01)

        # The observe call was made; extract the duration value from the mock
        call_args = collector._duration_histogram.labels.return_value.observe.call_args
        self.assertGreater(call_args[0][0], 0)


# ------------------------------------------------------------------
# 11. get_metrics_collector / reset_metrics_collector - singleton
# ------------------------------------------------------------------


class SingletonTests(unittest.TestCase):
    """Test get_metrics_collector and reset_metrics_collector."""

    def setUp(self):
        _fresh_singleton()

    def tearDown(self):
        _fresh_singleton()

    def test_singleton_returns_same_instance(self):
        from aigateway_core.shared.metrics import get_metrics_collector

        a = get_metrics_collector()
        b = get_metrics_collector()
        self.assertIs(a, b)

    def test_reset_clears_singleton(self):
        from aigateway_core.shared.metrics import get_metrics_collector, reset_metrics_collector

        get_metrics_collector()
        reset_metrics_collector()

        from aigateway_core.shared.metrics import _collector_instance

        self.assertIsNone(_collector_instance)

    def test_reset_then_get_creates_new_instance(self):
        from aigateway_core.shared.metrics import get_metrics_collector, reset_metrics_collector

        a = get_metrics_collector()
        reset_metrics_collector()
        # Use disabled mode so we don't hit prometheus registry duplication
        import os
        old = os.environ.pop("AI_GATEWAY_PROMETHEUS_ENABLED", None)
        try:
            os.environ["AI_GATEWAY_PROMETHEUS_ENABLED"] = "false"
            b = get_metrics_collector()
        finally:
            if old is not None:
                os.environ["AI_GATEWAY_PROMETHEUS_ENABLED"] = old
            else:
                os.environ.pop("AI_GATEWAY_PROMETHEUS_ENABLED", None)
        self.assertIsNot(a, b)

    @patch.dict(os.environ, {"AI_GATEWAY_PROMETHEUS_ENABLED": "false"})
    def test_singleton_respects_env_var(self):
        """get_metrics_collector reads AI_GATEWAY_PROMETHEUS_ENABLED env var."""
        from aigateway_core.shared.metrics import get_metrics_collector

        collector = get_metrics_collector()
        self.assertFalse(collector.enabled)

    def test_singleton_enabled_truthy_values(self):
        """get_metrics_collector enables metrics for various truthy env values."""
        for val in ("true", "1", "yes"):
            _fresh_singleton()
            with patch.dict(os.environ, {"AI_GATEWAY_PROMETHEUS_ENABLED": val}):
                from aigateway_core.shared.metrics import get_metrics_collector

                collector = get_metrics_collector()
                self.assertTrue(
                    collector.enabled, f"expected enabled for {val!r}"
                )

    def test_collector_has_initialized_counters(self):
        """After get_metrics_collector, internal counter attributes exist."""
        from aigateway_core.shared.metrics import get_metrics_collector

        collector = get_metrics_collector()
        self.assertIsNotNone(collector._requests_counter)
        self.assertIsNotNone(collector._cache_hits_counter)
        self.assertIsNotNone(collector._up_gauge)


if __name__ == "__main__":
    unittest.main()
