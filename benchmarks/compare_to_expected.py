"""Drift detection: compare current benchmark results against expected_results.md snapshot.

D15: Snapshot is NEVER auto-updated on pass. Human must confirm before updating.
Exit codes:
  0 = within tolerance (<=15% deviation), do NOT update snapshot
  1 = drift exceeds threshold (>15%), fail the check
  2 = expected_results.md missing or malformed
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 15.0


def parse_expected_results(path: str) -> Dict[str, float]:
    """Parse expected_results.md and extract key metrics as floats."""
    result_path = Path(path)
    if not result_path.exists():
        raise FileNotFoundError(f"Expected results file not found: {result_path}")

    content = result_path.read_text(encoding="utf-8")
    metrics: Dict[str, float] = {}

    # Pattern: metric_name: value% or metric_name: value
    patterns = [
        (r"token_saving_pct:\s*([\d.]+)", "token_saving_pct"),
        (r"cost_saving_pct:\s*([\d.]+)", "cost_saving_pct"),
        (r"quality_baseline:\s*([\d.]+)", "quality_baseline"),
        (r"quality_optimized:\s*([\d.]+)", "quality_optimized"),
        (r"quality_delta:\s*([\d.+-]+)", "quality_delta"),
        (r"avg_latency_ms:\s*([\d.]+)", "avg_latency_ms"),
        (r"cache_hit_rate:\s*([\d.]+)", "cache_hit_rate"),
    ]

    for pattern, name in patterns:
        match = re.search(pattern, content)
        if match:
            try:
                metrics[name] = float(match.group(1))
            except ValueError:
                logger.warning(f"Could not parse metric '{name}': {match.group(1)}")

    return metrics


def parse_current_results(path: str) -> Dict[str, float]:
    """Parse a generated Markdown report and extract key metrics."""
    result_path = Path(path)
    if not result_path.exists():
        raise FileNotFoundError(f"Current results file not found: {result_path}")

    content = result_path.read_text(encoding="utf-8")
    metrics: Dict[str, float] = {}

    # Parse from markdown tables
    token_match = re.search(r"Token saving[^|]*\|\s*([\d.]+)%", content)
    if token_match:
        metrics["token_saving_pct"] = float(token_match.group(1))

    cost_match = re.search(r"Cost saving[^|]*\|\s*([\d.]+)%", content)
    if cost_match:
        metrics["cost_saving_pct"] = float(cost_match.group(1))

    quality_base_match = re.search(r"Quality baseline[^|]*\|\s*([\d.]+)", content)
    if quality_base_match:
        metrics["quality_baseline"] = float(quality_base_match.group(1))

    quality_opt_match = re.search(r"Quality optimized[^|]*\|\s*([\d.]+)", content)
    if quality_opt_match:
        metrics["quality_optimized"] = float(quality_opt_match.group(1))

    latency_match = re.search(r"p50 latency[^|]*\|\s*([\d.]+)\s*ms", content)
    if latency_match:
        metrics["avg_latency_ms"] = float(latency_match.group(1))

    cache_match = re.search(r"Cache hit[^|]*\|\s*(\d+)/(\d+)", content)
    if cache_match:
        denom = int(cache_match.group(2))
        if denom > 0:
            metrics["cache_hit_rate"] = int(cache_match.group(1)) / denom

    return metrics


def compute_deviation(expected: float, actual: float) -> float:
    """Compute absolute percentage deviation."""
    if expected == 0:
        return 0.0 if actual == 0 else 100.0
    return abs(actual - expected) / abs(expected) * 100


def compare(
    expected_path: str,
    current_path: str,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> Tuple[bool, Dict[str, float], Dict[str, float]]:
    """Compare current results against expected snapshot.

    Returns:
        (within_threshold, expected_metrics, current_metrics)
    Raises:
        FileNotFoundError: if either file is missing
        ValueError: if critical metrics are missing from both files
    """
    expected = parse_expected_results(expected_path)
    current = parse_current_results(current_path)

    if not expected:
        raise ValueError(f"No measurable metrics found in {expected_path}")
    if not current:
        raise ValueError(f"No measurable metrics found in {current_path}")

    deviations: Dict[str, float] = {}
    all_within = True

    for key in expected:
        if key not in current:
            logger.warning(f"Metric '{key}' missing from current results — skipping")
            continue
        dev = compute_deviation(expected[key], current[key])
        deviations[key] = dev
        if dev > threshold_pct:
            all_within = False
            logger.warning(
                f"DRIFT: {key} expected={expected[key]}, actual={current[key]}, "
                f"deviation={dev:.1f}% (threshold={threshold_pct}%)"
            )

    return all_within, expected, current


def print_report(
    expected: Dict[str, float],
    current: Dict[str, float],
    deviations: Dict[str, float],
    threshold_pct: float,
) -> None:
    """Print a human-readable drift report."""
    print("\n=== Benchmark Drift Report ===")
    print(f"Threshold: ±{threshold_pct}%\n")
    print(f"{'Metric':<25} {'Expected':>12} {'Actual':>12} {'Deviation':>12} {'Status':>10}")
    print("-" * 72)

    for key in expected:
        if key not in deviations:
            continue
        exp = expected[key]
        act = current.get(key, "N/A")
        dev = deviations[key]
        status = "OK" if dev <= threshold_pct else "DRIFT"
        act_str = f"{act:.3f}" if isinstance(act, float) else str(act)
        print(f"{key:<25} {exp:>12.3f} {act_str:>12} {dev:>11.1f}% {status:>10}")

    print()
    drifted_keys = [k for k, v in deviations.items() if v > threshold_pct]
    if drifted_keys:
        print(f"⚠️  DRIFT DETECTED in {len(drifted_keys)} metric(s): {', '.join(drifted_keys)}")
        print("   Update expected_results.md manually after reviewing.")
    else:
        print("✓ All metrics within tolerance. Snapshot NOT updated (D15).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark drift detector")
    parser.add_argument(
        "--expected",
        default="benchmarks/expected_results.md",
        help="Path to expected results snapshot",
    )
    parser.add_argument(
        "--current",
        required=True,
        help="Path to current benchmark report (.md)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_PCT,
        help=f"Drift threshold percentage (default: {DEFAULT_THRESHOLD_PCT})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        within, expected, current = compare(args.expected, args.current, args.threshold)
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return 2

    deviations: Dict[str, float] = {}
    for key in expected:
        if key in current:
            deviations[key] = compute_deviation(expected[key], current[key])

    print_report(expected, current, deviations, args.threshold)

    return 0 if within else 1


if __name__ == "__main__":
    sys.exit(main())
