# AI Gateway Benchmark Suite

Self-verifying benchmark suite for measuring token savings, quality, and observability.

## Quick Start

```bash
# One-click run all text scenarios
./benchmarks/run_benchmark.sh

# With multimedia (slow, ~30+ min)
./benchmarks/run_benchmark.sh --with-media

# With LLM-as-judge quality scoring
./benchmarks/run_benchmark.sh --judge

# Dry run (validate setup only)
./benchmarks/run_benchmark.sh --dry-run
```

## Prerequisites

- Gateway running on `http://localhost:8000`
- `AI_GATEWAY_ADMIN_KEY` environment variable set
- Python 3.12+ with dependencies installed

## Scenarios

| Scenario | Description | Default | Time |
|----------|-------------|---------|------|
| `text_qa` | SQuAD + HotpotQA + synthetic QA | ✅ | ~2 min |
| `long_conversation` | Multi-turn conversations | ✅ | ~3 min |
| `multimedia_gen` | Image/video generation | ❌ (opt-in) | ~30+ min |

## Output

Reports are written to `benchmarks/reports/`:
- `report-YYYY-MM-DD.md` — Markdown comparison table
- `report-YYYY-MM-DD.html` — HTML visualization

## Drift Detection

After running benchmarks, check against expected snapshot:

```bash
python benchmarks/compare_to_expected.py \
  --current benchmarks/reports/report-$(date +%Y-%m-%d).md
```

Exit codes:
- `0` = within ±15% tolerance
- `1` = drift exceeds threshold (review required)
- `2` = missing files or malformed data

## Expected Results

See `benchmarks/expected_results.md` for the reference snapshot.

**Important:** The snapshot is NEVER auto-updated. When results drift ≤15%, keep the old snapshot. When drift >15%, review and manually update after confirming the new numbers are correct.

## Dataset Generation

To regenerate sample datasets:

```bash
python benchmarks/scripts/sample_datasets.py --seed 42 --output benchmarks/datasets/
```

## CI Integration

GitHub Actions workflow at `.github/workflows/benchmark.yml` runs on PRs to `main`.

## Configuration

Edit `benchmarks/config.yaml` to change:
- Model selections per scenario
- Per-model pricing
- Judge API settings
- Gateway connection details

## Architecture

```
benchmarks/
├── engine.py              # Core engine (stats, caching, reports)
├── judge.py               # LLM-as-judge quality scorer
├── benchmark.py           # CLI orchestrator
├── config.yaml            # Scenario registry
├── compare_to_expected.py # Drift detection
├── scenarios/             # Scenario adapters
│   ├── text_qa.py
│   ├── long_conversation.py
│   └── multimedia_gen.py
├── scripts/               # Utility scripts
│   └── sample_datasets.py
├── datasets/              # Sample data (gitignored in production)
└── reports/               # Generated reports (gitignored)
```

## Design Decisions

- **D9**: Restart gateway between baseline/optimized instead of flush_cache (L1/L2 have no API)
- **D10**: Match cache hits by trace_id for exact attribution
- **D12**: Sequential execution (concurrency=1) for deterministic results
- **D14**: Images in CI, video local-only
- **D15**: Snapshot never auto-updates on pass

## Performance Notes

- Baseline run: plugins disabled, no cache/compression/RAG
- Optimized run: all plugins enabled
- Savings = (baseline - optimized) / baseline × 100%
- Quality scoring uses deepseek-v4-flash at temperature=0, double judging averaged
