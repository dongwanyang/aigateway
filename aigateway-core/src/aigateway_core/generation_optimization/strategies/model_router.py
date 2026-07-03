"""
ModelRouterStrategy — 智能模型路由策略
======================================

基于复杂度评分、模态分类和定价信息，从配置的 provider 模型列表中
动态选择最优模型。

路由逻辑:
1. 按 Model_Modality 三大分类筛选（llm/mllm/generative）
2. 按 capability_score >= complexity_score 筛选合格模型
3. 在合格模型中选择价格最低的（动态选择，非固定层级）
4. 支持 routing_hint: "best quality" / "cheapest" / 具体模型名
5. 支持 model_override 绕过路由或拒绝不存在的模型
6. 模型不可用时按 fallback_models 列表降级，跨 provider 降级

需求: 2.2, 2.4, 2.5, 2.6, 2.9, 2.10
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aigateway_core.generation_optimization.config import ModelRouterConfig
from aigateway_core.generation_optimization.exceptions import ModelRoutingError
from aigateway_core.generation_optimization.models import RoutingDecision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部辅助数据结构
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """模型配置信息 — 路由决策所需的模型元数据.

    Attributes:
        name: 模型标识符（如 "agnes-2.0-flash"）
        provider: 所属 provider 名称（如 "agnes"）
        modality: 模态类别 ("llm" | "mllm" | "generative")
        capability_score: 能力评分 (0-100)
        price_per_request: 每次请求的估算价格 (USD)
        fallback_models: 该 provider 分组的降级模型列表
        is_available: 模型当前是否可用
    """

    name: str
    provider: str
    modality: str  # "llm" | "mllm" | "generative"
    capability_score: int  # 0-100
    price_per_request: float
    fallback_models: List[str] = field(default_factory=list)
    is_available: bool = True


# ---------------------------------------------------------------------------
# ModelRouterStrategy
# ---------------------------------------------------------------------------


class ModelRouterStrategy:
    """模型路由器 — 从配置的 provider 模型列表中选择最优模型.

    基于复杂度评分和配置的模型列表动态选择最优模型，支持多种路由模式
    （正常路由、路由提示、模型覆盖）以及故障降级。

    Attributes:
        config: ModelRouterConfig 实例，包含模型能力和模态映射
        providers_config: config.yaml 中 "providers" 节的字典
    """

    def __init__(
        self,
        config: ModelRouterConfig,
        providers_config: Dict[str, Any],
    ) -> None:
        """初始化模型路由器.

        Args:
            config: ModelRouterConfig 实例，包含 model_capabilities 和 model_modalities
            providers_config: config.yaml 中 "providers" 节的原始字典，
                包含各 provider 的 model_grouper（含 pricing 和 fallback_models）
        """
        self.config = config
        self.providers_config = providers_config
        self._model_list: List[ModelConfig] = self._build_model_list()

    async def route(
        self,
        complexity_score: int,
        required_modality: str,
        routing_hint: Optional[str] = None,
        model_override: Optional[str] = None,
        available_models: Optional[List[ModelConfig]] = None,
    ) -> RoutingDecision:
        """执行路由决策.

        根据复杂度评分、模态要求和用户偏好，选择最优模型。

        路由优先级:
        1. model_override: 直接指定模型，绕过一切路由逻辑
        2. routing_hint: 用户偏好（"best quality"/"cheapest"/具体名称）
        3. 正常路由: modality 筛选 → capability 筛选 → 价格排序

        Args:
            complexity_score: 复杂度评分 (0-100)
            required_modality: 所需模态类别 ("llm" | "mllm" | "generative")
            routing_hint: 用户路由提示（可选）
            model_override: 用户指定的模型覆盖（可选）
            available_models: 可用模型配置列表（可选，默认使用内部构建的列表）

        Returns:
            RoutingDecision 包含选中模型、provider、原因和预估成本

        Raises:
            ModelRoutingError: model_override 指定了不存在的模型
        """
        models = available_models if available_models is not None else self._model_list

        # --- 1. model_override 优先 ---
        if model_override:
            return self._handle_override(model_override, complexity_score, models)

        # --- 2. routing_hint 次优先 ---
        if routing_hint:
            decision = self._handle_hint(
                routing_hint, required_modality, complexity_score, models
            )
            if decision is not None:
                return decision

        # --- 3. 正常路由 ---
        return self._handle_normal_routing(
            complexity_score, required_modality, models
        )

    # ------------------------------------------------------------------
    # 内部路由逻辑
    # ------------------------------------------------------------------

    def _handle_override(
        self,
        model_override: str,
        complexity_score: int,
        models: List[ModelConfig],
    ) -> RoutingDecision:
        """处理 model_override 模式.

        如果指定模型存在且可用，直接使用。如果不存在，抛出 ModelRoutingError。
        如果存在但不可用，尝试 fallback。

        Raises:
            ModelRoutingError: 模型不存在于配置中
        """
        target = self._find_model_by_name(model_override, models)

        if target is None:
            raise ModelRoutingError(
                f"指定的模型 '{model_override}' 不存在于配置的 provider 模型列表中"
            )

        # 模型存在但不可用时尝试 fallback
        if not target.is_available:
            fallback = self._try_fallback(target, models)
            if fallback is not None:
                logger.warning(
                    "model_router.override_fallback: model=%s unavailable, "
                    "falling back to %s",
                    model_override,
                    fallback.name,
                )
                return RoutingDecision(
                    selected_model=fallback.name,
                    selected_provider=fallback.provider,
                    reason="fallback",
                    complexity_score=complexity_score,
                    estimated_cost=fallback.price_per_request,
                )
            raise ModelRoutingError(
                f"指定的模型 '{model_override}' 当前不可用且无可用降级模型"
            )

        return RoutingDecision(
            selected_model=target.name,
            selected_provider=target.provider,
            reason="override",
            complexity_score=complexity_score,
            estimated_cost=target.price_per_request,
        )

    def _handle_hint(
        self,
        routing_hint: str,
        required_modality: str,
        complexity_score: int,
        models: List[ModelConfig],
    ) -> Optional[RoutingDecision]:
        """处理 routing_hint 模式.

        支持:
        - "best quality": 在指定模态中选最高 capability 的模型
        - "cheapest": 在指定模态中选最低价格的模型
        - 具体模型名: 直接选择该模型

        Returns:
            RoutingDecision 或 None（hint 无效时回退到正常路由）
        """
        hint_lower = routing_hint.strip().lower()

        # 按模态筛选可用模型
        modality_models = [
            m for m in models
            if m.modality == required_modality and m.is_available
        ]

        if hint_lower == "best quality":
            if not modality_models:
                return None
            best = max(modality_models, key=lambda m: m.capability_score)
            return RoutingDecision(
                selected_model=best.name,
                selected_provider=best.provider,
                reason="hint",
                complexity_score=complexity_score,
                estimated_cost=best.price_per_request,
            )

        if hint_lower == "cheapest":
            if not modality_models:
                return None
            cheapest = min(modality_models, key=lambda m: m.price_per_request)
            return RoutingDecision(
                selected_model=cheapest.name,
                selected_provider=cheapest.provider,
                reason="hint",
                complexity_score=complexity_score,
                estimated_cost=cheapest.price_per_request,
            )

        # hint 是具体模型名
        target = self._find_model_by_name(routing_hint.strip(), models)
        if target is not None and target.is_available:
            return RoutingDecision(
                selected_model=target.name,
                selected_provider=target.provider,
                reason="hint",
                complexity_score=complexity_score,
                estimated_cost=target.price_per_request,
            )

        # hint 无法匹配 → 返回 None，回退到正常路由
        logger.warning(
            "model_router.hint_unresolved: hint=%r, falling back to normal routing",
            routing_hint,
        )
        return None

    def _handle_normal_routing(
        self,
        complexity_score: int,
        required_modality: str,
        models: List[ModelConfig],
    ) -> RoutingDecision:
        """正常路由逻辑.

        步骤:
        1. 按 required_modality 筛选
        2. 按 capability_score >= complexity_score 筛选合格模型
        3. 在合格模型中选择价格最低的
        4. 如果没有合格模型，选择该模态中最高 capability 的模型
        5. 如果选中模型不可用，尝试 fallback
        """
        # Step 1: 按模态筛选
        modality_models = [
            m for m in models if m.modality == required_modality
        ]

        if not modality_models:
            # 无该模态模型，使用默认模型
            logger.warning(
                "model_router.no_modality_match: modality=%s, using default_model=%s",
                required_modality,
                self.config.default_model,
            )
            return self._default_decision(complexity_score)

        # Step 2: 按 capability >= complexity 筛选
        qualified = [
            m for m in modality_models
            if m.capability_score >= complexity_score and m.is_available
        ]

        if qualified:
            # Step 3: 选择价格最低的
            selected = min(qualified, key=lambda m: m.price_per_request)
        else:
            # Step 4: 无合格模型，选最高 capability（含不可用的也参与选择）
            available_modality = [m for m in modality_models if m.is_available]
            if available_modality:
                selected = max(available_modality, key=lambda m: m.capability_score)
            else:
                # 所有该模态模型都不可用，尝试 fallback
                best_unavailable = max(
                    modality_models, key=lambda m: m.capability_score
                )
                fallback = self._try_fallback(best_unavailable, models)
                if fallback is not None:
                    return RoutingDecision(
                        selected_model=fallback.name,
                        selected_provider=fallback.provider,
                        reason="fallback",
                        complexity_score=complexity_score,
                        estimated_cost=fallback.price_per_request,
                    )
                return self._default_decision(complexity_score)

        # Step 5: 选中模型不可用时尝试 fallback
        if not selected.is_available:
            fallback = self._try_fallback(selected, models)
            if fallback is not None:
                return RoutingDecision(
                    selected_model=fallback.name,
                    selected_provider=fallback.provider,
                    reason="fallback",
                    complexity_score=complexity_score,
                    estimated_cost=fallback.price_per_request,
                )
            return self._default_decision(complexity_score)

        return RoutingDecision(
            selected_model=selected.name,
            selected_provider=selected.provider,
            reason="complexity",
            complexity_score=complexity_score,
            estimated_cost=selected.price_per_request,
        )

    # ------------------------------------------------------------------
    # Fallback 降级逻辑
    # ------------------------------------------------------------------

    def _try_fallback(
        self,
        model: ModelConfig,
        all_models: List[ModelConfig],
    ) -> Optional[ModelConfig]:
        """尝试为不可用模型找到可用的降级替代.

        降级策略:
        1. 先按 model.fallback_models 列表顺序尝试（同 provider）
        2. 如果 fallback 列表耗尽，尝试其他 provider 中相同模态且可用的模型
           按 capability_score 降序选择

        Returns:
            可用的降级模型，或 None
        """
        # 1. 按 fallback_models 顺序尝试
        for fb_name in model.fallback_models:
            fb = self._find_model_by_name(fb_name, all_models)
            if fb is not None and fb.is_available:
                return fb

        # 2. 跨 provider 降级: 选同模态、可用、不同 provider 的最高 capability 模型
        cross_provider = [
            m for m in all_models
            if m.modality == model.modality
            and m.is_available
            and m.provider != model.provider
        ]
        if cross_provider:
            return max(cross_provider, key=lambda m: m.capability_score)

        # 3. 同 provider 中其他可用模型（同模态）
        same_provider = [
            m for m in all_models
            if m.modality == model.modality
            and m.is_available
            and m.name != model.name
            and m.provider == model.provider
        ]
        if same_provider:
            return max(same_provider, key=lambda m: m.capability_score)

        return None

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _find_model_by_name(
        self, name: str, models: List[ModelConfig]
    ) -> Optional[ModelConfig]:
        """按名称查找模型配置."""
        for m in models:
            if m.name == name:
                return m
        return None

    def _default_decision(self, complexity_score: int) -> RoutingDecision:
        """生成默认模型路由决策（当所有路由逻辑都无法选择时）."""
        # 尝试从模型列表中找默认模型获取 provider
        default = self._find_model_by_name(self.config.default_model, self._model_list)
        provider = default.provider if default else "unknown"
        cost = default.price_per_request if default else 0.0

        return RoutingDecision(
            selected_model=self.config.default_model,
            selected_provider=provider,
            reason="fallback",
            complexity_score=complexity_score,
            estimated_cost=cost,
        )

    def _build_model_list(self) -> List[ModelConfig]:
        """从 providers_config 和 ModelRouterConfig 构建统一的模型列表.

        合并信息来源:
        - providers_config: 模型名称、provider、pricing、fallback_models
        - config.model_capabilities: 模型能力评分
        - config.model_modalities: 模型模态分类

        未在 model_capabilities 中注册的模型默认 capability_score=50。
        未在 model_modalities 中注册的模型默认 modality="generative"。
        pricing 中未配置的模型使用 prompt 价格作为 price_per_request，
        如果连 prompt 价格也没有则默认 0.0。
        """
        model_list: List[ModelConfig] = []
        seen_models: set = set()

        for provider_name, provider_data in (self.providers_config or {}).items():
            if not isinstance(provider_data, dict):
                continue

            model_groupers = provider_data.get("model_grouper", [])
            if not isinstance(model_groupers, list):
                continue

            for group in model_groupers:
                if not isinstance(group, dict):
                    continue

                group_models = group.get("models", [])
                group_fallbacks = group.get("fallback_models", [])
                group_pricing = group.get("pricing", {})

                if not isinstance(group_models, list):
                    continue
                if not isinstance(group_fallbacks, list):
                    group_fallbacks = []
                if not isinstance(group_pricing, dict):
                    group_pricing = {}

                for model_entry in group_models:
                    # 支持两种格式：
                    # 1. 字符串: "model-name"（向后兼容）
                    # 2. 字典: {"name": "model-name", "modality": "llm", "capability": 80}
                    if isinstance(model_entry, dict):
                        model_name = model_entry.get("name", "")
                        if not model_name:
                            continue
                        # 从 model_entry 读取 modality/capability 覆盖
                        entry_modality = model_entry.get("modality")
                        entry_capability = model_entry.get("capability")
                    elif isinstance(model_entry, str) and model_entry:
                        model_name = model_entry
                        entry_modality = None
                        entry_capability = None
                    else:
                        continue

                    if model_name in seen_models:
                        continue
                    seen_models.add(model_name)

                    # 获取能力评分（优先级：model_entry > config.model_capabilities > default）
                    if entry_capability is not None:
                        try:
                            capability = int(entry_capability)
                        except (TypeError, ValueError):
                            capability = self.config.model_capabilities.get(
                                model_name, self.config.default_capability_score
                            )
                    else:
                        capability = self.config.model_capabilities.get(
                            model_name, self.config.default_capability_score
                        )

                    # 获取模态（优先级：model_entry > config.model_modalities > default "generative"）
                    if entry_modality is not None:
                        modality = entry_modality
                    else:
                        modality = self.config.model_modalities.get(
                            model_name, "generative"
                        )

                    # 获取价格: 使用 pricing 中的 prompt 价格作为 price_per_request
                    price = 0.0
                    model_pricing = group_pricing.get(model_name, {})
                    if isinstance(model_pricing, dict):
                        prompt_price = model_pricing.get("prompt", 0.0)
                        try:
                            price = float(prompt_price)
                        except (TypeError, ValueError):
                            price = 0.0

                    model_list.append(
                        ModelConfig(
                            name=model_name,
                            provider=provider_name,
                            modality=modality,
                            capability_score=capability,
                            price_per_request=price,
                            fallback_models=group_fallbacks,
                            is_available=True,
                        )
                    )

        logger.debug(
            "model_router._build_model_list: built %d models from providers_config",
            len(model_list),
        )
        return model_list

    def get_model_list(self) -> List[ModelConfig]:
        """获取当前构建的模型列表（用于外部检查和测试）."""
        return list(self._model_list)

    def update_model_availability(self, model_name: str, is_available: bool) -> None:
        """更新模型可用性状态.

        当某个模型因故障或维护不可用时，调用此方法标记，
        路由器将自动触发 fallback 逻辑。

        Args:
            model_name: 模型标识符
            is_available: 是否可用
        """
        for m in self._model_list:
            if m.name == model_name:
                m.is_available = is_available
                logger.info(
                    "model_router.availability_update: model=%s, available=%s",
                    model_name,
                    is_available,
                )
                return
