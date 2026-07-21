"""IntentClassifier —— 异步 LLM 意图预判.

调廉价文本模型, 输出固定 JSON {"generation":"...","hint":"..."}.
超时/异常降级到启发式(带图→image, 纯文本→understanding).
预判调用显式传文本模型 + intent=understanding, 不触发智能路由(避免循环依赖).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是一个意图分类器。判断用户最后一条消息的意图, 并判断用户是否指定了特定模型。"
    "只输出一个 JSON, 格式固定: {\"generation\":\"understanding|image|video\",\"hint\":\"<模型名或None>\"}。"
    "generation 取值: understanding(文本理解/对话/推理)、image(图片生成)、video(视频生成)。"
    "hint: 若用户明确要求用某模型则填该模型名, 否则填 \"None\"。"
    "不要输出 JSON 以外的任何文字。"
)


class IntentClassifier:
    """异步 LLM 意图预判, 输出 {generation, hint} JSON."""

    def __init__(
        self,
        bridge: Any,
        model_selector: Any,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._bridge = bridge
        self._model_selector = model_selector
        self._config = config or {}
        self._timeout = float(self._config.get("timeout_seconds", 60))
        self._default_model = self._config.get("model", "agnes-2.0-flash")

    async def classify(
        self,
        messages: List[Dict[str, Any]],
        body_model: Optional[str],
    ) -> Dict[str, Any]:
        """返回 {"generation": str, "hint": str}."""
        try:
            return await asyncio.wait_for(
                self._do_classify(messages, body_model), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            logger.warning("IntentClassifier 超时, 降级启发式")
            return self._heuristic(messages)
        except Exception as exc:
            logger.warning("IntentClassifier 异常 %s, 降级启发式", exc)
            return self._heuristic(messages)

    async def _do_classify(
        self,
        messages: List[Dict[str, Any]],
        body_model: Optional[str],
    ) -> Dict[str, Any]:
        text_model = await self._model_selector.select_text_model()
        user_text = self._extract_last_user_text(messages)
        prompt_msgs = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        response = await self._bridge.completion(
            messages=prompt_msgs,
            model=text_model,
            intent="understanding",
        )
        content = self._extract_content(response)
        return self._parse(content, messages)

    def _extract_last_user_text(self, messages: List[Dict[str, Any]]) -> str:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    parts = []
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text", ""))
                    return " ".join(parts) if parts else "(multimodal content)"
        return ""

    def _extract_content(self, response: Dict[str, Any]) -> str:
        if "error" in response and "data" not in response:
            return ""
        data = response.get("data", response)
        choices = data.get("choices", []) if isinstance(data, dict) else []
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        c = msg.get("content", "")
        return c.strip() if isinstance(c, str) else ""

    def _parse(self, content: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not content:
            return self._heuristic(messages)
        # 抽取第一个 {...} JSON (支持嵌套大括号)
        start = content.find("{")
        if start == -1:
            return self._heuristic(messages)
        depth = 0
        end = -1
        for i in range(start, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return self._heuristic(messages)
        json_str = content[start:end]
        try:
            obj = json.loads(json_str)
        except json.JSONDecodeError:
            return self._heuristic(messages)
        gen = str(obj.get("generation", "")).strip().lower()
        hint = obj.get("hint", "None")
        if gen not in ("understanding", "image", "video"):
            return self._heuristic(messages)
        if hint is None:
            hint = "None"
        return {"generation": gen, "hint": str(hint)}

    # 降级启发式用的生成意图关键词。仅当用户文本明确含生成动词时才判生成;
    # 带图片/视频输入块不再直接判生成 —— "描述这张图"是理解(mllm),不是生成。
    _IMAGE_GEN_KEYWORDS = ("画", "生成图", "生成一张", "生成图片", "draw", "generate image",
                           "create image", "生成图像")
    _VIDEO_GEN_KEYWORDS = ("生成视频", "生成一段视频", "generate video", "create video",
                           "make a video")

    def _heuristic(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """降级启发式: 仅按最后一条 user 文本的生成关键词判定, 带图输入默认 understanding.

        旧实现"带图→image"会把"描述这张图/图里有什么"这类 mllm 理解请求误判为生成,
        错误路由到 _do_image_generation。图片/视频输入块本身不构成生成意图。
        """
        user_text = self._extract_last_user_text(messages).lower()
        if any(kw in user_text for kw in self._VIDEO_GEN_KEYWORDS):
            return {"generation": "video", "hint": "None"}
        if any(kw in user_text for kw in self._IMAGE_GEN_KEYWORDS):
            return {"generation": "image", "hint": "None"}
        return {"generation": "understanding", "hint": "None"}
