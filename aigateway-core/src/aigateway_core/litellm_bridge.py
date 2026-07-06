"""
LiteLLMBridge — LiteLLM 桥接层
=============================

封装 LiteLLM 的 Router / Completion / CostTracker，
提供统一的下游 LLM 调用接口，支持 fallback 链和重试。

根据 TECH_SPEC.md:
- LiteLLM 1.40+ 统一 OpenAI 兼容接口对接下游多提供商
- 内置 Router, CostTracker, Fallback 链
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


def _emit_bridge_debug(start_monotonic: float, status: str,
                       payload: Optional[Dict[str, Any]] = None) -> None:
    """若 bridge 维度 debug 开关开启,发一条 kind=debug TraceEvent."""
    import time as _time
    from aigateway_core.trace_event import TraceCollector
    collector = TraceCollector.current()
    if collector is None:
        return
    collector.emit_debug(
        stage="bridge", name="litellm_bridge.completion",
        duration_ms=(_time.monotonic() - start_monotonic) * 1000,
        status=status, dimension="bridge", payload=payload or {},
    )


class ProviderCooldownTracker:
    """per-model cooldown 状态跟踪器。

    由 LiteLLMBridge 通过 litellm Router 的 deployment callback 驱动。
    litellm 内部自己也有 cooldown(_filter_cooldown_deployments),这里
    维护一份镜像供 /metrics 与 admin 同步读取(避免每次 /metrics 请求
    调 litellm async API)。

    状态:CLOSED(0)/ OPEN(1),不实现 HALF-OPEN(litellm 无对应概念)。
    """

    def __init__(
        self,
        allowed_fails: int = 5,
        cooldown_time: int = 60,
        long_open_alert_seconds: int = 300,
    ) -> None:
        import threading
        self.allowed_fails = allowed_fails
        self.cooldown_time = cooldown_time
        self.long_open_alert_seconds = long_open_alert_seconds
        # {model_name: {"state": "CLOSED"/"OPEN", "failure_count": int,
        #               "last_failure_time": float, "last_success_time": float,
        #               "cooldown_until": float|None}}
        self._models: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _extract_provider(model: str) -> str:
        """从 model 名提取 provider,如 openai/gpt-4o → openai。"""
        if "/" in model:
            return model.split("/", 1)[0]
        return model

    def _get_or_init(self, model: str) -> Dict[str, Any]:
        if model not in self._models:
            self._models[model] = {
                "state": "CLOSED",
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_success_time": 0.0,
                "cooldown_until": None,
            }
        return self._models[model]

    def on_failure(self, model: str) -> None:
        """记一次失败;累计达 allowed_fails → 转 OPEN。"""
        if not model:
            return
        with self._lock:
            entry = self._get_or_init(model)
            entry["failure_count"] += 1
            entry["last_failure_time"] = time.time()
            if entry["state"] == "CLOSED" and entry["failure_count"] >= self.allowed_fails:
                entry["state"] = "OPEN"
                entry["cooldown_until"] = time.time() + self.cooldown_time
                logger.warning(
                    "cooldown: model=%s → OPEN(连续失败 %d 次,cooldown %ds)",
                    model, entry["failure_count"], self.cooldown_time,
                )
            # long_open 告警:本次失败发生时如果已 OPEN 且时间过长,输出一次 error
            if entry["state"] == "OPEN" and entry["cooldown_until"]:
                open_duration = time.time() - (entry["cooldown_until"] - self.cooldown_time)
                if open_duration >= self.long_open_alert_seconds:
                    logger.error(
                        "cooldown alert: model=%s OPEN 持续 %.0fs 超过阈值 %ds",
                        model, open_duration, self.long_open_alert_seconds,
                    )

    def on_success(self, model: str) -> None:
        """记一次成功;OPEN → CLOSED,或 CLOSED 状态下重置 failure_count。"""
        if not model:
            return
        with self._lock:
            entry = self._get_or_init(model)
            entry["last_success_time"] = time.time()
            if entry["state"] == "OPEN":
                logger.info("cooldown: model=%s → CLOSED(恢复正常)", model)
            entry["state"] = "CLOSED"
            entry["failure_count"] = 0
            entry["cooldown_until"] = None

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """返回所有 model 状态的浅拷贝(供 /admin/health 读)。"""
        with self._lock:
            return {
                m: {
                    "state": e["state"],
                    "state_value": 0 if e["state"] == "CLOSED" else 1,
                    "failure_count": e["failure_count"],
                    "last_failure_time": e["last_failure_time"],
                    "last_success_time": e["last_success_time"],
                    "cooldown_until": e["cooldown_until"],
                }
                for m, e in self._models.items()
            }

    def get_provider_states(self) -> Dict[str, int]:
        """按 provider 聚合状态,任一 model OPEN → provider OPEN。

        供 /metrics 上报 Prometheus circuit_breaker_state gauge。
        """
        with self._lock:
            provider_state: Dict[str, int] = {}
            for m, e in self._models.items():
                p = self._extract_provider(m)
                v = 0 if e["state"] == "CLOSED" else 1
                cur = provider_state.get(p)
                if cur is None or v > cur:
                    provider_state[p] = v
            return provider_state


class LiteLLMBridge:
    """LiteLLM 桥接封装。

    统一管理 downstream LLM 提供商调用，提供:
    - 标准化 completion 请求
    - Fallback 降级链
    - 重试机制
    - 用量追踪 (CostTracker 集成)

    属性:
        router: LiteLLM Router 实例。
        cost_tracker: 成本追踪器。
        config: 下游提供商配置。
        _fallback_chain: 当前请求的 fallback 模型列表。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        Args:
            config: 提供商配置字典，格式见 TECH_SPEC.md config.yaml providers 段。
        """
        self.config = config or {}
        self.router: Any = None
        self.cost_tracker: Any = None
        self._fallback_chain: List[str] = []
        self._model_alias_map: Dict[str, str] = {}  # 裸模型名 -> Router 注册名
        self._model_modalities: Dict[str, List[str]] = {}  # 裸模型名 -> modality 列表
        self._model_pricing: Dict[str, Dict[str, float]] = {}  # litellm_model -> {prompt, completion}
        # auto 模型解析器(可选,由 main.py 注入)
        # bridge 收到 model=='auto' 时用它按 pipeline_kind + complexity 选模型,
        # 让「选哪个模型」的决策在管道链末端而不是入口做。
        self._auto_resolver: Any = None
        # cooldown tracker(在 initialize() 里创建,init 时占位)
        self._cooldown_tracker: Optional[ProviderCooldownTracker] = None

    def set_auto_resolver(self, resolver: Any) -> None:
        """注入 auto 模型解析器(通常是 ModelRouterStrategy 单例)。

        没注入时,收到 model=='auto' 会回落到「取第一个已注册模型」。
        """
        self._auto_resolver = resolver

    async def _resolve_auto(
        self,
        messages: List[Dict[str, Any]],
        pipeline_kind: str,
    ) -> Dict[str, Any]:
        """把 model=='auto' 解析为真实模型名。

        Args:
            messages: 当前请求 messages(已过 PII/media 前置),用于估算 complexity。
            pipeline_kind: "understanding" | "generation",决定候选池模态。

        Returns:
            {"model": <resolved>, "meta": {...}} 或
            {"error": {...}} 表示无可用模型。
        """
        # 按管道确定所需模态
        required_modality = "generative" if pipeline_kind == "generation" else "llm"

        # 估算 complexity(与原 _resolve_auto_model 保持一致)
        full_text_parts: List[str] = []
        for m in messages or []:
            content = m.get("content", "") if isinstance(m, dict) else ""
            if isinstance(content, str):
                full_text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        full_text_parts.append(block.get("text", ""))
        full_text = " ".join(full_text_parts)
        complexity_score = min(100, max(0, len(full_text) // 50))

        if self._auto_resolver is not None:
            try:
                decision = await self._auto_resolver.route(
                    complexity_score=complexity_score,
                    required_modality=required_modality,
                )
                return {
                    "model": decision.selected_model,
                    "meta": {
                        "selected_model": decision.selected_model,
                        "selected_provider": decision.selected_provider,
                        "reason": decision.reason,
                        "estimated_cost": decision.estimated_cost,
                        "pipeline_kind": pipeline_kind,
                    },
                }
            except Exception as exc:
                logger.warning("bridge auto 解析失败,回落到 fallback: %s", exc)

        # Fallback: 取第一个符合模态的已注册模型
        registered = self.get_registered_models()
        for m in registered:
            mods = self._model_modalities.get(m, [])
            if required_modality == "generative":
                if any(x in mods for x in ("generative", "image", "video")):
                    return {"model": m, "meta": {"selected_model": m, "reason": "auto_fallback",
                                                  "pipeline_kind": pipeline_kind}}
            else:
                # understanding: llm / mllm / 未标记的都当理解模型
                if not mods or any(x in mods for x in ("llm", "mllm")):
                    return {"model": m, "meta": {"selected_model": m, "reason": "auto_fallback",
                                                  "pipeline_kind": pipeline_kind}}
        if registered:
            return {"model": registered[0], "meta": {"selected_model": registered[0],
                                                       "reason": "auto_last_resort",
                                                       "pipeline_kind": pipeline_kind}}
        return {"error": {"code": "model_not_found",
                          "message": f"'auto' 无可用 {required_modality} 模型"}}

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self, providers_config: Optional[Dict[str, Any]] = None) -> None:
        """初始化 LiteLLM Router 和 CostTracker。

        从 providers_config 读取各提供商的 api_key / base_url / models 配置。

        Args:
            providers_config: 提供商配置字典。
                              若不提供则使用 self.config。
        """
        providers = providers_config or self.config.get("providers", {})

        try:
            from litellm import Router

            # CostTracker was removed in litellm >= 1.45
            try:
                from litellm import CostTracker
            except ImportError:
                CostTracker = None  # type: ignore[misc,assignment]

            # 构建 LiteLLM Router 需要的 model_list 格式
            model_list = self._build_model_list(providers)
            routing_strategy_config = self._build_routing_strategy(providers)

            # 读取 circuit_breaker 段(向前兼容),映射到 litellm cooldown 参数
            cb_cfg = self.config.get("circuit_breaker", {}) if isinstance(self.config, dict) else {}
            allowed_fails = int(cb_cfg.get("failure_threshold", 5)) if cb_cfg else 5
            cooldown_time = int(cb_cfg.get("recovery_timeout", 60)) if cb_cfg else 60
            long_open_alert = int(cb_cfg.get("long_open_alert_seconds", 300)) if cb_cfg else 300

            # 创建 tracker(供 /metrics 与 /admin/health 同步读)
            self._cooldown_tracker = ProviderCooldownTracker(
                allowed_fails=allowed_fails,
                cooldown_time=cooldown_time,
                long_open_alert_seconds=long_open_alert,
            )

            self.router = Router(
                model_list=model_list,
                routing_strategy=routing_strategy_config,
                num_retries=getattr(self.config, "num_retries", 3)
                if hasattr(self.config, "num_retries")
                else 3,
                allowed_fails=allowed_fails,
                cooldown_time=cooldown_time,
            )

            # 注册 litellm deployment callback,把失败/成功事件转发给 tracker
            # litellm 1.83.7 callback 签名:async def(kwargs, response, start_time, end_time)
            tracker_ref = self._cooldown_tracker

            async def _on_failure(kwargs, response, start_time, end_time):
                try:
                    model = kwargs.get("model", "") or (kwargs.get("litellm_params") or {}).get("model", "")
                    if tracker_ref is not None:
                        tracker_ref.on_failure(model)
                except Exception as exc:
                    logger.warning("cooldown tracker on_failure 异常: %s", exc)

            async def _on_success(kwargs, response, start_time, end_time):
                try:
                    model = kwargs.get("model", "") or (kwargs.get("litellm_params") or {}).get("model", "")
                    if tracker_ref is not None:
                        tracker_ref.on_success(model)
                except Exception as exc:
                    logger.warning("cooldown tracker on_success 异常: %s", exc)

            self.router.deployment_callback_on_failure = _on_failure
            self.router.deployment_callback_on_success = _on_success

            if CostTracker is not None:
                self.cost_tracker = CostTracker()

            logger.info(
                "LiteLLM Router 初始化成功: %d 个模型, 提供商: %s",
                len(model_list),
                list(providers.keys()),
            )

        except ImportError:
            logger.error("litellm 库未安装，Bridge 无法初始化")
            raise
        except Exception as exc:
            logger.error("LiteLLM Router 初始化失败: %s", exc)
            raise

    def _build_model_list(self, providers_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 providers_config 构建 LiteLLM Router model_list。

        格式: [{"model_name": "...", "litellm_params": {...}}, ...]

        参考: https://docs.litellm.com/docs/proxy/fast_chat

        Per-model base_url override:
          每个 model_entry dict 可包含可选的 ``base_url`` 字段。
          若设置（非空字符串），覆盖 provider 级别的 base_url；
          若未设置或为空字符串，继承 providers.<name>.base_url。
          Fallback 模型始终使用 provider 级别 base_url。

        Args:
            providers_config: 提供商配置。

        Returns:
            model_list 列表。
        """
        model_list: List[Dict[str, Any]] = []

        for provider_name, provider_cfg in providers_config.items():
            api_key = provider_cfg.get("api_key", "")
            base_url = provider_cfg.get("base_url", None)
            model_grouper = provider_cfg.get("model_grouper", [])
            num_retries = provider_cfg.get("num_retries", 3)
            retry_after = provider_cfg.get("retry_after", 1000)
            timeout = provider_cfg.get("timeout", 120)

            if model_grouper:
                # 多模型分组模式
                for group in model_grouper:
                    fallback_models = group.get("fallback_models", [])
                    for model_entry in group.get("models", []):
                        # 支持字符串或字典格式；modality 必须为 list
                        model_base_url: Optional[str] = None
                        if isinstance(model_entry, dict):
                            model_name = model_entry.get("name", "")
                            if not model_name:
                                continue
                            # 可选：per-model base_url 覆盖 provider 级别
                            model_base_url = model_entry.get("base_url") or None
                            raw_modality = model_entry.get("modality")
                            if isinstance(raw_modality, list):
                                model_modality = [
                                    str(x) for x in raw_modality if x
                                ] or ["generative"]
                            else:
                                if raw_modality is not None:
                                    logger.warning(
                                        "litellm_bridge: model=%s modality expected list, "
                                        "got %r; defaulting to ['generative']",
                                        model_name,
                                        type(raw_modality).__name__,
                                    )
                                model_modality = ["generative"]
                        elif isinstance(model_entry, str):
                            model_name = model_entry
                            model_modality = ["generative"]
                        else:
                            continue

                        # 记录 modality
                        self._model_modalities[model_name] = model_modality

                        # 生效的 base_url：优先 per-model，回退 provider 级别
                        effective_base_url = model_base_url or base_url

                        if effective_base_url:
                            # OpenAI 兼容提供商：用 openai/{model_name}，Router 通过 openai 前缀
                            # 选择 OpenAI client，再通过 litellm_params 中的 base_url 路由到具体地址
                            litellm_model = f"openai/{model_name}"
                        else:
                            # 标准提供商：用 {provider_name}/{model_name}
                            litellm_model = f"{provider_name}/{model_name}"
                        # 建立裸模型名 -> 完整名的映射
                        self._model_alias_map[model_name] = litellm_model

                        entry = {
                            "model_name": litellm_model,
                            "litellm_params": {
                                "model": litellm_model,
                                "api_key": api_key,
                                "base_url": effective_base_url,
                                "num_retries": num_retries,
                                "retry_after": retry_after,
                                "timeout": timeout,
                            },
                            "fallbacks": fallback_models,
                        }

                        # 注册定价信息到 LiteLLM cost map，抑制告警
                        # 优先使用 config 中的 pricing，未配置则用 placeholder 0 cost
                        self._register_model_pricing(group, litellm_model, effective_base_url, provider_name)

                        model_list.append(entry)

                    # 同时注册 fallback 模型到 alias map 和 model_list
                    for fb_model_name in fallback_models:
                        if fb_model_name in self._model_alias_map:
                            continue  # 已注册，跳过
                        if base_url:
                            fb_litellm_model = f"openai/{fb_model_name}"
                        else:
                            fb_litellm_model = f"{provider_name}/{fb_model_name}"
                        self._model_alias_map[fb_model_name] = fb_litellm_model

                        fb_entry = {
                            "model_name": fb_litellm_model,
                            "litellm_params": {
                                "model": fb_litellm_model,
                                "api_key": api_key,
                                "base_url": base_url,
                                "num_retries": num_retries,
                                "retry_after": retry_after,
                            },
                            "fallbacks": [],
                        }
                        self._register_model_pricing(group, fb_litellm_model, base_url, provider_name)
                        model_list.append(fb_entry)
            else:
                # 单模型模式
                model_name = f"{provider_name}/{provider_name}"
                model_list.append({
                    "model_name": model_name,
                    "litellm_params": {
                        "model": model_name,
                        "api_key": api_key,
                        "base_url": base_url,
                        "num_retries": num_retries,
                        "retry_after": retry_after,
                    },
                })

        return model_list

    def _register_model_pricing(
        self, group: Dict[str, Any], litellm_model: str, base_url: Optional[str], provider_name: str
    ) -> None:
        """注册模型定价到 LiteLLM cost map，抑制 "not in built-in cost map" 告警。

        优先级:
        1. config 中 group.pricing 里该模型的定价
        2. config 中 group.pricing 的默认键 "$default"
        3. placeholder（0 cost）

        同时将定价存入 self._model_pricing 供成本计算使用。
        """
        try:
            import litellm

            pricing = group.get("pricing", {})
            if not pricing:
                pricing = {"$default": {"prompt": 0.0, "completion": 0.0}}

            # 提取裸模型名（必须在查找定价之前）
            bare_model = litellm_model.split("/")[-1] if "/" in litellm_model else litellm_model

            # 尝试用完整模型名、裸模型名、provider名、$default 四种 key 查找定价
            model_price = (
                pricing.get(litellm_model,
                    pricing.get(bare_model,
                        pricing.get(provider_name,
                            pricing.get("$default", {"prompt": 0.0, "completion": 0.0}))))
            )

            # 存储定价供 _estimate_cost 使用（同时用完整名和裸模型名作 key）
            self._model_pricing[litellm_model] = {
                "prompt": model_price.get("prompt", 0.0),
                "completion": model_price.get("completion", 0.0),
            }
            # 也用裸模型名存储，方便 _estimate_cost 查找
            self._model_pricing[bare_model] = self._model_pricing[litellm_model].copy()

            # 确定 litellm_provider：标准提供商用自身前缀，OpenAI 兼容用 openai
            lite_provider = "openai" if base_url else provider_name

            entry = {
                "max_tokens": 128000,
                "input_cost_per_token": model_price.get("prompt", 0.0),
                "output_cost_per_token": model_price.get("completion", 0.0),
                "litellm_provider": lite_provider,
                "mode": "chat",
            }

            litellm.register_model({litellm_model: entry})
        except Exception:
            pass  # 注册失败不影响主流程

    def resolve_model(self, model: str) -> str:
        """将用户传入的裸模型名解析为 Router 注册的完整模型名。

        例如: "agnes-2.0-flash" -> "openai/agnes-2.0-flash"
        """
        if model in self._model_alias_map:
            resolved = self._model_alias_map[model]
            logger.debug("模型名解析: %s -> %s", model, resolved)
            return resolved
        # 已经带前缀或 Router 能识别的名称，原样返回
        return model

    def is_model_registered(self, model: str) -> bool:
        """检查模型是否已在 Router 中注册。

        支持裸模型名和带前缀的完整名。

        Args:
            model: 模型名称（裸名或完整名）。

        Returns:
            模型是否已注册。
        """
        # 裸模型名在 alias map 中
        if model in self._model_alias_map:
            return True
        # 完整名在 alias map 的 values 中
        if model in self._model_alias_map.values():
            return True
        # 检查 Router model_list（兜底）
        if self.router is not None:
            try:
                model_list = self.router.get_model_list()
                registered_names = {m.get("model_name", "") for m in model_list}
                return model in registered_names
            except Exception:
                pass
        return False

    def get_registered_models(self) -> List[str]:
        """返回所有已注册的裸模型名列表。"""
        return list(self._model_alias_map.keys())

    def get_cooldown_status(self) -> Dict[str, Any]:
        """返回所有 model 的 cooldown 状态(供 /admin/health 读)。"""
        if self._cooldown_tracker is None:
            return {}
        return self._cooldown_tracker.get_all_status()

    def get_cooldown_status_by_provider(self) -> Dict[str, int]:
        """按 provider 聚合状态(供 /metrics 上报 Prometheus)。

        Returns:
            {provider: 0|1} 字典,0=CLOSED, 1=OPEN。
        """
        if self._cooldown_tracker is None:
            return {}
        return self._cooldown_tracker.get_provider_states()

    def _build_routing_strategy(self, providers_config: Dict[str, Any]) -> str:
        """构建路由策略配置。

        Args:
            providers_config: 提供商配置。

        Returns:
            路由策略字符串 ("simple-shuffle", "latency-based", "cost-based", ...)。
        """
        router_cfg = self.config.get("plugins", [])
        for plugin in router_cfg:
            if isinstance(plugin, dict) and plugin.get("name") == "model_router":
                strategy = plugin.get("config", {}).get("strategy", "quality")
                strategy_map = {
                    "cost": "cost-based-routing",
                    "speed": "latency-based-routing",
                    "quality": "simple-shuffle",
                }
                return strategy_map.get(strategy, "simple-shuffle")
        return "simple-shuffle"

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------

    async def completion(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        user_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        stop: Optional[Any] = None,
        fallback_chain: Optional[List[str]] = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        pipeline_kind: str = "understanding",
    ) -> Dict[str, Any]:
        """发送聊天补全请求到下游 LLM。

        支持 model=='auto' 的末端解析:bridge 内部按 pipeline_kind 选模型,
        让「选哪个模型」的决策发生在管道链末端而非入口。
        支持重试和 fallback 链——当主模型调用失败时,依次尝试 fallback_chain 中的模型。

        Args:
            messages: 消息列表，OpenAI 格式。
            model: 目标模型名称。传 'auto' 时由 bridge 结合 pipeline_kind 内部解析。
            user_id: 用户 ID，用于用量追踪。
            temperature: 采样温度。
            max_tokens: 最大输出 token 数。
            top_p: Top-p 采样。
            frequency_penalty: 频率惩罚。
            presence_penalty: 存在惩罚。
            stream: 是否流式。
            tools: 工具定义列表。
            tool_choice: 工具选择策略。
            stop: 停止序列。
            fallback_chain: 降级模型列表 [{model, provider}]。
            max_retries: 最大重试次数。
            extra_headers: 额外 HTTP 请求头（用于 trace context 传播等）。
            pipeline_kind: 请求所属管道 "understanding" | "generation",
                仅在 model=='auto' 时用于筛选候选池模态。

        Returns:
            完整的响应字典（OpenAI 格式）。model=='auto' 时 _meta.model_router
            会记录实际选中的模型。
        """
        import time as _time
        _start = _time.monotonic()
        max_retries = max_retries or 3
        attempts = 0
        last_error: Optional[Exception] = None
        fallback_used: List[str] = []

        # ===== model=='auto' 末端解析(总分总架构:让 bridge 决定用哪个模型)=====
        auto_router_meta: Optional[Dict[str, Any]] = None
        if model == "auto":
            resolved = await self._resolve_auto(messages, pipeline_kind)
            if "error" in resolved:
                return {"error": resolved["error"]}
            model = resolved["model"]
            auto_router_meta = resolved.get("meta")
            logger.info("bridge auto 解析: pipeline=%s → model=%s", pipeline_kind, model)

        # 前置校验：检查模型是否已注册
        if not self.is_model_registered(model):
            registered = self.get_registered_models()
            return {
                "error": {
                    "code": "model_not_found",
                    "message": (
                        f"Model '{model}' is not registered. "
                        f"Available models: {registered}. "
                        f"Please add it to config.yaml providers section."
                    ),
                }
            }

        # 构建尝试模型列表：先尝试指定 model，再走 fallback
        candidates = [model]
        if fallback_chain:
            for fb in fallback_chain:
                if isinstance(fb, dict):
                    candidates.append(fb.get("model", ""))
                elif isinstance(fb, str):
                    candidates.append(fb)

        while attempts <= max_retries:
            current_model = candidates[attempts % len(candidates)] if candidates else model

            # 记录 fallback
            if attempts > 0 and current_model != model:
                fallback_used.append(current_model)

            try:
                result = await self._do_completion(
                    messages=messages,
                    model=current_model,
                    user_id=user_id,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    frequency_penalty=frequency_penalty,
                    presence_penalty=presence_penalty,
                    stream=stream,
                    tools=tools,
                    tool_choice=tool_choice,
                    stop=stop,
                    extra_headers=extra_headers,
                )

                # 成功：记录用量，获取本次请求成本
                request_cost = self._track_usage(current_model, result)

                return {
                    "data": result,
                    "_meta": {
                        "routed_to": {
                            "provider": self._extract_provider(current_model),
                            "model": current_model,
                            "fallback_chain": fallback_used,
                        },
                        "cost": request_cost,
                        # auto 解析结果(model=='auto' 时才有,None 表示走的是显式模型)
                        "model_router": auto_router_meta,
                    },
                    "usage": result.get("usage", {}) if isinstance(result, dict) else {},
                }

            except Exception as exc:
                last_error = exc
                attempts += 1
                retry_delay = self.config.get("retry_delay_ms", 1000) / 1000.0

                logger.warning(
                    "模型 %s 调用失败 (尝试 %d/%d): %s",
                    current_model,
                    attempts,
                    max_retries + 1,
                    exc,
                )

                if attempts <= max_retries:
                    await asyncio_sleep(retry_delay * attempts)  # 递增退避

        # 全部重试和 fallback 均失败
        logger.error(
            "所有模型调用均失败: model=%s, 最后错误=%s",
            model,
            last_error,
        )

        return {
            "error": {
                "code": "upstream_timeout",
                "message": f"All models failed: {last_error}",
                "fallback_chain": fallback_used,
            }
        }

    async def _do_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """执行实际的 LiteLLM completion 调用。

        Args:
            messages: 消息列表。
            model: 模型名称。
            **kwargs: 其他 completion 参数。

        Returns:
            解析后的响应字典。

        Raises:
            Exception: 下游调用失败时透传。
        """
        if self.router is None:
            raise RuntimeError("LiteLLM Router 未初始化，请先调用 initialize()")

        # debug emit 测的是本次实际 completion 调用耗时(不含外层 auto 解析/重试退避),
        # 所以 _start 必须在本方法作用域内定义 —— 675/681 行的 _emit_bridge_debug 引用它。
        import time as _time
        _start = _time.monotonic()

        # 解析裸模型名为 Router 注册的全名
        resolved_model = self.resolve_model(model)

        params: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "stream": kwargs.get("stream", False),
        }

        # 透传可选参数
        for key in [
            "temperature", "max_tokens", "top_p",
            "frequency_penalty", "presence_penalty",
            "tools", "tool_choice", "stop",
        ]:
            val = kwargs.get(key)
            if val is not None:
                params[key] = val

        # 透传 extra_headers（用于 trace context 传播）
        extra_headers = kwargs.get("extra_headers")
        if extra_headers:
            params["extra_headers"] = extra_headers

        # 执行请求
        if kwargs.get("stream", False):
            # 流式：逐 chunk 聚合
            chunks: List[str] = []
            response = await self.router.acompletion(**params)
            async for chunk in response:
                chunk_data = chunk.dict() if hasattr(chunk, "dict") else dict(chunk)
                chunks.append(chunk_data)

            # 将流式 chunks 合成为非流式响应格式（因为上层期望字典返回）
            aggregated = self._aggregate_stream_chunks(chunks)
            _emit_bridge_debug(_start, "ok", {"model": model, "stream": True})
            return aggregated

        # 非流式
        response = await self.router.acompletion(**params)
        response_data = response.dict() if hasattr(response, "dict") else dict(response)
        _emit_bridge_debug(_start, "ok", {"model": model, "stream": False})
        return response_data

    def _aggregate_stream_chunks(
        self, chunks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """将流式 chunks 聚合成完整的非流式响应。

        Args:
            chunks: 原始 stream chunk 列表。

        Returns:
            聚合后的非流式响应。
        """
        if not chunks:
            return {"data": {"choices": []}}

        # 取首个 chunk 的基础字段
        first = chunks[0]
        result = {
            "id": first.get("id", ""),
            "object": "chat.completion",
            "created": first.get("created", int(time.time())),
            "model": first.get("model", ""),
            "choices": [],
        }

        # 聚合 content
        content_parts: List[str] = []
        finish_reason = None
        role = None
        tool_calls_map: Dict[int, Any] = {}

        for chunk in chunks:
            choices = chunk.get("choices", [])
            for choice in choices:
                delta = choice.get("delta", {})
                idx = choice.get("index", 0)

                if delta.get("role"):
                    role = delta["role"]

                if delta.get("content"):
                    content_parts.append(delta["content"])

                if delta.get("tool_calls"):
                    for tc in delta["tool_calls"]:
                        tc_idx = tc.get("index", 0)
                        if tc_idx not in tool_calls_map:
                            tool_calls_map[tc_idx] = {
                                "id": tc.get("id", ""),
                                "type": tc.get("type", "function"),
                                "function": {"name": "", "arguments": ""},
                            }
                        func = tool_calls_map[tc_idx]["function"]
                        if tc.get("function", {}).get("name"):
                            func["name"] = tc["function"]["name"]
                        if tc.get("function", {}).get("arguments"):
                            func["arguments"] += tc["function"]["arguments"]

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        content = "".join(content_parts)

        message: Dict[str, Any] = {"role": role or "assistant", "content": content}
        if tool_calls_map:
            message["tool_calls"] = list(tool_calls_map.values())

        result["choices"] = [{
            "index": 0,
            "finish_reason": finish_reason or "stop",
            "message": message,
        }]

        # 用量信息（通常在最后一个 chunk 中）
        last_chunk = chunks[-1]
        usage = last_chunk.get("usage")
        if usage:
            result["usage"] = usage

        return result

    # ------------------------------------------------------------------
    # 用量追踪
    # ------------------------------------------------------------------

    def _track_usage(self, model: str, response: Dict[str, Any]) -> float:
        """记录模型调用的 token 用量和成本。

        Args:
            model: 使用的模型名称。
            response: 响应字典。

        Returns:
            本次请求的成本（美元）。
        """
        try:
            usage = response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            # 累计用量到 cost_tracker（如果可用）
            if self.cost_tracker is not None:
                self.cost_tracker.total_input_tokens += prompt_tokens
                self.cost_tracker.total_output_tokens += completion_tokens
                self.cost_tracker.total_tokens += total_tokens

            # 估算成本（基于 config.yaml 或内置定价表）
            cost = self._estimate_cost(model, total_tokens)

            if self.cost_tracker is not None:
                self.cost_tracker.total_cost += cost

            logger.debug(
                "用量已追踪: model=%s, tokens_in=%d, tokens_out=%d, cost=$%.4f",
                model,
                prompt_tokens,
                completion_tokens,
                cost,
            )
        except Exception as exc:
            logger.warning("用量追踪失败: %s", exc)
        return cost

    def _estimate_cost(self, model: str, total_tokens: int) -> float:
        """根据模型估算成本（美元）。

        优先使用 config.yaml 中的定价（prompt + completion 分开计算），
        未配置则 fallback 到内置定价表。

        Args:
            model: 模型名称。
            total_tokens: 总 token 数。

        Returns:
            估算成本（美元）。
        """
        # 尝试用完整模型名查找 config.yaml 定价
        pricing = self._model_pricing.get(model)
        if pricing:
            # config.yaml 中有定价：prompt 和 completion 分开
            # 由于我们不知道 prompt/completion 各自多少 token，
            # 用 prompt 价格作为基准（偏保守估计）
            return round(total_tokens * pricing.get("prompt", 0.0), 6)

        # Fallback: 内置定价表
        base_model = model.split("/")[-1] if "/" in model else model
        builtin_pricing = {
            "gpt-4o": 0.000005,
            "gpt-4o-mini": 0.00000015,
            "claude-3-5-sonnet": 0.000003,
            "claude-3-haiku": 0.00000025,
            "gemini-1.5-pro": 0.0000025,
            "agnes-2.0-flash": 0.0000005,
        }
        price_per_token = builtin_pricing.get(base_model, 0.000001)
        return round(total_tokens * price_per_token, 6)

    @staticmethod
    def _extract_provider(model: str) -> str:
        """从模型名提取提供商标识。

        Args:
            model: 模型名，如 "openai/gpt-4o" 或 "gpt-4o"。

        Returns:
            提供商标识，如 "openai"。
        """
        if "/" in model:
            return model.split("/")[0]
        # 根据模型名猜测提供商
        model_lower = model.lower()
        if model_lower.startswith("gpt") or model_lower.startswith("davinci"):
            return "openai"
        if model_lower.startswith("claude"):
            return "anthropic"
        if model_lower.startswith("gemini"):
            return "gemini"
        if model_lower.startswith("llama") or model_lower.startswith("ollama"):
            return "ollama"
        return "unknown"

    # ------------------------------------------------------------------
    # 模型列表
    # ------------------------------------------------------------------

    async def list_models(self) -> List[Dict[str, Any]]:
        """列出当前配置的可用模型。

        Returns:
            OpenAI 格式的模型列表。
        """
        if self.router is None:
            return []

        try:
            model_list = self.router.get_model_list()
            result = []
            for m in model_list:
                model_name = m.get("model_name", "")
                model_info = m.get("model_info", {})
                # 去掉 provider 前缀，只保留实际模型名
                bare_model = model_name.split("/")[-1] if "/" in model_name else model_name
                # 从 _model_modalities 获取模态分类
                modality = self._model_modalities.get(bare_model, ["generative"])
                result.append({
                    "id": bare_model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": self._extract_provider(model_name),
                    "modality": modality,
                    **{k: v for k, v in model_info.items() if k not in ("id", "object", "created", "owned_by", "modality")},
                })
            return result
        except Exception as exc:
            logger.error("获取模型列表失败: %s", exc)
            return []

    async def completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        user_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        stop: Optional[Any] = None,
        fallback_chain: Optional[List[str]] = None,
        max_retries: Optional[int] = None,
        pipeline_kind: str = "understanding",
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式发送聊天补全请求到下游 LLM。

        支持 model=='auto':按 pipeline_kind 内部解析,首个 chunk 里 _meta 会带
        model_router 信息(客户端如需 SSE frame 里的选中模型可从此读)。

        Yields:
            每个 chunk 的字典（OpenAI 流式格式）。
        """
        # ===== model=='auto' 末端解析 =====
        if model == "auto":
            resolved = await self._resolve_auto(messages, pipeline_kind)
            if "error" in resolved:
                yield {"error": resolved["error"]}
                return
            model = resolved["model"]
            logger.info("bridge auto 流式解析: pipeline=%s → model=%s", pipeline_kind, model)

        # 前置校验：检查模型是否已注册
        if not self.is_model_registered(model):
            registered = self.get_registered_models()
            yield {
                "error": {
                    "code": "model_not_found",
                    "message": (
                        f"Model '{model}' is not registered. "
                        f"Available models: {registered}. "
                        f"Please add it to config.yaml providers section."
                    ),
                }
            }
            return

        max_retries = max_retries or 3
        attempts = 0
        last_error: Optional[Exception] = None

        candidates = [model]
        if fallback_chain:
            for fb in fallback_chain:
                if isinstance(fb, dict):
                    candidates.append(fb.get("model", ""))
                elif isinstance(fb, str):
                    candidates.append(fb)

        while attempts <= max_retries:
            current_model = candidates[attempts % len(candidates)] if candidates else model
            # 解析裸模型名为 Router 注册的全名
            resolved_model = self.resolve_model(current_model)
            try:
                params: Dict[str, Any] = {
                    "model": resolved_model,
                    "messages": messages,
                    "stream": True,
                }
                for key in ["temperature", "max_tokens", "top_p", "frequency_penalty",
                             "presence_penalty", "tools", "tool_choice", "stop"]:
                    val = locals().get(key)
                    if val is not None:
                        params[key] = val

                if self.router is None:
                    raise RuntimeError("LiteLLM Router not initialized")

                response = await self.router.acompletion(**params)
                async for chunk in response:
                    chunk_data = chunk.dict() if hasattr(chunk, "dict") else dict(chunk)
                    yield chunk_data

                return  # 成功完成
            except Exception as exc:
                last_error = exc
                attempts += 1
                retry_delay = self.config.get("retry_delay_ms", 1000) / 1000.0
                logger.warning("模型 %s 流式调用失败 (尝试 %d/%d): %s", current_model, attempts, max_retries + 1, exc)
                if attempts <= max_retries:
                    await asyncio_sleep(retry_delay * attempts)

        # 全部失败
        yield {"error": {"code": "upstream_timeout", "message": f"All stream models failed: {last_error}"}}

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_cost_summary(self) -> Dict[str, Any]:
        """获取用量成本摘要。

        Returns:
            包含总 token 数和成本的字典。
        """
        if self.cost_tracker is None:
            return {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
            }

        return {
            "total_input_tokens": self.cost_tracker.total_input_tokens,
            "total_output_tokens": self.cost_tracker.total_output_tokens,
            "total_tokens": self.cost_tracker.total_tokens,
            "total_cost": self.cost_tracker.total_cost,
        }


async def asyncio_sleep(seconds: float) -> None:
    """异步睡眠辅助函数。

    Args:
        seconds: 睡眠时间（秒）。
    """
    import asyncio
    await asyncio.sleep(seconds)
