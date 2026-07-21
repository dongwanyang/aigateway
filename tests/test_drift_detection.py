"""Tests for benchmarks/compare_to_expected.py — T6: Drift detection."""

from __future__ import annotations

import pytest
from pathlib import Path

from benchmarks.compare_to_expected import (
    compute_deviation,
    parse_expected_results,
    parse_current_results,
)


class TestComputeDeviation:
    def test_no_deviation(self):
        assert compute_deviation(100.0, 100.0) == 0.0

    def test_within_tolerance(self):
        assert compute_deviation(100.0, 90.0) == 10.0

    def test_exceeds_tolerance(self):
        assert compute_deviation(100.0, 80.0) == 20.0

    def test_expected_zero_actual_nonzero(self):
        assert compute_deviation(0.0, 10.0) == 100.0

    def test_both_zero(self):
        assert compute_deviation(0.0, 0.0) == 0.0


class TestParseExpectedResults:
    def test_parse_valid_file(self, tmp_path: Path):
        expected_file = tmp_path / "expected_results.md"
        expected_file.write_text("""# Expected Results

token_saving_pct: 35.0
cost_saving_pct: 32.0
quality_baseline: 4.1
quality_optimized: 4.2
quality_delta: +0.1
cache_hit_rate: 0.25
""")
        metrics = parse_expected_results(str(expected_file))
        assert metrics["token_saving_pct"] == 35.0
        assert metrics["cost_saving_pct"] == 32.0
        assert metrics["quality_baseline"] == 4.1
        assert metrics["quality_optimized"] == 4.2
        assert metrics["cache_hit_rate"] == 0.25

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_expected_results("/nonexistent/path.md")

    def test_empty_file_returns_empty(self, tmp_path: Path):
        expected_file = tmp_path / "empty.md"
        expected_file.write_text("")
        metrics = parse_expected_results(str(expected_file))
        assert metrics == {}


class TestParseCurrentResults:
    def test_parse_markdown_report(self, tmp_path: Path):
        report = tmp_path / "report.md"
        report.write_text("""# Benchmark Report

| Dimension | Result |
|-----------|--------|
| Token saving | 33.5% (335 tokens) |
| Cost saving | 30.2% ($0.001234) |
| Latency p50 delta | +2.1% |
| Quality baseline | 4.0 |
| Quality optimized | 4.3 |
| Cache hits | 25/100 |
""")
        metrics = parse_current_results(str(report))
        assert metrics["token_saving_pct"] == 33.5
        assert metrics["quality_baseline"] == 4.0
        assert metrics["quality_optimized"] == 4.3
        assert metrics["cache_hit_rate"] == 0.25

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_current_results("/nonexistent/report.md")
