#!/bin/bash
# One-click benchmark runner
# Usage: ./benchmarks/run_benchmark.sh [--with-media] [--judge]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "=== AI Gateway Benchmark Suite ==="
echo "Working directory: $PROJECT_ROOT"
echo ""

# Check prerequisites
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

if [ ! -f "config.yaml" ]; then
    echo "ERROR: config.yaml not found in project root"
    exit 1
fi

# Activate test venv if available
if [ -f ".test-venv/bin/activate" ]; then
    source .test-venv/bin/activate
    echo "✓ Test venv activated"
else
    echo "⚠ No .test-venv found, using system Python"
fi

# Build arguments
ARGS=(
    benchmarks/benchmark.py
    --scenarios text_qa long_conversation
    --config benchmarks/config.yaml
    --output-dir benchmarks/reports
    --concurrency 1
)

if [ "${1:-}" = "--with-media" ]; then
    ARGS+=(--with-media)
    echo "✓ Including multimedia scenario"
fi

if [ "${1:-}" = "--judge" ] || [ "${2:-}" = "--judge" ]; then
    ARGS+=(--judge)
    echo "✓ Enabling LLM-as-judge quality scoring"
fi

if [ "${1:-}" = "--dry-run" ]; then
    ARGS+=(--dry-run)
    echo "✓ Dry run mode — datasets only"
fi

# Run benchmark
echo ""
echo "Running: python3 ${ARGS[*]}"
echo ""

python3 "${ARGS[@]}"

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "=== Benchmark completed successfully ==="
    echo "Reports:"
    ls -la benchmarks/reports/*.md 2>/dev/null | tail -5
else
    echo ""
    echo "=== Benchmark failed with exit code $EXIT_CODE ==="
fi

exit $EXIT_CODE
