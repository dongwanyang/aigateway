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

            self.router = Router(
                model_list=model_list,
                routing_strategy=routing_strategy_config,
                num_retries=getattr(self.config, "num_retries", 3)
                if hasattr(self.config, "num_retries")
                else 3,
            )

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

            if model_grouper:
                # 多模型分组模式
                for group in model_grouper:
                    for model_name in group.get("models", []):
                        fallback_models = group.get("fallback_models", [])
                        if base_url:
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
                                "base_url": base_url,
                                "num_retries": num_retries,
                                "retry_after": retry_after,
                            },
                            "fallbacks": fallback_models,
                        }

                        # 注册定价信息到 LiteLLM cost map，抑制告警
                        # 优先使用 config 中的 pricing，未配置则用 placeholder 0 cost
                        self._register_model_pricing(group, litellm_model, base_url, provider_name)

                        model_list.append(entry)
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

        Args:
            group: model_grouper 中的一个分组字典。
            litellm_model: LiteLLM Router 注册的完整模型名。
            base_url: 是否有自定义 base_url。
            provider_name: 提供商名称（如 "openai", "anthropic", "agnes"）。
        """
        try:
            import litellm

            pricing = group.get("pricing", {})
            if not pricing:
                pricing = {"$default": {"prompt": 0.0, "completion": 0.0}}

            # 尝试用完整模型名和裸模型名两种 key 查找定价
            model_price = pricing.get(litellm_model, pricing.get(provider_name, pricing.get("$default", {"prompt": 0.0, "completion": 0.0})))

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
    ) -> Dict[str, Any]:
        """发送聊天补全请求到下游 LLM。

        支持重试和 fallback 链。当主模型调用失败时，
        依次尝试 fallback_chain 中的模型。

        Args:
            messages: 消息列表，OpenAI 格式。
            model: 目标模型名称。
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

        Returns:
            完整的响应字典（OpenAI 格式）。
        """
        max_retries = max_retries or 3
        attempts = 0
        last_error: Optional[Exception] = None
        fallback_used: List[str] = []

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
                )

                # 成功：记录用量
                self._track_usage(current_model, result)

                return {
                    "data": result,
                    "_meta": {
                        "routed_to": {
                            "provider": self._extract_provider(current_model),
                            "model": current_model,
                            "fallback_chain": fallback_used,
                        }
                    },
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

        # 执行请求
        if kwargs.get("stream", False):
            # 流式：逐 chunk 聚合
            chunks: List[str] = []
            async for chunk in self.router.acompletion(**params):
                chunk_data = chunk.dict() if hasattr(chunk, "dict") else dict(chunk)
                chunks.append(chunk_data)

            # 将流式 chunks 合成为非流式响应格式（因为上层期望字典返回）
            aggregated = self._aggregate_stream_chunks(chunks)
            return aggregated

        # 非流式
        response = await self.router.acompletion(**params)
        response_data = response.dict() if hasattr(response, "dict") else dict(response)
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

    def _track_usage(self, model: str, response: Dict[str, Any]) -> None:
        """记录模型调用的 token 用量和成本。

        Args:
            model: 使用的模型名称。
            response: 响应字典。
        """
        if self.cost_tracker is None:
            return

        try:
            usage = response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            self.cost_tracker.total_input_tokens += prompt_tokens
            self.cost_tracker.total_output_tokens += completion_tokens
            self.cost_tracker.total_tokens += total_tokens

            # 估算成本（基于模型定价）
            cost = self._estimate_cost(model, total_tokens)
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

    def _estimate_cost(self, model: str, total_tokens: int) -> float:
        """根据模型估算成本（美元）。

        简化实现，实际应查询 LiteLLM CostTracker 的 pricing。

        Args:
            model: 模型名称。
            total_tokens: 总 token 数。

        Returns:
            估算成本（美元）。
        """
        # 简化的模型定价表
        pricing = {
            "gpt-4o": 0.000005,        # $5 / 1M tokens
            "gpt-4o-mini": 0.00000015,  # $0.15 / 1M tokens
            "claude-3-5-sonnet": 0.000003,
            "claude-3-haiku": 0.00000025,
            "gemini-1.5-pro": 0.0000025,
        }

        # 提取基础模型名
        base_model = model.split("/")[-1] if "/" in model else model
        price_per_token = pricing.get(base_model, 0.000001)

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
            models = await self.router.list_models()
            result = []
            for m in models:
                model_id = m.get("id", m.get("model_name", ""))
                result.append({
                    "id": model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": self._extract_provider(model_id),
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
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式发送聊天补全请求到下游 LLM。

        Yields:
            每个 chunk 的字典（OpenAI 流式格式）。
        """
        max_retries = max_retries or 3
        attempts = 0

        candidates = [model]
        if fallback_chain:
            for fb in fallback_chain:
                if isinstance(fb, dict):
                    candidates.append(fb.get("model", ""))
                elif isinstance(fb, str):
                    candidates.append(fb)

        while attempts <= max_retries:
            current_model = candidates[attempts % len(candidates)] if candidates else model
            try:
                params: Dict[str, Any] = {
                    "model": current_model,
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

                async for chunk in self.router.acompletion(**params):
                    chunk_data = chunk.dict() if hasattr(chunk, "dict") else dict(chunk)
                    yield chunk_data

                return  # 成功完成
            except Exception as exc:
                attempts += 1
                retry_delay = self.config.get("retry_delay_ms", 1000) / 1000.0
                logger.warning("模型 %s 流式调用失败 (尝试 %d/%d): %s", current_model, attempts, max_retries + 1, exc)
                if attempts <= max_retries:
                    await asyncio_sleep(retry_delay * attempts)

        # 全部失败
        yield {"error": {"code": "upstream_timeout", "message": f"All stream models failed: {exc}"}}

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
