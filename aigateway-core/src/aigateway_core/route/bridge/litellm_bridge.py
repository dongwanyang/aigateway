"""LiteLLMBridge — LiteLLM 桥接层 (route layer home).

Part of the unified route layer (``aigateway_core.route.bridge``).

Moved here from the root ``aigateway_core/litellm_bridge.py`` as part of the
runtime structure refactor (Task 4). Behavior is unchanged.

封装 LiteLLM 的 Router / Completion / CostTracker，
提供统一的下游 LLM 调用接口，支持 fallback 链和重试。

根据 TECH_SPEC.md:
- LiteLLM 1.40+ 统一 OpenAI 兼容接口对接下游多提供商
- 内置 Router, CostTracker, Fallback 链
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .cooldown import ProviderCooldownTracker

logger = logging.getLogger(__name__)

# Intent → required capability mapping (used by _resolve_by_intent + completion/completion_stream)
_INTENT_TO_CAPABILITY = {
    "understanding": "text",
    "generation:image": "image",
    "generation:video": "video",
}


def _emit_bridge_debug(start_monotonic: float, status: str,
                       payload: Optional[Dict[str, Any]] = None) -> None:
    """Bridge 的 stage 事件已由 dispatcher._emit_stage 在 completion 路径发出,
    不需要额外发 kind=debug 事件 —— 否则 trace 里同一操作出现两行(stage+debug)。
    保留此 stub 以便后续需要时通过 stage 事件的 payload 字段查看 bridge 信息。
    """


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
        self._model_capabilities: Dict[str, List[str]] = {}  # 裸模型名 -> capabilities 列表
        self._model_pricing: Dict[str, Dict[str, float]] = {}  # litellm_model -> {prompt, completion}
        # auto 模型解析器(可选,由 main.py 注入)
        # 留作后续复杂度评分接入点; 当前 _resolve_by_intent 不依赖它
        # (ModelRouterStrategy 内部仍用旧 llm/mllm/generative 分类, 见 Task 9)。
        self._auto_resolver: Any = None
        # cooldown tracker(在 initialize() 里创建,init 时占位)
        self._cooldown_tracker: Optional[ProviderCooldownTracker] = None

    def set_auto_resolver(self, resolver: Any) -> None:
        """注入 auto 模型解析器(通常是 ModelRouterStrategy 单例)。

        留作后续复杂度评分接入点; 当前 intent-based 解析不依赖它。
        """
        self._auto_resolver = resolver

    async def _resolve_by_intent(
        self,
        intent: str,
        model_hint: Optional[str],
    ) -> Dict[str, Any]:
        """按意图对应能力过滤候选池, 选最佳模型.

        Args:
            intent: "understanding" | "generation:image" | "generation:video"
            model_hint: 客户端/预判指定的模型名(裸名), 可为 None.
                若在候选池内则优先选它; 否则忽略.

        Returns:
            {"model": <resolved>, "meta": {...}} 或 {"error": {...}} (池空).

        Note: 不调 _auto_resolver —— ModelRouterStrategy 内部仍用旧 llm/mllm/generative
        分类(见 Task 9 才迁移 capabilities), 取值与本函数的 text/image/video 不匹配,
        调用会选不到模型。池内选首 + hint 优先即可; 复杂度评分留作后续接入点。
        """
        required_capability = _INTENT_TO_CAPABILITY.get(intent, "text")

        registered = self.get_registered_models()
        pool = [
            m for m in registered
            if required_capability in self._model_capabilities.get(m, ["text"])
        ]
        if not pool:
            return {
                "error": {
                    "code": "no_model_for_intent",
                    "message": f"No model with capability '{required_capability}' for intent '{intent}'",
                }
            }

        # hint 在池内 -> 优先
        if model_hint and model_hint in pool:
            return {
                "model": model_hint,
                "meta": {"selected_model": model_hint, "reason": "hint_matched",
                          "intent": intent},
            }

        # 无 hint 或 hint 不在池内 -> 取池首(后续可接 intent_evaluator 评分)
        return {
            "model": pool[0],
            "meta": {"selected_model": pool[0], "reason": "pool_first",
                      "intent": intent},
        }

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

            # 注册 litellm deployment callback,把失败/成功事件转发给 tracker。
            # 直接覆盖 Router 实例上的方法不生效,因为 Router.__init__ 已把它的
            # 绑定方法注册进 litellm.{success,failure,_async_*}_callback 列表。
            # 正确做法:往 litellm 全局 callback 列表 append 我们的回调,这样
            # litellm 每次成功/失败时会依次调用我们与 Router 的回调。
            # litellm 1.83.7 callback 签名:(kwargs, response, start_time, end_time)
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

            import litellm as _litellm
            if not isinstance(_litellm._async_failure_callback, list):
                _litellm._async_failure_callback = []
            if not isinstance(_litellm._async_success_callback, list):
                _litellm._async_success_callback = []
            # 只 append 一次(避免热重载/多 bridge 场景重复注册)
            if _on_failure not in _litellm._async_failure_callback:
                _litellm._async_failure_callback.append(_on_failure)
            if _on_success not in _litellm._async_success_callback:
                _litellm._async_success_callback.append(_on_success)

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
                        # 支持字符串或字典格式；capabilities 必须为 list
                        model_base_url: Optional[str] = None
                        if isinstance(model_entry, dict):
                            model_name = model_entry.get("name", "")
                            if not model_name:
                                continue
                            # 可选：per-model base_url 覆盖 provider 级别
                            model_base_url = model_entry.get("base_url") or None
                            raw_caps = model_entry.get("capabilities")
                            if isinstance(raw_caps, list):
                                model_caps = [
                                    str(x) for x in raw_caps if x
                                ] or ["text"]
                            else:
                                if raw_caps is not None:
                                    logger.warning(
                                        "litellm_bridge: model=%s capabilities expected list, "
                                        "got %r; defaulting to ['text']",
                                        model_name,
                                        type(raw_caps).__name__,
                                    )
                                model_caps = ["text"]
                        elif isinstance(model_entry, str):
                            model_name = model_entry
                            model_caps = ["text"]
                        else:
                            continue

                        # 记录 capabilities
                        self._model_capabilities[model_name] = model_caps

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
        intent: str = "understanding",
        model_hint: Optional[str] = None,
        # 过渡别名:Task 6 之前 dispatcher 仍传 pipeline_kind=,Task 6 后移除。
        pipeline_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送聊天补全请求到下游 LLM。

        支持按意图解析模型:bridge 内部按 intent 选模型,
        让「选哪个模型」的决策发生在管道链末端而非入口。
        支持重试和 fallback 链——当主模型调用失败时,依次尝试 fallback_chain 中的模型。

        Args:
            messages: 消息列表，OpenAI 格式。
            model: 目标模型名称。作为 hint;若不具备 intent 所需 capability 则忽略,走池解析。
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
            intent: 请求意图 "understanding" | "generation:image" | "generation:video",
                决定候选池所需 capability。
            model_hint: 预判/客户端指定的模型名(裸名),在候选池内则优先选它。

        Returns:
            完整的响应字典（OpenAI 格式）。_meta.model_router
            会记录实际选中的模型。
        """
        import time as _time
        _start = _time.monotonic()
        max_retries = max_retries or 3
        attempts = 0
        last_error: Optional[Exception] = None
        fallback_used: List[str] = []

        # 过渡兼容:Task 6 之前 dispatcher 仍传 pipeline_kind= 而非 intent=。
        # 把旧值映射到 intent; 显式 intent= 优先,仅当 intent 取默认值且 pipeline_kind 显式传入时采用 pipeline_kind。
        _PIPELINE_KIND_TO_INTENT = {
            "understanding": "understanding",
            "generation": "generation:image",
            "generation:video": "generation:video",
        }
        if pipeline_kind is not None and intent == "understanding":
            intent = _PIPELINE_KIND_TO_INTENT.get(pipeline_kind, "understanding")

        # ===== 按意图解析模型(取消 auto 魔法字符串; 客户端 model 作 hint)=====
        auto_router_meta: Optional[Dict[str, Any]] = None
        required_capability = _INTENT_TO_CAPABILITY.get(intent, "text")

        explicit_model = model if (model and model != "auto") else None
        # 显式模型已注册 AND 具备本次意图所需 capability -> 直连(内部调用/合法 hint 走此路径)
        if explicit_model and self.is_model_registered(explicit_model) \
                and required_capability in self._model_capabilities.get(explicit_model, []):
            # 不触发智能路由(预判/ai_director 内部调用也走此路径)
            pass
        else:
            # 显式模型不具备所需能力(如传 text 模型却意图 image) -> 忽略它作 hint, 走池解析
            hint = model_hint or explicit_model
            resolved = await self._resolve_by_intent(intent=intent, model_hint=hint)
            if "error" in resolved:
                return {"error": resolved["error"]}
            model = resolved["model"]
            auto_router_meta = resolved.get("meta")
            logger.info("bridge 意图解析: intent=%s → model=%s", intent, model)

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

        # ===== 按意图分发: image/video 走专门分支(不调 chat completions)=====
        if intent == "generation:image":
            # 从 messages 抽 prompt(若 ai_director 已改写, 取最后 user 文本)
            prompt_text = self._extract_prompt_from_messages(messages)
            img_result = await self._do_image_generation(
                prompt=prompt_text, model=model, extra_headers=extra_headers,
            )
            # Track usage from image gen response (prompt tokens only, no completion tokens)
            request_cost = self._track_usage(model, img_result)
            return {"data": img_result, "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": request_cost},
                    "usage": img_result.get("usage", {})}
        if intent == "generation:video":
            vid_result = await self._do_video_generation(
                messages=messages, model=model, extra_headers=extra_headers,
            )
            # Track usage from video gen response (usually zero tokens)
            request_cost = self._track_usage(model, vid_result)
            return {"data": vid_result, "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": request_cost},
                    "usage": {}}

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
                        # 意图解析结果(池解析时才有,显式直连时为 None)
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

    async def _do_image_generation(
        self,
        prompt: str,
        model: str,
        size: Optional[str] = None,
        n: int = 1,
        response_format: Optional[str] = None,
        quality: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """调 OpenAI Images API (/images/generations) 生成图片, 归一为 chat completions.

        请求体严格遵循 Agnes Image API 格式（兼容 OpenAI Images API 语义）:
        - model / prompt / size 为顶级参数
        - response_format 放入 extra_body（Agnes 要求）
        - quality / n 非 Agnes 原生参数，忽略
        """
        import httpx

        gen_cfg = (self.config.get("generation", {}) or {}).get("image", {}) or {}
        size = size or gen_cfg.get("default_size", "2K")
        response_format = response_format or gen_cfg.get("response_format", "url")

        base_url, api_key = self._get_model_endpoint(model)
        endpoint = f"{base_url.rstrip('/')}/images/generations"

        # 构建符合 Agnes API 的请求体
        body: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
        }
        if n > 1:
            body["n"] = n
        # response_format 必须放在 extra_body 中
        if response_format:
            body["extra_body"] = {"response_format": response_format}

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()

        data_list = payload.get("data", [])
        content_parts = []
        for item in data_list:
            if item.get("url"):
                content_parts.append(item["url"])
            elif item.get("b64_json"):
                content_parts.append(item["b64_json"])
        content = content_parts[0] if content_parts else ""

        usage = payload.get("usage", {}) or {}
        prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        total = usage.get("total_tokens", prompt_tokens)

        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "total_tokens": total,
            },
        }

    async def _do_video_generation(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        seconds: str = "4",
        size: Optional[str] = None,
        input_reference: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """调 OpenAI Videos API (POST /videos, 异步) 提交任务, 返回含 task_id 的 chat completions."""
        import httpx

        base_url, api_key = self._get_model_endpoint(model)
        endpoint = f"{base_url.rstrip('/')}/videos"

        prompt = self._extract_prompt_from_messages(messages)
        body: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "seconds": seconds,
            "size": size or "720x1280",
        }
        if input_reference:
            body["input_reference"] = input_reference

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()

        video_id = payload.get("id", "")
        content = f"Video generation submitted. id={video_id}, poll /v1/videos/{video_id}"
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def retrieve_video(self, video_id: str) -> Dict[str, Any]:
        """轮询视频任务状态 (GET /videos/{id}), 对应 OpenAI Retrieve a video."""
        import httpx

        # 使用 _get_model_endpoint 找第一个已注册模型的端点（优先 per-model base_url）
        base_url, api_key = self._get_model_endpoint(
            self.get_registered_models()[0] if self.get_registered_models() else "unknown"
        )
        if not base_url:
            return {"error": {"code": "no_provider", "message": "No configured provider found"}}
        endpoint = f"{base_url.rstrip('/')}/videos/{video_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _extract_prompt_from_messages(self, messages: List[Dict[str, Any]]) -> str:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                    if parts:
                        return " ".join(parts)
        return ""

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

    def _get_model_endpoint(self, model: str) -> tuple[str, str]:
        """返回 (base_url, api_key) 供 Images/Video API 直调.

        per-model base_url 优先于 provider 级别（Agnes 图片/视频端点与 chat 不同）。
        """
        bare = model.split("/")[-1] if "/" in model else model
        providers = self.config.get("providers", {}) if isinstance(self.config, dict) else {}
        for provider_cfg in providers.values():
            if not isinstance(provider_cfg, dict):
                continue
            api_key = provider_cfg.get("api_key", "")
            provider_base_url = provider_cfg.get("base_url", "")
            for group in provider_cfg.get("model_grouper", []) or []:
                for m in (group.get("models", []) if isinstance(group, dict) else []):
                    if isinstance(m, dict) and m.get("name") == bare:
                        # per-model base_url 覆盖 provider 级别
                        return (m.get("base_url") or provider_base_url), api_key
        # fallback: 第一个有 base_url 的 provider
        for provider_cfg in providers.values():
            if isinstance(provider_cfg, dict) and provider_cfg.get("base_url"):
                return provider_cfg["base_url"], provider_cfg.get("api_key", "")
        return "", ""

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
                # 从 _model_capabilities 获取能力分类
                modality = self._model_capabilities.get(bare_model, ["text"])
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
        extra_headers: Optional[Dict[str, str]] = None,
        intent: str = "understanding",
        model_hint: Optional[str] = None,
        # 过渡别名:Task 6 之前 dispatcher 仍传 pipeline_kind=,Task 6 后移除。
        pipeline_kind: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式发送聊天补全请求到下游 LLM。

        支持按意图解析:按 intent 内部解析,首个 chunk 里 _meta 会带
        model_router 信息(客户端如需 SSE frame 里的选中模型可从此读)。

        Yields:
            每个 chunk 的字典（OpenAI 流式格式）。
        """
        # 过渡兼容:Task 6 之前 dispatcher 仍传 pipeline_kind= 而非 intent=。
        # 把旧值映射到 intent; 显式 intent= 优先,仅当 intent 取默认值且 pipeline_kind 显式传入时采用 pipeline_kind。
        _PIPELINE_KIND_TO_INTENT = {
            "understanding": "understanding",
            "generation": "generation:image",
            "generation:video": "generation:video",
        }
        if pipeline_kind is not None and intent == "understanding":
            intent = _PIPELINE_KIND_TO_INTENT.get(pipeline_kind, "understanding")

        # ===== 按意图解析模型 =====
        required_capability = _INTENT_TO_CAPABILITY.get(intent, "text")

        explicit_model = model if (model and model != "auto") else None
        auto_router_meta: Optional[Dict[str, Any]] = None
        if explicit_model and self.is_model_registered(explicit_model) \
                and required_capability in self._model_capabilities.get(explicit_model, []):
            pass
        else:
            hint = model_hint or explicit_model
            resolved = await self._resolve_by_intent(intent=intent, model_hint=hint)
            if "error" in resolved:
                yield {"error": resolved["error"]}
                return
            model = resolved["model"]
            auto_router_meta = resolved.get("meta")
            logger.info("bridge 意图流式解析: intent=%s → model=%s", intent, model)

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

        # ===== 按意图分发: image/video 走专门分支(不调 chat completions) =====
        # 注意: completion_stream 也需要此分支,因为 Images/Video API 非流式,
        # 直接走下游 acompletion 会错误地调 chat completions 端点。
        if intent == "generation:image":
            try:
                prompt_text = self._extract_prompt_from_messages(messages)
                img_result = await self._do_image_generation(
                    prompt=prompt_text, model=model, extra_headers=extra_headers,
                )
                request_cost = self._track_usage(model, img_result)
                yield {
                    "id": f"gen-img-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "delta": {"role": "assistant", "content": img_result.get("choices", [{}])[0].get("message", {}).get("content", "")},
                    }],
                    "usage": img_result.get("usage", {}),
                    "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": request_cost, "model_router": auto_router_meta},
                }
            except Exception as exc:
                logger.error("image generation failed in stream: %s", exc, exc_info=True)
                yield {
                    "id": f"gen-img-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "delta": {"content": f"[Image generation error] {exc}"},
                    }],
                    "error": {"code": "image_generation_failed", "message": str(exc)},
                }
            return
        if intent == "generation:video":
            try:
                vid_result = await self._do_video_generation(
                    messages=messages, model=model, extra_headers=extra_headers,
                )
                request_cost = self._track_usage(model, vid_result)
                content = vid_result.get("choices", [{}])[0].get("message", {}).get("content", "")
                yield {
                    "id": f"gen-vid-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "delta": {"role": "assistant", "content": content},
                    }],
                    "usage": vid_result.get("usage", {}),
                    "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": request_cost, "model_router": auto_router_meta},
                }
            except Exception as exc:
                logger.error("video generation failed in stream: %s", exc, exc_info=True)
                yield {
                    "id": f"gen-vid-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "delta": {"content": f"[Video generation error] {exc}"},
                    }],
                    "error": {"code": "video_generation_failed", "message": str(exc)},
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

                # user_id and extra_headers must survive retries (locals() list above omits them)
                if user_id:
                    params["user_id"] = user_id
                if extra_headers:
                    params["extra_headers"] = extra_headers

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
