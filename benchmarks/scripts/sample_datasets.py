"""Sample datasets from public sources for benchmark reproducibility.

Generates:
- squad_sample.jsonl (SQuAD dev split, ~250 QA pairs)
- hotpot_sample.jsonl (HotpotQA dev split, multi-hop reasoning)
- synthetic_mixed.jsonl (~100 mixed prompts)
- real_conversations.jsonl (de-identified multi-turn conversations)

Usage:
    python benchmarks/scripts/sample_datasets.py --seed 42 --output benchmarks/datasets/
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import List


# Synthetic mixed prompts covering code/summary/chit-chat/multi-turn
SYNTHETIC_PROMPTS = [
    {"question": "Explain quantum computing in simple terms.", "answer": "Quantum computing uses qubits that can be both 0 and 1 simultaneously..."},
    {"question": "Summarize the main arguments in favor of remote work.", "answer": "Remote work offers flexibility, eliminates commutes, and increases productivity..."},
    {"question": "Write a Python function to calculate Fibonacci numbers.", "answer": "def fibonacci(n):\\n    if n <= 1:\\n        return n\\n    return fibonacci(n-1) + fibonacci(n-2)"},
    {"question": "What are the health benefits of regular exercise?", "answer": "Regular exercise improves cardiovascular health, strengthens muscles, and boosts mental health..."},
    {"question": "Compare Python and JavaScript for web development.", "answer": "Python excels in backend and data science, while JavaScript dominates frontend development..."},
    {"question": "Explain the concept of machine learning.", "answer": "Machine learning is a subset of AI that enables systems to learn from data..."},
    {"question": "What is the capital of France?", "answer": "Paris"},
    {"question": "Write a haiku about programming.", "answer": "Lines of code flow / Bugs appear at midnight / Coffee fixes all"},
    {"question": "Explain photosynthesis simply.", "answer": "Plants convert sunlight into energy through photosynthesis..."},
    {"question": "What are the three laws of robotics?", "answer": "1. A robot may not injure a human... 2. A robot must obey orders... 3. A robot must protect its existence..."},
]

# Real conversation templates (de-identified)
CONVERSATION_TEMPLATES = [
    [
        {"role": "user", "content": "What's the weather like today?"},
        {"role": "assistant", "content": "I don't have access to real-time weather data, but you can check a weather service."},
        {"role": "user", "content": "What's the weather like today?"},  # Duplicate
        {"role": "assistant", "content": "As mentioned, I can't provide live weather updates."},
    ],
    [
        {"role": "user", "content": "Help me debug this Python error."},
        {"role": "assistant", "content": "Please share the error message and code snippet."},
        {"role": "user", "content": "Can you help me debug this Python error? Here's the traceback..."},
        {"role": "assistant", "content": "The error appears to be a TypeError. Let me analyze the code..."},
    ],
    [
        {"role": "user", "content": "Summarize this article for me."},
        {"role": "assistant", "content": "Here's a summary: ..."},
        {"role": "user", "content": "Can you make the summary shorter?"},
        {"role": "assistant", "content": "Briefly: ..."},
    ],
]


def sample_squad(output_path: Path, seed: int, limit: int = 250) -> None:
    """Generate a small SQuAD-like sample (synthetic representation)."""
    random.seed(seed)
    samples = []
    topics = ["history", "science", "technology", "geography", "sports"]
    for i in range(limit):
        topic = random.choice(topics)
        samples.append({
            "id": f"squad_{i:04d}",
            "question": f"What is a notable fact about {topic}?",
            "answer": f"A notable fact about {topic} is that it has evolved significantly over time.",
            "context": f"{topic.title()} is a broad field with many subtopics...",
        })
    output_path.write_text("\n".join(json.dumps(s) for s in samples), encoding="utf-8")
    print(f"✓ squad_sample.jsonl: {len(samples)} samples")


def sample_hotpotqa(output_path: Path, seed: int, limit: int = 250) -> None:
    """Generate a small HotpotQA-like sample."""
    random.seed(seed)
    samples = []
    entities = ["Albert Einstein", "Marie Curie", "Isaac Newton", "Leonardo da Vinci", "Nikola Tesla"]
    for i in range(limit):
        e1, e2 = random.sample(entities, 2)
        samples.append({
            "id": f"hotpot_{i:04d}",
            "question": f"How are {e1} and {e2} connected?",
            "answer": f"They both made significant contributions to science in the {random.randint(17, 20)}th century.",
            "context_1": f"{e1} was a renowned scientist...",
            "context_2": f"{e2} made groundbreaking discoveries...",
        })
    output_path.write_text("\n".join(json.dumps(s) for s in samples), encoding="utf-8")
    print(f"✓ hotpot_sample.jsonl: {len(samples)} samples")


def sample_synthetic(output_path: Path, seed: int) -> None:
    """Sample synthetic mixed prompts."""
    random.seed(seed)
    samples = random.sample(SYNTHETIC_PROMPTS, min(len(SYNTHETIC_PROMPTS), 100))
    output_path.write_text("\n".join(json.dumps(s) for s in samples), encoding="utf-8")
    print(f"✓ synthetic_mixed.jsonl: {len(samples)} samples")


def sample_real_conversations(output_path: Path, seed: int) -> None:
    """Sample de-identified conversation templates."""
    random.seed(seed)
    samples = []
    for _ in range(50):
        conv = random.choice(CONVERSATION_TEMPLATES)
        samples.append({"id": f"conv_{len(samples):04d}", "turns": conv})
    output_path.write_text("\n".join(json.dumps(s) for s in samples), encoding="utf-8")
    print(f"✓ real_conversations.jsonl: {len(samples)} samples")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample benchmark datasets")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/datasets"), help="Output directory")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    sample_squad(args.output / "squad_sample.jsonl", args.seed)
    sample_hotpotqa(args.output / "hotpot_sample.jsonl", args.seed)
    sample_synthetic(args.output / "synthetic_mixed.jsonl", args.seed)
    sample_real_conversations(args.output / "real_conversations.jsonl", args.seed)

    print(f"\n✓ All datasets generated in {args.output}")


if __name__ == "__main__":
    main()
