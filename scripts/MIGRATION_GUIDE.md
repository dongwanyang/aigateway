# Migration Guide: scripts/ab_test.py → benchmarks/

`scripts/ab_test.py` has been deprecated in favor of the new benchmark suite at `benchmarks/`.

## Why?

The old `ab_test.py` had critical issues that the new suite fixes:
- **D9**: Only flushed L3 cache, leaving L1/L2 contaminated between baseline and optimized runs
- **D10**: Matched cache hits by time-order approximation instead of trace_id
- **D12**: Used concurrent execution, introducing non-determinism
- No quality scoring (LLM-as-judge)
- No YAML scenario registry
- No drift detection against expected results

## Quick Migration

### Old way (deprecated)
```bash
python scripts/ab_test.py --model deepseek-v4-flash --concurrency 10
```

### New way
```bash
# Run text QA scenarios
./benchmarks/run_benchmark.sh

# With quality scoring
./benchmarks/run_benchmark.sh --judge

# Include multimedia (slow)
./benchmarks/run_benchmark.sh --with-media

# Dry run (validate setup)
./benchmarks/run_benchmark.sh --dry-run
```

## Feature Comparison

| Feature | Old `ab_test.py` | New `benchmarks/` |
|---------|------------------|-------------------|
| Cache reset | L3 only (L1/L2 contaminated) | Gateway restart (clean state) |
| Cache hit matching | Time-order approximation | trace_id exact match |
| Concurrency | Configurable (non-deterministic) | Sequential (deterministic) |
| Quality scoring | None | LLM-as-judge (optional) |
| Scenario config | Hardcoded | YAML registry |
| Drift detection | None | compare_to_expected.py |
| Reports | Markdown only | Markdown + HTML |
| Dataset management | Built-in 20 prompts | Public datasets + synthetic |
| Multimedia support | None | Image/video (opt-in) |

## Key Changes

### 1. Plugin Toggle

Old:
```python
PLUGINS_TO_TOGGLE = ["prompt_cache", "semantic_cache", "prompt_compress"]
```

New (`benchmarks/engine.py`):
```python
PLUGINS_TO_TOGGLE = ["prompt_cache", "semantic_cache", "prompt_compress", "rag_retriever"]
```

### 2. Baseline Reset

Old: `flush_cache()` (only L3)

New: `restart_gateway()` (full clean state)

### 3. Cache Hit Matching

Old: Time-based approximation
```python
def match_cache_hits_from_logs(samples, logs):
    # Matches by order, not trace_id
```

New: trace_id exact match
```python
def match_cache_hits_by_trace_id(samples, logs):
    # Builds trace_id map, exact attribution
```

### 4. Quality Scoring

New optional feature using LLM-as-judge:
```bash
./benchmarks/run_benchmark.sh --judge
```

Scores responses 1-5 on helpfulness, accuracy, clarity. Double judging averaged for determinism.

## Running Locally

### Prerequisites
- Gateway running on `http://localhost:8000`
- `AI_GATEWAY_ADMIN_KEY` environment variable
- Python 3.12+ with dependencies

### Basic Run
```bash
cd /path/to/aigateway
./benchmarks/run_benchmark.sh
```

### Custom Configuration
Edit `benchmarks/config.yaml`:
```yaml
scenarios:
  text_qa:
    model: deepseek-v4-flash
    concurrency: 1
    do_judge: true
```

### Check Drift
```bash
python benchmarks/compare_to_expected.py \
  --current benchmarks/reports/report-$(date +%Y-%m-%d).md
```

## Expected Results

See `benchmarks/expected_results.md` for the reference snapshot.

**Important:** The snapshot is NEVER auto-updated. When drift exceeds 15%, review manually and update the file.

## Tests

```bash
# All benchmark tests
python3 -m pytest tests/test_benchmark_engine.py tests/test_benchmark_judge.py tests/test_multimedia_gen.py tests/test_drift_detection.py -v

# Just engine (T1-T2)
python3 -m pytest tests/test_benchmark_engine.py -v

# Just judge (T3)
python3 -m pytest tests/test_benchmark_judge.py -v

# Just multimedia (T4)
python3 -m pytest tests/test_multimedia_gen.py -v

# Just drift detection (T6)
python3 -m pytest tests/test_drift_detection.py -v
```

## File Structure

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
├── datasets/              # Sample data (gitignored)
└── reports/               # Generated reports (gitignored)
```

## Questions?

See `benchmarks/README.md` for full documentation.
