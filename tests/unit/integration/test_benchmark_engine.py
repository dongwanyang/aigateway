"""Tests for benchmarks/engine.py — T1 (baseline reset) + T2 (trace_id cache hit matching)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from benchmarks.engine import (
    GroupStats,
    Sample,
    compute_savings,
    compute_stats,
    fetch_recent_logs,
    match_cache_hits_by_trace_id,
    render_html,
    render_markdown,
    restart_gateway,
    set_plugins,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_ok():
    return Sample(
        prompt="hello",
        ok=True,
        status="ok",
        latency_ms=100.0,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cost_usd=0.00001,
        cache_hit=False,
        trace_id="trace-001",
    )


@pytest.fixture
def sample_cached():
    return Sample(
        prompt="hello again",
        ok=True,
        status="ok",
        latency_ms=5.0,
        prompt_tokens=10,
        completion_tokens=0,
        total_tokens=10,
        cost_usd=0.0,
        cache_hit=True,
        cache_tier="L3",
        trace_id="trace-002",
    )


@pytest.fixture
def sample_error():
    return Sample(
        prompt="bad prompt",
        ok=False,
        status="error",
        latency_ms=50.0,
        error="connection refused",
        trace_id="trace-003",
    )


# ---------------------------------------------------------------------------
# T1: Baseline reset (restart_gateway)
# ---------------------------------------------------------------------------

class TestBaselineReset:
    """D9: Gateway restart instead of flush_cache."""

    @pytest.mark.asyncio
    async def test_restart_gateway_uses_admin_endpoint(self):
        """restart_gateway() calls POST /admin/restart on success."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        post_context = MagicMock()
        post_context.__aenter__ = AsyncMock(return_value=mock_resp)
        post_context.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=post_context)
        mock_session.__aenter__.return_value = mock_session

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await restart_gateway("http://localhost:8000", timeout=5)

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert "/admin/restart" in str(call_kwargs[0][0])

    @pytest.mark.asyncio
    async def test_restart_gateway_fallback_to_config_touch(self):
        """If /admin/restart fails, restart_gateway touches config.yaml."""
        mock_resp = MagicMock()
        mock_resp.status = 500

        post_context = MagicMock()
        post_context.__aenter__ = AsyncMock(
            side_effect=RuntimeError("no restart endpoint")
        )
        post_context.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=post_context)
        mock_session.__aenter__.return_value = mock_session

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("os.utime") as mock_utime, \
             patch("os.path.exists", return_value=True):
            await restart_gateway("http://localhost:8000", timeout=5)

        mock_utime.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_gateway_raises_when_no_config(self):
        """If no restart endpoint and no config.yaml, raise RuntimeError."""
        post_context = MagicMock()
        post_context.__aenter__ = AsyncMock(side_effect=RuntimeError("no endpoint"))
        post_context.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=post_context)
        mock_session.__aenter__.return_value = mock_session

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("os.path.exists", return_value=False):
            with pytest.raises(RuntimeError, match="Cannot restart gateway"):
                await restart_gateway("http://localhost:8000", timeout=5)


class TestBenchmarkAdminContracts:
    @pytest.mark.asyncio
    async def test_set_plugins_uses_one_admin_contract_request_per_plugin(self):
        response = MagicMock(status=200)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.put.return_value = response

        await set_plugins(False, session, "http://gateway.test")

        assert session.put.call_count > 1
        assert all(
            set(call.kwargs["json"]) == {"name", "enabled"}
            for call in session.put.call_args_list
        )
        assert all(
            call.kwargs["json"]["enabled"] is False
            for call in session.put.call_args_list
        )

    @pytest.mark.asyncio
    async def test_fetch_recent_logs_unwraps_admin_response(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(
            return_value={"data": {"items": [{"trace_id": "trace-1"}]}}
        )
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get.return_value = response

        logs = await fetch_recent_logs(session, "http://gateway.test")

        assert logs == [{"trace_id": "trace-1"}]


# ---------------------------------------------------------------------------
# T2: Cache hit trace_id matching
# ---------------------------------------------------------------------------

class TestCacheHitTraceIdMatch:
    """D10: Exact trace_id matching instead of time-order approximation."""

    def test_match_cache_hits_by_trace_id(self):
        """Samples with matching trace_ids get cache_hit=True."""
        samples = [
            Sample(prompt="a", trace_id="t1"),
            Sample(prompt="b", trace_id="t2"),
            Sample(prompt="c", trace_id="t3"),
        ]
        logs = [
            {"trace_id": "t1", "cache_hit": True, "cache_tier": "L2"},
            {"trace_id": "t3", "cache_hit": False},
        ]

        match_cache_hits_by_trace_id(samples, logs)

        assert samples[0].cache_hit is True
        assert samples[0].cache_tier == "L2"
        assert samples[1].cache_hit is False
        assert samples[2].cache_hit is False

    def test_sample_without_trace_id_not_matched(self):
        """Samples without trace_id remain unmatched."""
        samples = [Sample(prompt="x", trace_id=None)]
        logs = [{"trace_id": "x", "cache_hit": True}]

        match_cache_hits_by_trace_id(samples, logs)

        assert samples[0].cache_hit is False

    def test_log_with_request_id_alias(self):
        """Logs may use request_id instead of trace_id."""
        samples = [Sample(prompt="x", trace_id="req-abc")]
        logs = [{"request_id": "req-abc", "cache_hit": True, "cache_tier": "L1"}]

        match_cache_hits_by_trace_id(samples, logs)

        assert samples[0].cache_hit is True
        assert samples[0].cache_tier == "L1"

    def test_log_with_cached_field(self):
        """Logs with cached=True also indicate cache hit."""
        samples = [Sample(prompt="x", trace_id="t-cached")]
        logs = [{"trace_id": "t-cached", "status": "ok", "cached": True}]

        match_cache_hits_by_trace_id(samples, logs)

        assert samples[0].cache_hit is True


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

class TestComputeStats:
    def test_basic_stats(self, sample_ok, sample_cached, sample_error):
        stats = compute_stats([sample_ok, sample_cached, sample_error])
        assert stats.count == 3
        assert stats.ok_count == 2
        assert stats.error_count == 1
        assert stats.cache_hits == 1
        assert stats.cache_tiers == {"L3": 1}
        assert stats.p50_latency == pytest.approx(50.0)

    def test_cost_estimation(self, sample_ok):
        # Prices are per-1M tokens: (prompt_price_per_1M, completion_price_per_1M)
        prices = {"deepseek-v4-flash": (0.5, 2.0)}
        sample_ok.model = "deepseek-v4-flash"
        stats = compute_stats([sample_ok], prices)
        # (10 * 0.5 + 20 * 2.0) / 1_000_000 = 0.000045
        assert stats.total_cost_usd == pytest.approx(0.000045)

    def test_cache_hit_excluded_from_cost(self, sample_cached):
        prices = {"model": (1.0, 2.0)}
        sample_cached.model = "model"
        stats = compute_stats([sample_cached], prices)
        assert stats.total_cost_usd == 0.0

    def test_empty_samples(self):
        stats = compute_stats([])
        assert stats.count == 0
        assert stats.p50_latency == 0.0
        assert stats.success_rate == 0.0


# ---------------------------------------------------------------------------
# Savings computation
# ---------------------------------------------------------------------------

class TestComputeSavings:
    def test_token_saving(self):
        baseline = GroupStats(total_tokens=1000, ok_count=10, count=10)
        optimized = GroupStats(total_tokens=500, ok_count=10, count=10)
        savings = compute_savings(baseline, optimized)
        assert savings["token_saving_pct"] == 50.0
        assert savings["token_saving"] == 500

    def test_quality_delta(self):
        baseline = GroupStats(quality_scores=[4.0, 3.5])
        optimized = GroupStats(quality_scores=[4.5, 4.0])
        savings = compute_savings(baseline, optimized)
        assert savings["quality_baseline"] == 3.75
        assert savings["quality_optimized"] == 4.25
        assert savings["quality_delta"] == 0.5


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

class TestRenderReport:
    def test_render_markdown_contains_key_sections(self):
        md = render_markdown(
            "test_scenario",
            GroupStats(count=10, total_tokens=1000),
            GroupStats(count=10, total_tokens=500),
            {"token_saving_pct": 50.0},
            {"python": "3.11"},
        )
        assert "test_scenario" in md
        assert "Token saving" in md or "token_saving" in md.lower()

    def test_render_html_wraps_markdown(self):
        md = "# Test Report\n\nHello"
        html = render_html("Test", md)
        assert "<html>" in html
        assert "<pre" in html
        assert "Hello" in html
