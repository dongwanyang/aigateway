"""Benchmark CLI — main driver.

Usage:
    python benchmarks/benchmark.py --scenarios text_qa long_conversation --with-media
    python benchmarks/benchmark.py --all

Orchestration:
1. Load config.yaml
2. For each scenario: run baseline → restart gateway → run optimized
3. Fetch logs, match cache hits by trace_id
4. Compute stats + savings
5. Optionally run LLM-as-judge on paired responses
6. Write Markdown + HTML reports
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.engine import (
    GroupStats,
    compute_savings,
    load_yaml_config,
    render_html,
    render_markdown,
    run_scenario,
    wait_for_healthy,
)
from benchmarks.judge import run_judge

logger = logging.getLogger("benchmark")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Gateway Benchmark Suite")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["text_qa", "long_conversation"],
        help="Scenarios to run (default: text_qa long_conversation)",
    )
    parser.add_argument("--with-media", action="store_true", help="Include multimedia scenario")
    parser.add_argument("--config", default="benchmarks/config.yaml", help="Config file path")
    parser.add_argument("--output-dir", default="benchmarks/reports", help="Report output directory")
    parser.add_argument("--concurrency", type=int, default=1, help="Request concurrency (CI uses 1)")
    parser.add_argument("--judge", action="store_true", help="Enable LLM-as-judge quality scoring")
    parser.add_argument("--base-url", default=None, help="Gateway base URL (overrides config)")
    parser.add_argument("--dry-run", action="store_true", help="Load datasets only, don't send requests")
    return parser.parse_args()


async def run_single_scenario(
    scenario_name: str,
    scenario_cfg: Dict[str, Any],
    base_url: str,
    prices: Dict[str, Tuple[float, float]],
    do_judge: bool,
    judge_cfg: Dict[str, Any],
    output_dir: str,
) -> Tuple[GroupStats, GroupStats, Dict[str, Any]]:
    """Run one scenario: baseline -> optimized -> report."""
    logger.info(f"=== Running scenario: {scenario_name} ===")

    model = scenario_cfg.get("model", "deepseek-v4-flash")
    adapter_module_path = scenario_cfg.get("adapter")

    if not adapter_module_path:
        raise ValueError(f"Scenario {scenario_name} missing 'adapter' field")

    # Dynamic import of adapter
    module = importlib.import_module(adapter_module_path)
    adapter_fn = getattr(module, f"run_{scenario_name.replace('-', '_')}")

    # Judge callback if enabled
    judge_callback = None
    if do_judge:
        judge_api_url = judge_cfg.get("api_url", "")
        judge_api_key = os.environ.get("JUDGE_API_KEY", "")
        judge_model = judge_cfg.get("model", "deepseek-v4-flash")

        async def _judge_callback(prompt: str, response_a: str, response_b: str) -> Optional[float]:
            return await run_judge(
                prompt=prompt,
                response_a=response_a,
                response_b=response_b,
                judge_api_url=judge_api_url,
                judge_api_key=judge_api_key,
                judge_model=judge_model,
            )

        judge_callback = _judge_callback

    # Run baseline and optimized separately
    baseline_samples = await adapter_fn(
        base_url=base_url,
        model=model,
        concurrency=scenario_cfg.get("concurrency", 1),
        prices=prices,
        do_judge=do_judge,
        judge_callback=judge_callback,
        mode="baseline",
    )

    optimized_samples = await adapter_fn(
        base_url=base_url,
        model=model,
        concurrency=scenario_cfg.get("concurrency", 1),
        prices=prices,
        do_judge=do_judge,
        judge_callback=judge_callback,
        mode="optimized",
    )

    # Compute stats
    from benchmarks.engine import compute_stats, fetch_recent_logs, match_cache_hits_by_trace_id

    baseline_stats = compute_stats(baseline_samples, prices)
    optimized_stats = compute_stats(optimized_samples, prices)

    # Fetch logs and match cache hits
    async with aiohttp.ClientSession() as session:
        logs = await fetch_recent_logs(session, base_url)
        match_cache_hits_by_trace_id(optimized_samples, logs)

    # Recompute with cache hit info
    optimized_stats = compute_stats(optimized_samples, prices)

    # Compute savings
    savings = compute_savings(baseline_stats, optimized_stats)

    # Generate report
    env_snapshot = {
        "scenario": scenario_name,
        "model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gateway": base_url,
        "concurrency": str(scenario_cfg.get("concurrency", 1)),
    }

    md_report = render_markdown(scenario_name, baseline_stats, optimized_stats, savings, env_snapshot)
    html_report = render_html(scenario_name, md_report)

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    md_path = Path(output_dir) / f"{scenario_name}-{timestamp}.md"
    html_path = Path(output_dir) / f"{scenario_name}-{timestamp}.html"

    md_path.write_text(md_report)
    html_path.write_text(html_report)

    logger.info(f"Reports written: {md_path}, {html_path}")

    return baseline_stats, optimized_stats, savings


async def main_async(args: argparse.Namespace) -> int:
    """Main async entry point."""
    cfg = load_yaml_config(args.config)
    scenarios_cfg = cfg.get("scenarios", {})
    pricing = cfg.get("pricing", {})
    judge_cfg = cfg.get("judge", {})
    gateway_cfg = cfg.get("gateway", {})

    base_url = args.base_url or gateway_cfg.get("base_url", "http://localhost:8000")

    # Validate gateway is reachable
    try:
        await wait_for_healthy(base_url, timeout=gateway_cfg.get("health_timeout", 60))
    except Exception as e:
        logger.error(f"Gateway not healthy: {e}")
        return 1

    # Build scenario list
    scenarios_to_run = args.scenarios
    if args.with_media and "multimedia_gen" not in scenarios_to_run:
        scenarios_to_run.append("multimedia_gen")

    # Filter out opt-in scenarios unless explicitly requested or --with-media used
    scenarios_to_run = [
        s for s in scenarios_to_run
        if s in scenarios_cfg and (
            not scenarios_cfg[s].get("opt_in", False) or s == "multimedia_gen" and args.with_media
        )
    ]

    if not scenarios_to_run:
        logger.error("No scenarios to run")
        return 1

    # Dry run: just validate datasets
    if args.dry_run:
        logger.info("Dry run enabled — loading datasets only")
        for name in scenarios_to_run:
            scenario_cfg = scenarios_cfg[name]
            adapter_module_path = scenario_cfg.get("adapter")
            if adapter_module_path:
                module = importlib.import_module(adapter_module_path)
                logger.info(f"✓ {name}: {adapter_module_path} loaded")
        return 0

    # Run scenarios sequentially (D12)
    all_results: Dict[str, Tuple[GroupStats, GroupStats, Dict[str, Any]]] = {}

    for scenario_name in scenarios_to_run:
        scenario_cfg = scenarios_cfg[scenario_name]

        try:
            baseline, optimized, savings = await run_single_scenario(
                scenario_name=scenario_name,
                scenario_cfg=scenario_cfg,
                base_url=base_url,
                prices=pricing,
                do_judge=args.judge,
                judge_cfg=judge_cfg,
                output_dir=args.output_dir,
            )
            all_results[scenario_name] = (baseline, optimized, savings)
        except Exception as e:
            logger.error(f"Scenario {scenario_name} failed: {e}")
            continue

    # Generate combined report
    if all_results:
        combined_md_parts = ["# AI Gateway Benchmark Suite Report", ""]
        for name, (bs, opt, sav) in all_results.items():
            env_snapshot = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            combined_md_parts.append(render_markdown(name, bs, opt, sav, env_snapshot))
            combined_md_parts.append("\n---\n\n")

        combined_md = "\n".join(combined_md_parts)
        combined_html = render_html("Combined Benchmark", combined_md)

        timestamp = time.strftime("%Y-%m-%d", time.gmtime())
        md_path = Path(args.output_dir) / f"report-{timestamp}.md"
        html_path = Path(args.output_dir) / f"report-{timestamp}.html"

        md_path.write_text(combined_md)
        html_path.write_text(combined_html)

        logger.info(f"Combined reports: {md_path}, {html_path}")

    return 0


def main() -> None:
    """Sync entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
