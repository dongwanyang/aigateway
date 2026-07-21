# Expected Benchmark Results Snapshot

**Generated:** 2026-07-21  
**Environment:** Linux, Python 3.12, Gateway :8000  
**Seed:** 42 (datasets)  
**Concurrency:** 1 (CI deterministic)

## Reference Values

| Metric | Value | Notes |
|--------|-------|-------|
| token_saving_pct | 35.0 | Text QA scenarios avg |
| cost_saving_pct | 32.0 | Per-model pricing applied |
| quality_baseline | 4.1 | LLM-as-judge avg (1-5) |
| quality_optimized | 4.2 | LLM-as-judge avg (1-5) |
| quality_delta | +0.1 | Should be ≥0 for "省 token 不降质" |
| cache_hit_rate | 0.25 | L1+L2+L3 combined |
| avg_latency_ms | 850.0 | p50 latency optimized |

## Drift Tolerance

±15% on each metric independently. Exceeding threshold requires manual review before updating this file.

## Update Policy (D15)

This snapshot is **never auto-updated**. When drift exceeds 15%:
1. Review current results for correctness
2. Confirm new numbers reflect real improvement, not regression
3. Manually update values below and commit with explanatory message

## How to Regenerate

```bash
./benchmarks/run_benchmark.sh --judge
python benchmarks/compare_to_expected.py --current benchmarks/reports/report-$(date +%Y-%m-%d).md
```

## Environment Snapshot

```
OS: Linux 6.17.0-1019-aws
Python: 3.12.3
Gateway: http://localhost:8000
Models: deepseek-v4-flash, agnes-image-2.1-flash, agnes-video-2.1
Judge: deepseek-v4-flash @ https://api.deepseek.com/v1/chat/completions
```
