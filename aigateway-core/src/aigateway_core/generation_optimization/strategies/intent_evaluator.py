"""
IntentEvaluatorStrategy — 意图评估策略
=======================================

分析生成请求的复杂度，根据多个维度为请求打分 0-100。

评估维度:
- subject_count: 主体数量 (1=低, 2+=高)
- interaction_type: 物理交互类型 (none/contact/dynamic)
- camera_movement: 镜头运动类型 (static/pan/tracking)
- target_resolution: 目标分辨率

需求: 2.1, 2.3
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from aigateway_core.generation_optimization.config import ModelRouterConfig
from aigateway_core.generation_optimization.models import ComplexityEvaluation
from aigateway_core.media.types import MediaContent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 关键词集合
# ---------------------------------------------------------------------------

# 动态交互关键词（高复杂度物理交互）
_DYNAMIC_INTERACTION_KEYWORDS: List[str] = [
    "碰撞",
    "打斗",
    "爆炸",
    "interact",
    "collision",
    "fight",
    "explode",
    "explosion",
    "battle",
    "combat",
]

# 接触交互关键词（中等复杂度物理交互）
_CONTACT_INTERACTION_KEYWORDS: List[str] = [
    "接触",
    "握手",
    "touch",
    "handshake",
    "hug",
    "拥抱",
    "hold",
    "grab",
    "抓",
    "牵手",
]

# 跟踪镜头关键词
_TRACKING_CAMERA_KEYWORDS: List[str] = [
    "跟踪",
    "tracking",
    "follow",
    "跟随",
    "追踪",
]

# 平移镜头关键词
_PAN_CAMERA_KEYWORDS: List[str] = [
    "平移",
    "pan",
    "扫",
    "横摇",
    "sweep",
    "slide",
]


class IntentEvaluatorStrategy:
    """意图评估器 — 根据多个维度为生成请求打分.

    评估是纯计算操作（无 I/O），通过关键词匹配和参数分析
    快速判断请求复杂度。

    Attributes:
        config: 模型路由配置，包含评估超时等参数
    """

    def __init__(self, config: ModelRouterConfig) -> None:
        """初始化意图评估器.

        Args:
            config: ModelRouterConfig 实例，包含评估相关配置
        """
        self.config = config

    def evaluate(
        self,
        prompt: str,
        reference_images: List[MediaContent],
        generation_params: Dict[str, Any],
    ) -> ComplexityEvaluation:
        """评估生成请求复杂度.

        分析 prompt 和生成参数，基于四个维度综合打分。
        该方法为纯计算，不涉及异步 I/O。

        评估维度与权重:
        - subject_count: 1 主体=0, 2 主体=15, 3+ 主体=30
        - interaction_type: none=0, contact=15, dynamic=30
        - camera_movement: static=0, pan=10, tracking=20
        - target_resolution: ≤512px=0, ≤1024px=10, >1024px=20

        Args:
            prompt: 用户提示词（可能已被 AI Director 优化过）
            reference_images: 参考图列表
            generation_params: 生成参数字典，可包含 target_resolution 等

        Returns:
            ComplexityEvaluation 包含 score (0-100), factors 字典, recommended_model
        """
        # 计算各维度分数
        subject_score = self._evaluate_subject_count(prompt)
        interaction_score = self._evaluate_interaction_type(prompt)
        camera_score = self._evaluate_camera_movement(prompt)
        resolution_score = self._evaluate_resolution(generation_params)

        # 加权求和
        total_score = subject_score + interaction_score + camera_score + resolution_score

        # Clamp to [0, 100]
        total_score = max(0, min(100, total_score))

        # 构建 factors 明细
        factors: Dict[str, Any] = {
            "subject_count": subject_score,
            "interaction_type": interaction_score,
            "camera_movement": camera_score,
            "target_resolution": resolution_score,
        }

        logger.debug(
            "intent_evaluator.evaluate: score=%d, factors=%s",
            total_score,
            factors,
        )

        return ComplexityEvaluation(
            score=total_score,
            factors=factors,
            recommended_model="",  # 由 Model Router 填充
        )

    def _evaluate_subject_count(self, prompt: str) -> int:
        """评估主体数量.

        通过分析 prompt 中的逗号分隔、连词（和、与、and）以及
        语义模式来估算主体数量。

        Returns:
            评分: 1 主体=0, 2 主体=15, 3+ 主体=30
        """
        subject_count = self._count_subjects(prompt)

        if subject_count >= 3:
            return 30
        elif subject_count >= 2:
            return 15
        else:
            return 0

    def _count_subjects(self, prompt: str) -> int:
        """从 prompt 中估算主体数量.

        使用多种启发式方法:
        1. 以逗号或中文顿号分割的短语
        2. "和"、"与"、"and" 等连词连接的名词短语
        3. 数字词（"两个"、"三只" 等）

        Returns:
            估算的主体数量，最少为 1
        """
        if not prompt or not prompt.strip():
            return 1

        count = 1  # 至少一个主体

        # 检测中文数量词（如 "两个人"、"三只猫"、"四位"）
        cn_number_pattern = re.compile(
            r"([两三四五六七八九十百千]\s*[个只位条匹头尾棵朵辆])"
        )
        cn_numbers = cn_number_pattern.findall(prompt)
        if cn_numbers:
            # 映射中文数字
            cn_digit_map = {
                "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
                "百": 100, "千": 1000,
            }
            for match in cn_numbers:
                first_char = match[0]
                if first_char in cn_digit_map:
                    count = max(count, cn_digit_map[first_char])

        # 检测英文数字词（如 "two cats", "3 people"）
        en_number_pattern = re.compile(
            r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\s+\w+",
            re.IGNORECASE,
        )
        en_numbers = en_number_pattern.findall(prompt)
        en_digit_map = {
            "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        for match in en_numbers:
            lower_match = match.lower()
            if lower_match in en_digit_map:
                count = max(count, en_digit_map[lower_match])
            elif match.isdigit():
                num = int(match)
                if 2 <= num <= 100:
                    count = max(count, num)

        # 检测连词连接（"A和B"、"A与B"、"A and B"）
        conjunction_pattern = re.compile(r"[和与]|(?:\band\b)", re.IGNORECASE)
        conjunctions = conjunction_pattern.findall(prompt)
        if conjunctions:
            # 连词数量 + 1 = 主体数量（如 "A和B与C" -> 3个主体）
            conjunction_count = len(conjunctions) + 1
            count = max(count, conjunction_count)

        return count

    def _evaluate_interaction_type(self, prompt: str) -> int:
        """评估物理交互类型.

        检测 prompt 中的动态和接触交互关键词。
        优先检测动态交互（分值更高）。

        Returns:
            评分: none=0, contact=15, dynamic=30
        """
        prompt_lower = prompt.lower()

        # 优先检测动态交互（高复杂度）
        for keyword in _DYNAMIC_INTERACTION_KEYWORDS:
            if keyword.lower() in prompt_lower:
                return 30

        # 检测接触交互（中等复杂度）
        for keyword in _CONTACT_INTERACTION_KEYWORDS:
            if keyword.lower() in prompt_lower:
                return 15

        return 0

    def _evaluate_camera_movement(self, prompt: str) -> int:
        """评估镜头运动类型.

        检测 prompt 中的镜头运动关键词。
        优先检测跟踪镜头（分值更高）。

        Returns:
            评分: static=0, pan=10, tracking=20
        """
        prompt_lower = prompt.lower()

        # 优先检测跟踪镜头（高复杂度）
        for keyword in _TRACKING_CAMERA_KEYWORDS:
            if keyword.lower() in prompt_lower:
                return 20

        # 检测平移镜头（中等复杂度）
        for keyword in _PAN_CAMERA_KEYWORDS:
            if keyword.lower() in prompt_lower:
                return 10

        return 0

    def _evaluate_resolution(self, generation_params: Dict[str, Any]) -> int:
        """评估目标分辨率.

        从 generation_params 中获取 target_resolution，
        取宽高中的较大值作为判断依据。

        Args:
            generation_params: 生成参数字典，可包含:
                - target_resolution: Tuple[int, int] 或 List[int] 形式的 (width, height)
                - width / height: 分开指定的宽高

        Returns:
            评分: ≤512px=0, ≤1024px=10, >1024px=20
        """
        max_dimension = self._get_max_resolution(generation_params)

        if max_dimension > 1024:
            return 20
        elif max_dimension > 512:
            return 10
        else:
            return 0

    def _get_max_resolution(self, generation_params: Dict[str, Any]) -> int:
        """从生成参数中提取最大分辨率维度.

        支持多种参数格式:
        - target_resolution: (width, height) 元组/列表
        - width + height: 分开指定

        Returns:
            最大分辨率维度值（像素），默认返回 0
        """
        # 尝试从 target_resolution 获取
        target_res = generation_params.get("target_resolution")
        if target_res is not None:
            if isinstance(target_res, (tuple, list)) and len(target_res) >= 2:
                try:
                    return max(int(target_res[0]), int(target_res[1]))
                except (TypeError, ValueError):
                    pass

        # 尝试从 width/height 分别获取
        width = generation_params.get("width")
        height = generation_params.get("height")
        if width is not None or height is not None:
            try:
                w = int(width) if width is not None else 0
                h = int(height) if height is not None else 0
                return max(w, h)
            except (TypeError, ValueError):
                pass

        return 0
