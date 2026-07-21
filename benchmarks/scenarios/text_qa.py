"""Text QA scenario adapter.

Loads datasets from benchmarks/datasets/*.jsonl:
- squad_sample.jsonl (SQuAD dev split, ~250 QA pairs)
- hotpot_sample.jsonl (HotpotQA dev split, multi-hop reasoning)
- synthetic_mixed.jsonl (~100 mixed prompts)

Returns baseline + optimized sample lists.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

from benchmarks.engine import Sample, one_request

logger = logging.getLogger(__name__)


def load_jsonl(path: str) -> List[Dict[str, str]]:
    """Load JSONL dataset file."""
    samples = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    samples.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping invalid JSON in {path}: {e}")
    return samples


def get_dataset_paths() -> List[str]:
    """Get all dataset file paths."""
    dataset_dir = Path("benchmarks/datasets")
    if not dataset_dir.exists():
        logger.warning(f"Dataset directory {dataset_dir} does not exist")
        return []
    return sorted(str(p) for p in dataset_dir.glob("*.jsonl"))


async def run_text_qa(
    base_url: str,
    model: str,
    concurrency: int,
    prices: Optional[Dict[str, Tuple[float, float]]] = None,
    do_judge: bool = False,
    judge_callback: Optional[Callable] = None,
    mode: str = "baseline",
) -> List[Sample]:
    """Run text QA benchmark across all datasets."""
    dataset_paths = get_dataset_paths()
    if not dataset_paths:
        logger.error("No dataset files found in benchmarks/datasets/")
        return []

    # Load all prompts
    all_prompts: List[str] = []
    for path in dataset_paths:
        try:
            data = load_jsonl(path)
            for item in data:
                prompt = item.get("question") or item.get("prompt") or item.get("text", "")
                if prompt:
                    all_prompts.append(prompt)
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")

    if not all_prompts:
        logger.error("No prompts loaded from datasets")
        return []

    logger.info(f"Loaded {len(all_prompts)} prompts from {len(dataset_paths)} datasets")

    # Execute requests
    semaphore = asyncio.Semaphore(concurrency)
    results: List[Sample] = []

    async with aiohttp.ClientSession() as session:
        async def _run_one(prompt: str) -> Sample:
            async with semaphore:
                return await one_request(
                    session=session,
                    base_url=base_url,
                    prompt=prompt,
                    model=model,
                )

        tasks = [_run_one(p) for p in all_prompts]
        results = await asyncio.gather(*tasks)

    return list(results)
