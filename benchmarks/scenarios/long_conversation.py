"""Long conversation scenario adapter.

Tests multi-turn conversations with:
- Repeated/near-duplicate questions (tests L3 semantic cache)
- Long context that triggers prompt compression
- Progressive context growth to validate conv_compressor

Dataset: benchmarks/datasets/real_conversations.jsonl
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


def load_jsonl(path: str) -> List[Dict[str, Any]]:
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


async def run_long_conversation(
    base_url: str,
    model: str,
    concurrency: int,
    prices: Optional[Dict[str, Tuple[float, float]]] = None,
    do_judge: bool = False,
    judge_callback: Optional[Callable] = None,
    mode: str = "baseline",
) -> List[Sample]:
    """Run long conversation benchmark.

    Loads conversation turns from real_conversations.jsonl and builds
    progressively longer multi-turn contexts. Each turn is sent as a
    separate request so we can measure:
    - Token growth per turn
    - Cache hit rate on near-duplicate questions
    - Compression effectiveness on long histories
    """
    dataset_path = Path("benchmarks/datasets/real_conversations.jsonl")
    if not dataset_path.exists():
        logger.error(f"Conversation dataset not found: {dataset_path}")
        return []

    conversations = load_jsonl(str(dataset_path))
    if not conversations:
        logger.error("No conversations loaded")
        return []

    results: List[Sample] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(messages: List[Dict[str, str]], turn_idx: int) -> Sample:
        async with semaphore:
            return await one_request(
                session=session,
                base_url=base_url,
                prompt=json.dumps(messages),
                model=model,
            )

    # Use a single aiohttp session across all requests
    async with aiohttp.ClientSession() as session:
        for conv in conversations[:10]:  # Limit to 10 conversations for runtime
            turns = conv.get("turns", [])
            if len(turns) < 2:
                continue

            # Build progressive context: each turn adds one more exchange
            history: List[Dict[str, str]] = []
            for turn in turns[:5]:  # Max 5 turns per conversation
                if "user" in turn:
                    history.append({"role": "user", "content": turn["user"]})
                if "assistant" in turn:
                    history.append({"role": "assistant", "content": turn["assistant"]})

                if len(history) >= 2:  # At least user+assistant pair
                    sample = await _run_one(
                        messages=history,
                        turn_idx=len(history),
                    )
                    sample.prompt = f"[{len(history)} turns] {turn.get('user', '')[:100]}"
                    results.append(sample)

    logger.info(f"Long conversation: ran {len(results)} multi-turn requests")
    return results
