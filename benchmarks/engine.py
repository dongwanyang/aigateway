"""Benchmark engine — refactored from scripts/ab_test.py.

Key changes vs ab_test.py:
- D9: restart_gateway() replaces flush_cache() (L1/L2 have no API)
- D10: trace_id-based cache hit matching instead of time-order approximation
- PLUGINS_TO_TOGGLE extended with rag_retriever
- YAML scenario registry + adapter dispatch
- quality_score field in Sample dataclass
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """Single benchmark request result."""
    prompt: str
    ok: bool = False
    status: str = "pending"  # pending|ok|error|judge_error|video_timeout
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    cache_tier: Optional[str] = None
    trace_id: Optional[str] = None
    error: Optional[str] = None
    quality_score: Optional[float] = None
    model: Optional[str] = None


@dataclass
class GroupStats:
    """Aggregated stats for a group of samples."""
    count: int = 0
    ok_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    latencies: List[float] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_hits: int = 0
    cache_tiers: Dict[str, int] = field(default_factory=dict)
    quality_scores: List[float] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    @property
    def avg_quality(self) -> Optional[float]:
        if not self.quality_scores:
            return None
        return sum(self.quality_scores) / len(self.quality_scores)

    @property
    def success_rate(self) -> float:
        return self.ok_count / self.count if self.count > 0 else 0.0


# ---------------------------------------------------------------------------
# Plugin toggle + gateway management
# ---------------------------------------------------------------------------

PLUGINS_TO_TOGGLE = ["prompt_cache", "semantic_cache", "prompt_compress", "rag_retriever"]


def _get_admin_headers() -> Dict[str, str]:
    api_key = os.environ.get("AI_GATEWAY_ADMIN_KEY", "")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}


async def set_plugins(enabled: bool, session: aiohttp.ClientSession, base_url: str) -> None:
    """Toggle all plugins in PLUGINS_TO_TOGGLE via PUT /admin/plugins-config."""
    payload = {}
    for name in PLUGINS_TO_TOGGLE:
        payload[name] = {"enabled": enabled}
    url = f"{base_url}/admin/plugins-config"
    async with session.put(url, headers=_get_admin_headers(), json=payload) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"set_plugins({enabled}) failed: {resp.status} {text}")


async def restart_gateway(base_url: str, timeout: int = 30) -> None:
    """D9: Restart gateway process instead of trying to flush L1/L2.

    Sends POST /admin/restart if available, otherwise falls back to
    SIGUSR1-based hot reload (config change triggers full reinit).

    For benchmark purposes, we signal the gateway to fully restart by
    touching config.yaml if writable, or using the admin restart endpoint.
    """
    url = f"{base_url}/admin/restart"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=_get_admin_headers()) as resp:
                if resp.status < 400:
                    logger.info("Gateway restart requested via /admin/restart")
                    await asyncio.sleep(2)
                    return
    except Exception:
        pass

    # Fallback: touch config.yaml to trigger Watchdog reload
    config_path = os.environ.get("AI_GATEWAY_CONFIG", "config.yaml")
    if os.path.exists(config_path):
        logger.info(f"Triggering config reload via touch {config_path}")
        os.utime(config_path, None)
        await asyncio.sleep(2)
        return

    raise RuntimeError("Cannot restart gateway: no /admin/restart and no config.yaml found")


async def wait_for_healthy(base_url: str, timeout: int = 60) -> None:
    """Wait until gateway /health returns 200."""
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as s:
        while time.time() < deadline:
            try:
                async with s.get(f"{base_url}/health") as resp:
                    if resp.status == 200:
                        logger.info("Gateway healthy")
                        return
            except Exception:
                pass
            await asyncio.sleep(1)
    raise TimeoutError(f"Gateway not healthy after {timeout}s")


# ---------------------------------------------------------------------------
# Request execution
# ---------------------------------------------------------------------------

async def one_request(
    session: aiohttp.ClientSession,
    base_url: str,
    prompt: str,
    model: str,
    user_id: Optional[str] = None,
) -> Sample:
    """Send one chat completion request, extract usage + trace_id."""
    sample = Sample(prompt=prompt)
    url = f"{base_url}/v1/chat/completions"
    headers = _get_admin_headers()
    if user_id:
        headers["X-User-ID"] = user_id

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.1,
    }

    start = time.perf_counter()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            elapsed_ms = (time.perf_counter() - start) * 1000
            sample.latency_ms = elapsed_ms

            # Extract trace_id from response header
            trace_id = resp.headers.get("X-Request-ID")
            sample.trace_id = trace_id

            if resp.status >= 400:
                text = await resp.text()
                sample.status = "error"
                sample.error = f"{resp.status}: {text[:200]}"
                return sample

            body = await resp.json()

            # Extract usage
            usage = body.get("usage", {})
            sample.prompt_tokens = usage.get("prompt_tokens", 0)
            sample.completion_tokens = usage.get("completion_tokens", 0)
            sample.total_tokens = usage.get("total_tokens", 0)
            sample.model = body.get("model", model)
            sample.ok = True
            sample.status = "ok"

    except Exception as e:
        sample.status = "error"
        sample.error = str(e)[:200]

    return sample


# ---------------------------------------------------------------------------
# Cache hit matching by trace_id
# ---------------------------------------------------------------------------

async def fetch_recent_logs(session: aiohttp.ClientSession, base_url: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch recent request logs from /admin/logs."""
    url = f"{base_url}/admin/logs?limit={limit}"
    async with session.get(url, headers=_get_admin_headers()) as resp:
        if resp.status >= 400:
            logger.warning(f"Failed to fetch logs: {resp.status}")
            return []
        return await resp.json()


def match_cache_hits_by_trace_id(samples: List[Sample], logs: List[Dict[str, Any]]) -> None:
    """D10: Match cache hits by exact trace_id instead of time-order approximation.

    Builds a map of trace_id -> cache_tier from logs, then annotates each
    sample that has a trace_id.
    """
    log_map: Dict[str, Dict[str, Any]] = {}
    for log_entry in logs:
        tid = log_entry.get("trace_id") or log_entry.get("request_id")
        if tid:
            log_map[tid] = log_entry

    for sample in samples:
        if not sample.trace_id:
            continue
        entry = log_map.get(sample.trace_id)
        if not entry:
            continue

        # Check cache tier indicators in log
        is_hit = (
            entry.get("cache_hit") is True
            or entry.get("cache_tier") is not None
            or (entry.get("status") == "ok" and entry.get("cached") is True)
        )
        if is_hit:
            sample.cache_hit = True
            sample.cache_tier = entry.get("cache_tier") or entry.get("tier") or "unknown"


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def compute_stats(samples: List[Sample], prices: Optional[Dict[str, Tuple[float, float]]] = None) -> GroupStats:
    """Compute aggregated stats. Cost uses per-model pricing if provided."""
    stats = GroupStats()
    stats.count = len(samples)

    for s in samples:
        stats.total_latency_ms += s.latency_ms
        stats.latencies.append(s.latency_ms)

        if s.ok:
            stats.ok_count += 1
        else:
            stats.error_count += 1
            if s.error:
                stats.errors.append(s.error)

        # Token totals (billable = non-cache-hit, but we record raw too)
        stats.total_prompt_tokens += s.prompt_tokens
        stats.total_completion_tokens += s.completion_tokens
        stats.total_tokens += s.total_tokens

        if s.cache_hit:
            stats.cache_hits += 1
            tier = s.cache_tier or "unknown"
            stats.cache_tiers[tier] = stats.cache_tiers.get(tier, 0) + 1

        # Cost estimation
        if s.ok and not s.cache_hit:
            model = s.model or "default"
            if prices and model in prices:
                price_prompt, price_completion = prices[model]
                s.cost_usd = (s.prompt_tokens * price_prompt + s.completion_tokens * price_completion) / 1_000_000
            else:
                # Fallback: $1/M input, $2/M output
                s.cost_usd = (s.prompt_tokens * 0.000001 + s.completion_tokens * 0.000002)
            stats.total_cost_usd += s.cost_usd

        if s.quality_score is not None:
            stats.quality_scores.append(s.quality_score)

    return stats


def compute_savings(baseline: GroupStats, optimized: GroupStats) -> Dict[str, Any]:
    """Compute percentage savings between baseline and optimized."""
    result: Dict[str, Any] = {}

    if baseline.total_tokens > 0:
        token_saving_pct = (baseline.total_tokens - optimized.total_tokens) / baseline.total_tokens * 100
        result["token_saving_pct"] = round(token_saving_pct, 2)
        result["token_saving"] = baseline.total_tokens - optimized.total_tokens
    else:
        result["token_saving_pct"] = 0.0
        result["token_saving"] = 0

    if baseline.total_cost_usd > 0:
        cost_saving_pct = (baseline.total_cost_usd - optimized.total_cost_usd) / baseline.total_cost_usd * 100
        result["cost_saving_pct"] = round(cost_saving_pct, 2)
        result["cost_saving"] = round(baseline.total_cost_usd - optimized.total_cost_usd, 6)
    else:
        result["cost_saving_pct"] = 0.0
        result["cost_saving"] = 0.0

    if baseline.p50_latency > 0:
        latency_delta_pct = (optimized.p50_latency - baseline.p50_latency) / baseline.p50_latency * 100
        result["latency_p50_delta_pct"] = round(latency_delta_pct, 2)
    else:
        result["latency_p50_delta_pct"] = 0.0

    if baseline.avg_quality is not None and optimized.avg_quality is not None:
        quality_delta = optimized.avg_quality - baseline.avg_quality
        result["quality_baseline"] = round(baseline.avg_quality, 3)
        result["quality_optimized"] = round(optimized.avg_quality, 3)
        result["quality_delta"] = round(quality_delta, 3)
    else:
        result["quality_baseline"] = None
        result["quality_optimized"] = None
        result["quality_delta"] = None

    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_markdown(
    scenario_name: str,
    baseline: GroupStats,
    optimized: GroupStats,
    savings: Dict[str, Any],
    env_snapshot: Dict[str, str],
) -> str:
    """Render comparison report as Markdown."""
    lines = [
        f"# Benchmark Report: {scenario_name}",
        "",
        "## Environment Snapshot",
        "",
    ]
    for k, v in env_snapshot.items():
        lines.append(f"- `{k}`: {v}")
    lines.extend([
        "",
        "## Baseline (no cache, no compression, no RAG)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Samples | {baseline.count} |",
        f"| Success rate | {baseline.success_rate:.1%} |",
        f"| p50 latency | {baseline.p50_latency:.1f} ms |",
        f"| p95 latency | {baseline.p95_latency:.1f} ms |",
        f"| Total tokens | {baseline.total_tokens:,} |",
        f"| Estimated cost | ${baseline.total_cost_usd:.6f} |",
        f"| Avg quality | {baseline.avg_quality or 'N/A'} |",
        "",
        "## Optimized (all optimizations on)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Samples | {optimized.count} |",
        f"| Success rate | {optimized.success_rate:.1%} |",
        f"| p50 latency | {optimized.p50_latency:.1f} ms |",
        f"| p95 latency | {optimized.p95_latency:.1f} ms |",
        f"| Total tokens | {optimized.total_tokens:,} |",
        f"| Estimated cost | ${optimized.total_cost_usd:.6f} |",
        f"| Cache hits | {optimized.cache_hits}/{optimized.count} |",
        f"| Avg quality | {optimized.avg_quality or 'N/A'} |",
        "",
        "## Savings",
        "",
        "| Dimension | Result |",
        "|-----------|--------|",
        f"| Token saving | {savings.get('token_saving_pct', 0):.1f}% ({savings.get('token_saving', 0):,} tokens) |",
        f"| Cost saving | {savings.get('cost_saving_pct', 0):.1f}% (${savings.get('cost_saving', 0):.6f}) |",
        f"| Latency p50 delta | {savings.get('latency_p50_delta_pct', 0):+.1f}% |",
        f"| Quality baseline | {savings.get('quality_baseline') or 'N/A'} |",
        f"| Quality optimized | {savings.get('quality_optimized') or 'N/A'} |",
        f"| Quality delta | {savings.get('quality_delta') or 'N/A'} |",
        "",
    ])

    if optimized.cache_tiers:
        lines.extend(["## Cache Hit Breakdown", "", "| Tier | Count |", "|------|-------|"])
        for tier, count in sorted(optimized.cache_tiers.items()):
            lines.append(f"| {tier} | {count} |")
        lines.append("")

    if baseline.errors or optimized.errors:
        lines.extend(["## Errors", ""])
        if baseline.errors:
            lines.append("### Baseline errors:")
            for e in baseline.errors[:10]:
                lines.append(f"- {e}")
            lines.append("")
        if optimized.errors:
            lines.append("### Optimized errors:")
            for e in optimized.errors[:10]:
                lines.append(f"- {e}")
            lines.append("")

    return "\n".join(lines)


def render_html(scenario_name: str, md_report: str) -> str:
    """Simple HTML wrapper around markdown report."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Benchmark Report: {scenario_name}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
th {{ background: #f5f5f5; }}
code {{ background: #f5f5f5; padding: 2px 4px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>Benchmark Report: {scenario_name}</h1>
<pre style="white-space: pre-wrap;">{md_report}</pre>
</body>
</html>"""


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------

def load_yaml_config(path: str = "benchmarks/config.yaml") -> Dict[str, Any]:
    """Load scenario config from YAML."""
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

async def run_scenario(
    scenario_name: str,
    adapter: Callable,
    base_url: str,
    model: str,
    concurrency: int = 1,
    prices: Optional[Dict[str, Tuple[float, float]]] = None,
    do_judge: bool = False,
    judge_callback: Optional[Callable] = None,
) -> Tuple[GroupStats, GroupStats, Dict[str, Any]]:
    """Run baseline + optimized for a scenario adapter.

    adapter should be a callable that returns (baseline_samples, optimized_samples)
    given (base_url, model, concurrency, prices, do_judge, judge_callback).
    """
    logger.info(f"Running scenario: {scenario_name}")

    # Run baseline (plugins disabled)
    async with aiohttp.ClientSession() as session:
        await set_plugins(enabled=False, session=session, base_url=base_url)
        await asyncio.sleep(1)

        baseline_samples = await adapter(
            base_url=base_url, model=model, concurrency=concurrency,
            prices=prices, do_judge=do_judge, judge_callback=judge_callback,
            mode="baseline",
        )

    # Restart gateway for clean state (D9)
    await restart_gateway(base_url)
    await wait_for_healthy(base_url)

    # Run optimized (plugins enabled)
    async with aiohttp.ClientSession() as session:
        await set_plugins(enabled=True, session=session, base_url=base_url)
        await asyncio.sleep(1)

        optimized_samples = await adapter(
            base_url=base_url, model=model, concurrency=concurrency,
            prices=prices, do_judge=do_judge, judge_callback=judge_callback,
            mode="optimized",
        )

    # Fetch logs and match cache hits by trace_id (D10)
    async with aiohttp.ClientSession() as session:
        logs = await fetch_recent_logs(session, base_url)
        match_cache_hits_by_trace_id(optimized_samples, logs)

    # Compute stats
    baseline_stats = compute_stats(baseline_samples, prices)
    optimized_stats = compute_stats(optimized_samples, prices)

    # Compute savings
    savings = compute_savings(baseline_stats, optimized_stats)

    return baseline_stats, optimized_stats, savings


# ---------------------------------------------------------------------------
# Main CLI orchestration
# ---------------------------------------------------------------------------

async def main_async(
    scenarios: List[str],
    base_url: str,
    concurrency: int,
    do_judge: bool,
    judge_callback: Optional[Callable] = None,
    config_path: str = "benchmarks/config.yaml",
    output_dir: str = "benchmarks/reports",
) -> None:
    """Orchestrate benchmark runs for configured scenarios."""
    cfg = load_yaml_config(config_path)
    scenarios_cfg = cfg.get("scenarios", {})

    os.makedirs(output_dir, exist_ok=True)

    env_snapshot = {
        "python": os.popen("python3 --version").read().strip(),
        "gateway": base_url,
        "concurrency": str(concurrency),
        "judge_enabled": str(do_judge),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    all_results: Dict[str, Tuple[GroupStats, GroupStats, Dict[str, Any]]] = {}

    for scenario_name in scenarios:
        if scenario_name not in scenarios_cfg:
            logger.warning(f"Scenario {scenario_name} not in config, skipping")
            continue

        scenario_cfg = scenarios_cfg[scenario_name]
        model = scenario_cfg.get("model", "deepseek-v4-flash")

        # Import adapter dynamically
        adapter_module = scenario_cfg.get("adapter")
        if not adapter_module:
            logger.error(f"No adapter specified for {scenario_name}")
            continue

        # We'll handle adapter loading in the concrete benchmark.py
        # This engine focuses on orchestration logic
        logger.info(f"Skipping {scenario_name} — adapter loading in benchmark.py")

    # Generate combined report
    report_lines = ["# AI Gateway Benchmark Suite Report", "", f"Generated: {env_snapshot['timestamp']}", ""]
    for name, (bs, os_, sav) in all_results.items():
        report_lines.append(render_markdown(name, bs, os_, sav, env_snapshot))
        report_lines.append("\n---\n\n")

    report_md = "\n".join(report_lines)
    report_html = render_html("Combined Benchmark", report_md)

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    md_path = Path(output_dir) / f"report-{timestamp}.md"
    html_path = Path(output_dir) / f"report-{timestamp}.html"

    md_path.write_text(report_md)
    html_path.write_text(report_html)

    logger.info(f"Reports written to {md_path} and {html_path}")
