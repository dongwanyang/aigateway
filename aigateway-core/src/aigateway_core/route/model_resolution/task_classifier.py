"""Task-profile classification used by intent and model routing.

The deterministic classifier is intentionally conservative.  It handles
high-signal requests and is also the local fallback when the LLM classifier is
unavailable.  Ambiguous requests should be sent to ``IntentClassifier``'s LLM
path instead of pretending a keyword score is calibrated confidence.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

TASKS = ("coding", "reasoning", "summary", "vision", "general")
MODALITIES = ("text", "image", "audio", "video")
REQUIREMENTS = (
    "vision",
    "tool_calling",
    "structured_output",
    "long_context",
)


@dataclass(frozen=True)
class TaskProfile:
    """Multi-dimensional description consumed by policy and routing."""

    operation: str = "general"
    domain: str = "general"
    modalities: tuple[str, ...] = ("text",)
    complexity: int = 50
    requirements: tuple[str, ...] = ()
    confidence: float = 0.5
    source: str = "heuristic"
    signals: tuple[str, ...] = ()

    @property
    def task(self) -> str:
        """Compatibility alias for the original single-label classifier."""
        return self.operation

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("modalities", "requirements", "signals"):
            data[key] = list(data[key])
        data["task"] = self.operation
        return data

    @classmethod
    def from_dict(cls, value: Any, *, source: str = "llm") -> TaskProfile:
        if not isinstance(value, dict):
            raise ValueError("task_profile must be an object")

        operation = str(value.get("operation", value.get("task", "general"))).lower()
        if operation not in TASKS:
            raise ValueError(f"unsupported operation: {operation}")

        domain = str(value.get("domain", "general")).strip().lower() or "general"
        raw_modalities = value.get("modalities", ["text"])
        if not isinstance(raw_modalities, list):
            raise ValueError("task_profile.modalities must be a list")
        modalities = tuple(
            dict.fromkeys(str(item).lower() for item in raw_modalities if str(item).lower() in MODALITIES)
        ) or ("text",)

        raw_requirements = value.get("requirements", [])
        if not isinstance(raw_requirements, list):
            raise ValueError("task_profile.requirements must be a list")
        requirements = tuple(
            dict.fromkeys(
                str(item).lower()
                for item in raw_requirements
                if str(item).lower() in REQUIREMENTS
            )
        )

        if isinstance(value.get("complexity", 50), bool) or isinstance(
            value.get("confidence", 0.5), bool
        ):
            raise ValueError("task_profile complexity/confidence must be numeric")
        try:
            complexity = int(value.get("complexity", 50))
            confidence = float(value.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise ValueError("task_profile complexity/confidence must be numeric") from exc

        return cls(
            operation=operation,
            domain=domain[:64],
            modalities=modalities,
            complexity=max(0, min(100, complexity)),
            requirements=requirements,
            confidence=max(0.0, min(1.0, confidence)),
            source=source,
        )


# Backward-compatible name imported by existing callers/tests.
TaskClassification = TaskProfile


class TaskClassifier:
    """Conservative local task profiler with no external I/O."""

    _PATTERNS: dict[str, Sequence[tuple[str, str, int]]] = {
        "coding": (
            ("code_block", r"```", 6),
            ("coding_zh", r"(写|修改|修复|重构|调试|解释|审查).{0,12}(代码|函数|程序|接口|SQL)", 5),
            ("coding_en", r"\b(write|debug|fix|refactor|implement|review).{0,24}\b(code|function|class|sql|api)\b", 5),
            ("language", r"\b(python|javascript|typescript|java|golang|rust|c\+\+|bash)\b", 3),
        ),
        "summary": (
            ("summary_zh", r"(总结|摘要|概括|提炼|列出.{0,6}要点)", 6),
            ("summary_en", r"\b(summarize|summary|tl;dr|key points|condense)\b", 6),
        ),
        "reasoning": (
            ("reasoning_zh", r"(推理|证明|论证|分析|比较|求解|方程|逻辑|为什么|数学题)", 4),
            ("reasoning_en", r"\b(reason|analy[sz]e|compare|solve|prove|derive|step[- ]by[- ]step|why|logic|theorem|equation)\b", 4),
        ),
        "vision": (
            ("vision_zh", r"(这张图|图片中|图里|截图|识图|视觉)", 4),
            ("vision_en", r"\b(this image|in the image|screenshot|diagram|visual|ocr)\b", 4),
        ),
    }

    def classify(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        structured_output: bool = False,
    ) -> TaskProfile:
        text, modalities = self._request_features(messages)
        scores: dict[str, int] = {task: 0 for task in TASKS}
        signals: dict[str, list[str]] = {task: [] for task in TASKS}

        if "image" in modalities:
            scores["vision"] += 5
            signals["vision"].append("visual_content")

        for task, patterns in self._PATTERNS.items():
            for name, pattern, weight in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    scores[task] += weight
                    signals[task].append(name)

        best_task = max(TASKS[:-1], key=lambda item: scores[item])
        best_score = scores[best_task]
        requirements: list[str] = []
        if "image" in modalities:
            requirements.append("vision")
        if tools:
            requirements.append("tool_calling")
        if structured_output:
            requirements.append("structured_output")

        if best_score == 0:
            return TaskProfile(
                modalities=modalities,
                requirements=tuple(requirements),
                confidence=0.35,
                signals=("no_specific_signal",),
            )

        ordered = sorted((scores[t] for t in TASKS[:-1]), reverse=True)
        margin = best_score - ordered[1]
        # This is deliberately capped below "certain"; only the LLM path can
        # provide a high-confidence semantic classification.
        confidence = min(0.88, 0.55 + best_score * 0.035 + margin * 0.02)
        domain = "software" if scores["coding"] else "general"
        complexity = min(90, 35 + best_score * 5)
        return TaskProfile(
            operation=best_task,
            domain=domain,
            modalities=modalities,
            complexity=complexity,
            requirements=tuple(requirements),
            confidence=round(confidence, 2),
            signals=tuple(signals[best_task]),
        )

    def is_high_confidence(self, profile: TaskProfile, threshold: float = 0.8) -> bool:
        return profile.confidence >= threshold

    @staticmethod
    def _request_features(messages: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
        texts: list[str] = []
        modalities: list[str] = ["text"]
        for message in messages or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", ""))
                if block_type in {"text", "input_text"}:
                    texts.append(str(block.get("text", "")))
                elif block_type in {"image", "image_url", "input_image"}:
                    modalities.append("image")
                elif block_type in {"audio", "input_audio"}:
                    modalities.append("audio")
                elif block_type in {"video", "video_url", "input_video"}:
                    modalities.append("video")
        return "\n".join(texts[-3:]).lower(), tuple(dict.fromkeys(modalities))
