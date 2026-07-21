# 意图驱动路由 + 生成调用路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"帮我画一只猫"真正画出图 —— 路由由用户意图(LLM 预判)决定,且图片/视频生成用正确的 OpenAI Images/Video API endpoint 调通。

**Architecture:** classifier 调轻量 LLM 预判意图(返回 `{"generation":"...","hint":"..."}`),输出 `understanding|generation:image|generation:video`。bridge 按意图分发:`_do_completion`(chat/completions,现有)、`_do_image_generation`(/images/generations,新增)、`_do_video_generation`(/videos,新增,异步)。模型标 `capabilities:[text,image,video]` 多选,候选池按意图对应能力过滤。取消 `auto` 魔法字符串与 `generation_intent` 字段。预判/ai_director 内部调用经智能选择器选廉价文本模型显式传入,不触发智能路由。

**Tech Stack:** Python 3.12, FastAPI, pydantic, litellm (OpenAI-compatible Router), pytest + pytest-asyncio, httpx。

## Global Constraints

- 所有调用大模型的方式必须异步(`async def` + `await`),不阻塞其他请求。
- Images API 严格遵循 OpenAI 格式(`POST /v1/images/generations`,请求体 `model`/`prompt`/`n`/`size`/`quality`/`response_format` 顶级参数,响应 `{created,data:[{url|b64_json,revised_prompt}],usage}`)。
- Video API 严格遵循 OpenAI 格式(`POST /v1/videos` 异步返回 `{id,status:"queued",progress,...}`,轮询 `GET /v1/videos/{id}`)。
- 意图预判返回固定 JSON 体:`{"generation":"understanding|image|video","hint":"<模型名或None>"}`,字段名固定。
- 彻底取消 `model=='auto'` 魔法字符串与 `generation_intent` 字段,不保留向后兼容。
- endpoint 路径自动拼:provider 一个 `base_url`,understanding→`/chat/completions`、image→`/images/generations`、video→`/videos`。撤销每模型自定义 `base_url`。
- 测试用 `python3 -m pytest`,无 conftest 全局 fixture(单元测试不触发 e2e 健康检查)。
- 每个任务结束 `git add` + `git commit`,commit message 末尾加 `Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>`。
- 遵循 CLAUDE.md workflow rule 0:修复后跑 `window-code-review` skill 审 diff,**不自动 commit**——但本计划每个任务的 commit 步骤是用户已批准的实施步骤,执行时按计划 commit;最终全部完成后由用户审阅。

---

## File Structure

**新增文件:**
- `aigateway-core/src/aigateway_core/dispatch/intent_classifier.py` —— `IntentClassifier` 类:异步 LLM 意图预判,输出 `{generation,hint}` JSON,超时降级到启发式。
- `aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py` —— `ModelSelector` 类:按健康度/延迟/成本从 capabilities 含 `text` 的模型池选廉价文本模型(供预判/ai_director 内部调用用)。
- `aigateway-api/src/aigateway_api/video_routes.py` —— `GET /v1/videos/{id}` 轮询 endpoint(对应 OpenAI Retrieve a video)。
- `tests/test_intent_classifier.py`
- `tests/test_model_selector.py`
- `tests/test_generation_routing.py`
- `tests/test_image_generation.py`
- `tests/test_video_async.py`

**修改文件:**
- `aigateway-core/src/aigateway_core/dispatch/classifier.py` —— 重写 `classify_request`,改返回 `understanding|generation:image|generation:video`,调 `IntentClassifier`。
- `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` —— `pipeline_kind` 带媒介;`_dispatch_generation` 传意图;把预判结果 hint 写入 ctx。
- `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py` —— `completion()` 加 `intent`+`model_hint` 参数;按意图分发到 `_do_completion`/`_do_image_generation`/`_do_video_generation`;`_build_model_list` 读 `capabilities` 而非 `modality`;取消 `auto` 分支。
- `aigateway-core/src/aigateway_core/route/model_resolution/model_router.py` —— `_build_model_list` 读 `capabilities`;`route()` 按意图能力过滤候选池。
- `aigateway-core/src/aigateway_core/pipelines/generation/_common/config.py` —— `ModelRouterConfig` 用 `model_capabilities` 评分保留,`model_modalities` 字段废弃说明。
- `aigateway-api/src/aigateway_api/openai_compat.py` —— 删 `ChatCompletionRequest.generation_intent` 字段。
- `aigateway-api/src/aigateway_api/main.py` —— wiring `IntentClassifier`/`ModelSelector` 注入 dispatcher;注册 video_routes。
- `config.yaml` + `config.yaml.template` —— `modality`→`capabilities`,删每模型 `base_url`,删 `model_router` 死配置,加 `intent_classifier`/`model_selector`/`generation` 节。

---

## Task 1: config schema 迁移(modality→capabilities,删死配置)

**Files:**
- Modify: `config.yaml`
- Modify: `config.yaml.template`(若存在;不存在则跳过该子步骤)

**Interfaces:**
- Produces: providers 下 models 用 `capabilities: [text|image|video]` 数组标注;不再有每模型 `base_url`;无 `plugins: model_router` 死配置;新增顶层 `intent_classifier`/`model_selector`/`generation` 节。

- [ ] **Step 1: 改 config.yaml 的 agnes provider**

把 `providers.agnes.model_grouper[0].models` 三个模型的 `modality` 改为 `capabilities`,删 agnes-image-2.1-flash 与 agnes-video-v2.0 的 `base_url`。定位 `config.yaml:100-111`,替换为:

```yaml
    model_grouper:
    - models:
      - name: agnes-2.0-flash
        capabilities: [text, image, video]
      - name: agnes-image-2.1-flash
        capabilities: [image]
      - name: agnes-video-v2.0
        capabilities: [video]
      fallback_models: []
```

- [ ] **Step 2: 改 deepseek / glm5.2 provider 的 modality→capabilities**

把 `deepseek-v4-flash`、`glm-5.2`、`deepseek-v4-pro`、`kimi-k2.7-code` 的 `modality: [llm]` 全改为 `capabilities: [text]`。

- [ ] **Step 3: 删 plugins 下的 model_router 死配置**

定位 `config.yaml:55-58`,删除:

```yaml
- name: model_router
  enabled: true
  depends_on: []
  config: {}
```

- [ ] **Step 4: 删 generation_optimization.model_router.model_modalities**

定位 `config.yaml:360-366`,删除 `model_modalities:` 整块(保留 `model_capabilities` 和 `enabled`)。改后该节为:

```yaml
  model_router:
    model_capabilities:
      agnes-2.0-flash: 60
      agnes-image-2.1-flash: 80
      agnes-video-v2.0: 85
    enabled: true
```

- [ ] **Step 5: 新增 intent_classifier / model_selector / generation 节**

在 `config.yaml` 顶层(`generation_optimization` 节之后)新增:

```yaml
intent_classifier:
  model: agnes-2.0-flash
  timeout_seconds: 3
  fallback: heuristic

model_selector:
  health_check_interval: 60
  latency_weight: 0.4
  cost_weight: 0.2
  success_rate_weight: 0.4

generation:
  image:
    default_size: "1024x1024"
    response_format: "url"
    quality: "auto"
  video:
    poll_endpoint: /v1/videos/{id}
```

- [ ] **Step 6: 同步 config.yaml.template(若存在)**

Run: `ls config.yaml.template`
若有,对 `config.yaml.template` 重复 Step 1-5 的对应改动。若无,跳过。

- [ ] **Step 7: 验证 YAML 语法**

Run: `python3 -c "import yaml; yaml.safe_load(open('config.yaml')); print('OK')"`
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add config.yaml config.yaml.template
git commit -m "refactor(config): modality→capabilities multi-select, drop per-model base_url, remove dead model_router plugin, add intent_classifier/model_selector/generation sections

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 2: ModelSelector —— 智能选廉价文本模型

**Files:**
- Create: `aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py`
- Test: `tests/test_model_selector.py`

**Interfaces:**
- Consumes: `LiteLLMBridge.get_registered_models()`、`LiteLLMBridge._model_capabilities`(Task 3 产出)、`LiteLLMBridge._model_pricing`、`ProviderCooldownTracker`(现有)。
- Produces: `class ModelSelector` with `async def select_text_model(self) -> str`,返回裸模型名;`def get_health(self, model: str) -> dict`。

- [ ] **Step 1: 写失败测试 —— 选 capabilities 含 text 的最廉价模型**

Create `tests/test_model_selector.py`:

```python
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.model_resolution.model_selector import ModelSelector


def _make_bridge():
    bridge = MagicMock()
    bridge._model_capabilities = {
        "agnes-2.0-flash": ["text", "image", "video"],
        "agnes-image-2.1-flash": ["image"],
        "deepseek-v4-flash": ["text"],
    }
    bridge._model_pricing = {
        "agnes-2.0-flash": {"prompt": 0.02, "completion": 1.0},
        "deepseek-v4-flash": {"prompt": 0.01, "completion": 0.5},
    }
    bridge.get_registered_models = MagicMock(
        return_value=["agnes-2.0-flash", "agnes-image-2.1-flash", "deepseek-v4-flash"]
    )
    cooldown = MagicMock()
    cooldown.is_healthy = MagicMock(return_value=True)
    cooldown.get_stats = MagicMock(return_value={"success_rate": 1.0, "avg_latency_ms": 100})
    bridge._cooldown_tracker = cooldown
    return bridge


@pytest.mark.asyncio
async def test_selects_text_capable_model():
    bridge = _make_bridge()
    sel = ModelSelector(bridge=bridge, config={"latency_weight": 0.4, "cost_weight": 0.2, "success_rate_weight": 0.4})
    model = await sel.select_text_model()
    assert model in ("agnes-2.0-flash", "deepseek-v4-flash")


@pytest.mark.asyncio
async def test_excludes_non_text_models():
    bridge = _make_bridge()
    sel = ModelSelector(bridge=bridge, config={})
    for _ in range(5):
        assert await sel.select_text_model() != "agnes-image-2.1-flash"


@pytest.mark.asyncio
async def test_fallback_to_default_when_pool_empty():
    bridge = _make_bridge()
    bridge._model_capabilities = {"agnes-image-2.1-flash": ["image"]}
    sel = ModelSelector(bridge=bridge, config={}, default_model="agnes-2.0-flash")
    assert await sel.select_text_model() == "agnes-2.0-flash"


@pytest.mark.asyncio
async def test_prefers_healthy_cheap_model():
    bridge = _make_bridge()
    # deepseek 更便宜
    cooldown = MagicMock()
    # agnes-2.0-flash 不健康, deepseek 健康
    cooldown.is_healthy = MagicMock(side_effect=lambda m: m != "agnes-2.0-flash")
    cooldown.get_stats = MagicMock(return_value={"success_rate": 1.0, "avg_latency_ms": 100})
    bridge._cooldown_tracker = cooldown
    sel = ModelSelector(bridge=bridge, config={"latency_weight": 0.4, "cost_weight": 0.2, "success_rate_weight": 0.4})
    assert await sel.select_text_model() == "deepseek-v4-flash"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_model_selector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.route.model_resolution.model_selector'`

- [ ] **Step 3: 实现 ModelSelector**

Create `aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py`:

```python
"""ModelSelector —— 为内部调用(意图预判/ai_director)选廉价文本模型.

从 capabilities 含 'text' 的模型池中,按健康度/延迟/成本加权选最佳连接模型。
超时或池空时降级到 config 默认模型。避免触发智能路由(显式传具体模型名)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelSelector:
    """按健康度/延迟/成本从 text 能力模型池选廉价文本模型."""

    def __init__(
        self,
        bridge: Any,
        config: Optional[Dict[str, Any]] = None,
        default_model: str = "agnes-2.0-flash",
        timeout_seconds: float = 0.5,
    ) -> None:
        self._bridge = bridge
        self._config = config or {}
        self._default_model = default_model
        self._timeout = timeout_seconds
        self._latency_w = float(self._config.get("latency_weight", 0.4))
        self._cost_w = float(self._config.get("cost_weight", 0.2))
        self._success_w = float(self._config.get("success_rate_weight", 0.4))

    async def select_text_model(self) -> str:
        """选一个 capabilities 含 'text' 的健康模型,加权评分最高者."""
        try:
            return await asyncio.wait_for(self._select(), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning("ModelSelector 超时,降级到默认模型 %s", self._default_model)
            return self._default_model
        except Exception as exc:
            logger.warning("ModelSelector 异常 %s,降级到默认模型 %s", exc, self._default_model)
            return self._default_model

    async def _select(self) -> str:
        caps: Dict[str, List[str]] = getattr(self._bridge, "_model_capabilities", {}) or {}
        registered = self._bridge.get_registered_models() if self._bridge else []
        text_pool = [m for m in registered if "text" in caps.get(m, [])]
        if not text_pool:
            logger.warning("ModelSelector: 无 text 能力模型,降级到默认 %s", self._default_model)
            return self._default_model

        cooldown = getattr(self._bridge, "_cooldown_tracker", None)
        pricing: Dict[str, Dict[str, float]] = getattr(self._bridge, "_model_pricing", {}) or {}

        best: Optional[str] = None
        best_score = -1.0
        for m in text_pool:
            healthy = True
            stats = {"success_rate": 1.0, "avg_latency_ms": 100.0}
            if cooldown is not None:
                try:
                    healthy = cooldown.is_healthy(m)
                    stats = cooldown.get_stats(m)
                except Exception:
                    pass
            if not healthy:
                continue
            success_rate = float(stats.get("success_rate", 1.0))
            latency = max(float(stats.get("avg_latency_ms", 100.0)), 1.0)
            price = pricing.get(m, {})
            cost = float(price.get("prompt", 0.0)) + float(price.get("completion", 0.0))
            # 评分: 成功率越高越好, 延迟越低越好, 成本越低越好
            score = (
                self._success_w * success_rate
                + self._latency_w * (1.0 / (1.0 + latency / 1000.0))
                + self._cost_w * (1.0 / (1.0 + cost))
            )
            if score > best_score:
                best_score = score
                best = m
        if best is None:
            # 全不健康,取池里第一个
            return text_pool[0]
        return best

    def get_health(self, model: str) -> Dict[str, Any]:
        cooldown = getattr(self._bridge, "_cooldown_tracker", None)
        if cooldown is None:
            return {"success_rate": 1.0, "avg_latency_ms": 100.0, "healthy": True}
        try:
            return {
                "success_rate": cooldown.get_stats(model).get("success_rate", 1.0),
                "avg_latency_ms": cooldown.get_stats(model).get("avg_latency_ms", 100.0),
                "healthy": cooldown.is_healthy(model),
            }
        except Exception:
            return {"success_rate": 1.0, "avg_latency_ms": 100.0, "healthy": True}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_model_selector.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/model_resolution/model_selector.py tests/test_model_selector.py
git commit -m "feat(model_selector): add health/latency/cost-weighted text model selector for internal calls

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 3: bridge 读 capabilities + 取消 auto 分支

**Files:**
- Modify: `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py:67,255-389,528-687`
- Test: `tests/test_litellm_bridge.py`(扩展)

**Interfaces:**
- Consumes: Task 1 的 `capabilities` config。
- Produces: `LiteLLMBridge._model_capabilities: Dict[str, List[str]]`;`completion()` 新增 `intent: str` + `model_hint: Optional[str]` 参数(取代 auto 逻辑);`_resolve_auto` 改名/改签名为 `_resolve_by_intent(intent, model_hint)`。

- [ ] **Step 1: 写失败测试 —— capabilities 池过滤**

在 `tests/test_litellm_bridge.py` 末尾追加:

```python
class TestCapabilitiesPool:
    """capabilities 多选 + 按意图过滤候选池."""

    def _bridge_with_caps(self):
        models_config = {
            "agnes": {
                "api_key": "k",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "model_grouper": [
                    {
                        "models": [
                            {"name": "agnes-2.0-flash", "capabilities": ["text", "image", "video"]},
                            {"name": "agnes-image-2.1-flash", "capabilities": ["image"]},
                            {"name": "deepseek-v4-flash", "capabilities": ["text"]},
                        ],
                        "fallback_models": [],
                        "pricing": {},
                    }
                ],
            }
        }
        return _create_bridge_with_models(models_config)

    def test_capabilities_recorded(self):
        bridge = self._bridge_with_caps()
        assert "text" in bridge._model_capabilities["agnes-2.0-flash"]
        assert bridge._model_capabilities["agnes-image-2.1-flash"] == ["image"]

    @pytest.mark.asyncio
    async def test_resolve_by_intent_image_pools_image_models(self):
        bridge = self._bridge_with_caps()
        resolved = await bridge._resolve_by_intent(intent="generation:image", model_hint=None)
        assert "error" not in resolved
        assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")

    @pytest.mark.asyncio
    async def test_resolve_by_intent_hint_in_pool_preferred(self):
        bridge = self._bridge_with_caps()
        resolved = await bridge._resolve_by_intent(
            intent="generation:image", model_hint="agnes-2.0-flash"
        )
        assert resolved["model"] == "agnes-2.0-flash"

    @pytest.mark.asyncio
    async def test_resolve_by_intent_hint_not_in_pool_ignored(self):
        bridge = self._bridge_with_caps()
        # hint 是 text 模型, 但意图是 image -> 忽略 hint, 选 image 池
        resolved = await bridge._resolve_by_intent(
            intent="generation:image", model_hint="deepseek-v4-flash"
        )
        assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")

    @pytest.mark.asyncio
    async def test_resolve_by_intent_empty_pool_returns_error(self):
        bridge = self._bridge_with_caps()
        # 无 video-only 外的… 实际 agnes-2.0-flash 含 video, 改成移除它
        bridge._model_capabilities = {
            "agnes-2.0-flash": ["text", "image"],
            "agnes-image-2.1-flash": ["image"],
            "deepseek-v4-flash": ["text"],
        }
        resolved = await bridge._resolve_by_intent(intent="generation:video", model_hint=None)
        assert "error" in resolved
        assert resolved["error"]["code"] == "no_model_for_intent"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_litellm_bridge.py::TestCapabilitiesPool -v`
Expected: FAIL(`_model_capabilities` 不存在 / `_resolve_by_intent` 不存在)

- [ ] **Step 3: 改 `_build_model_list` 读 capabilities**

在 `litellm_bridge.py:67` 把 `_model_modalities` 改名为 `_model_capabilities`:

```python
        self._model_capabilities: Dict[str, List[str]] = {}  # 裸模型名 -> capabilities 列表
```

在 `_build_model_list`(行 297-318)把读 `modality` 改为读 `capabilities`。把这一段:

```python
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
```

替换为:

```python
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
```

紧接着把:

```python
                        # 记录 modality
                        self._model_modalities[model_name] = model_modality
```

替换为:

```python
                        # 记录 capabilities
                        self._model_capabilities[model_name] = model_caps
```

`elif isinstance(model_entry, str)` 分支里把 `model_modality = ["generative"]` 改为 `model_caps = ["text"]`,并在其后 `self._model_capabilities[model_name] = model_caps`。

- [ ] **Step 4: 核对 ModelRouterStrategy.route 签名(避免 required_modality 取值不匹配)**

Run: `grep -n "def route\|required_modality\|\"llm\"\|\"generative\"\|\"text\"\|\"image\"\|\"video\"" aigateway-core/src/aigateway_core/route/model_resolution/model_router.py`

确认 `route(complexity_score, required_modality)` 的 `required_modality` 取值。现状(已核实):ModelRouterStrategy 内部用旧分类 `llm`/`mllm`/`generative`(model_router.py:44/54/112/218),其 `modality` 列表来源是 `_build_model_list` 读的 `model_entry.get("modality")`。

**本计划决策**:不改 ModelRouterStrategy 的取值体系(改动面大、且它在 Task 9 才迁移到 capabilities)。`_resolve_by_intent` 里**不调** `_auto_resolver.route`(避免取值不匹配选不到模型),改为直接在 capabilities 池内选池首模型 + hint 优先。`_auto_resolver` 留作后续评分接入点,本计划不依赖它。

- [ ] **Step 5: 把 `_resolve_auto` 改为 `_resolve_by_intent`**

把 `litellm_bridge.py:83-151` 整个 `_resolve_auto` 方法替换为:

```python
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
        required_capability = {
            "understanding": "text",
            "generation:image": "image",
            "generation:video": "video",
        }.get(intent, "text")

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
```

- [ ] **Step 6: 改 `completion()` 签名, 取消 auto 分支, 加 capability 校验**

把 `litellm_bridge.py:528-591` 的 `completion` 签名与 auto 解析块改掉。签名改为(把 `pipeline_kind: str = "understanding"` 替换为 `intent: str = "understanding"` + 新增 `model_hint: Optional[str] = None`):

```python
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
    ) -> Dict[str, Any]:
```

把 docstring 里 `pipeline_kind` 相关描述改为 `intent`。把 auto 解析块(原行 583-591):

```python
        # ===== model=='auto' 末端解析(总分总架构:让 bridge 决定用哪个模型)=====
        auto_router_meta: Optional[Dict[str, Any]] = None
        if model == "auto":
            resolved = await self._resolve_auto(messages, pipeline_kind)
            if "error" in resolved:
                return {"error": resolved["error"]}
            model = resolved["model"]
            auto_router_meta = resolved.get("meta")
            logger.info("bridge auto 解析: pipeline=%s → model=%s", pipeline_kind, model)
```

替换为:

```python
        # ===== 按意图解析模型(取消 auto 魔法字符串; 客户端 model 作 hint)=====
        auto_router_meta: Optional[Dict[str, Any]] = None
        required_capability = {
            "understanding": "text",
            "generation:image": "image",
            "generation:video": "video",
        }.get(intent, "text")

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
```

**关键(问题 5 修正)**:显式模型直连前必须校验 `required_capability in _model_capabilities[explicit_model]`。否则用户传 `model="deepseek-v4-flash"`(text-only)却意图 image,会拿 text 模型打 `/images/generations` 必然失败。校验不通过则忽略该显式模型,降级到 `_resolve_by_intent` 池解析(spec 决策:hint 不在候选池内→忽略)。

- [ ] **Step 7: 修复 list_models 里的 _model_modalities 引用**

`litellm_bridge.py:963` 把 `self._model_modalities.get(bare_model, ["generative"])` 改为 `self._model_capabilities.get(bare_model, ["text"])`。

- [ ] **Step 8: 全文搜剩余 `_model_modalities` / `_resolve_auto` / `pipeline_kind` 引用并修正**

Run: `grep -n "_model_modalities\|_resolve_auto\b\|pipeline_kind" aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`
对每个命中: `_model_modalities`→`_model_capabilities`; `_resolve_auto`→`_resolve_by_intent`; bridge 内部 `pipeline_kind` 引用(meta 字段/docstring)改为 `intent`。**注意 `completion_stream`(行 977+)也有 `pipeline_kind` 参数和 auto 解析块(行 1004),同样改为 `intent`+`model_hint` + 调 `_resolve_by_intent`,逻辑与 `completion()` Step 6 一致。**

- [ ] **Step 9: 运行测试确认通过**

Run: `python3 -m pytest tests/test_litellm_bridge.py -v`
Expected: 全部 passed(含新 TestCapabilitiesPool 5 项 + 原有)

- [ ] **Step 10: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py tests/test_litellm_bridge.py
git commit -m "refactor(bridge): read capabilities, replace auto with intent-based resolution + model_hint

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 4: IntentClassifier —— 异步 LLM 意图预判

**Files:**
- Create: `aigateway-core/src/aigateway_core/dispatch/intent_classifier.py`
- Test: `tests/test_intent_classifier.py`

**Interfaces:**
- Consumes: `LiteLLMBridge.completion(messages, model, intent="understanding")`(Task 3)、`ModelSelector.select_text_model()`(Task 2)。
- Produces: `class IntentClassifier` with `async def classify(self, messages, body_model) -> dict` 返回 `{"generation": "understanding|image|video", "hint": "..."}`,字段 `generation`/`hint` 固定。

- [ ] **Step 1: 写失败测试**

Create `tests/test_intent_classifier.py`:

```python
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.intent_classifier import IntentClassifier


def _mock_bridge(text_model="agnes-2.0-flash"):
    bridge = MagicMock()
    bridge.completion = AsyncMock()
    selector = MagicMock()
    selector.select_text_model = AsyncMock(return_value=text_model)
    return bridge, selector


def _resp(content: str):
    return {"data": {"choices": [{"message": {"content": content}}]}, "_meta": {}}


@pytest.mark.asyncio
async def test_classify_image():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"image","hint":"None"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 3})
    result = await ic.classify(messages=[{"role": "user", "content": "帮我画一只猫"}], body_model="agnes-2.0-flash")
    assert result == {"generation": "image", "hint": "None"}
    # 预判调用必须显式传文本模型 + intent=understanding, 不触发智能路由
    call_kwargs = bridge.completion.call_args.kwargs
    assert call_kwargs.get("model") == "agnes-2.0-flash"
    assert call_kwargs.get("intent") == "understanding"


@pytest.mark.asyncio
async def test_classify_video_with_hint():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"video","hint":"agnes-video-v2.0"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "用 agnes-video 生成一段视频"}], body_model=None)
    assert result == {"generation": "video", "hint": "agnes-video-v2.0"}


@pytest.mark.asyncio
async def test_classify_understanding():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp('{"generation":"understanding","hint":"None"}')
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "解释这段代码"}], body_model=None)
    assert result["generation"] == "understanding"


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_text():
    bridge, sel = _mock_bridge()
    import asyncio as _a
    async def slow(*a, **k):
        await _a.sleep(5)
    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    result = await ic.classify(messages=[{"role": "user", "content": "你好"}], body_model=None)
    # 纯文本降级 -> understanding
    assert result["generation"] == "understanding"


@pytest.mark.asyncio
async def test_timeout_fallback_heuristic_image_content():
    bridge, sel = _mock_bridge()
    import asyncio as _a
    async def slow(*a, **k):
        await _a.sleep(5)
    bridge.completion = AsyncMock(side_effect=slow)
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={"timeout_seconds": 0.1})
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    result = await ic.classify(messages=msgs, body_model=None)
    # 带图降级 -> image
    assert result["generation"] == "image"


@pytest.mark.asyncio
async def test_malformed_json_fallback():
    bridge, sel = _mock_bridge()
    bridge.completion.return_value = _resp("not json at all")
    ic = IntentClassifier(bridge=bridge, model_selector=sel, config={})
    result = await ic.classify(messages=[{"role": "user", "content": "画图"}], body_model=None)
    assert result["generation"] in ("understanding", "image")  # 降级不崩
    assert "hint" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_intent_classifier.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 实现 IntentClassifier**

Create `aigateway-core/src/aigateway_core/dispatch/intent_classifier.py`:

```python
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
        self._timeout = float(self._config.get("timeout_seconds", 3))
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
        # 抽取第一个 {...} JSON
        match = re.search(r"\{[^{}]*\}", content)
        if not match:
            return self._heuristic(messages)
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return self._heuristic(messages)
        gen = str(obj.get("generation", "")).strip().lower()
        hint = obj.get("hint", "None")
        if gen not in ("understanding", "image", "video"):
            return self._heuristic(messages)
        if hint is None:
            hint = "None"
        return {"generation": gen, "hint": str(hint)}

    def _heuristic(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """降级: 带图→image, 纯文本→understanding."""
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type", "") in (
                        "image_url", "input_image", "image", "video", "input_video",
                    ):
                        t = b.get("type", "")
                        if t in ("video", "input_video"):
                            return {"generation": "video", "hint": "None"}
                        return {"generation": "image", "hint": "None"}
        return {"generation": "understanding", "hint": "None"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_intent_classifier.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/intent_classifier.py tests/test_intent_classifier.py
git commit -m "feat(intent_classifier): async LLM intent pre-judge returning {generation,hint} JSON with heuristic fallback

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 5: classifier.py 重写 —— 调 IntentClassifier, 返回带媒介 pipeline_kind

**Files:**
- Modify: `aigateway-core/src/aigateway_core/dispatch/classifier.py`(全文件重写)
- Test: `tests/test_intent_classifier.py`(已覆盖核心),新增对 `classify_request` 的集成测试在 Task 9。

**Interfaces:**
- Consumes: `IntentClassifier.classify()`(Task 4)。
- Produces: `async def classify_request(body, config_manager, intent_classifier=None) -> str`,返回 `"understanding"|"generation:image"|"generation:video"`。

- [ ] **Step 1: 写失败测试 —— classify_request 异步返回带媒介**

Create `tests/test_classifier_rewrite.py`:

```python
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.dispatch.classifier import classify_request


class _Body:
    def __init__(self, model=None, messages=None):
        self.model = model
        self.messages = messages or []


@pytest.mark.asyncio
async def test_classify_image_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "image", "hint": "None"})
    result = await classify_request(_Body(model="agnes-2.0-flash",
                                          messages=[{"role": "user", "content": "画一只猫"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "generation:image"


@pytest.mark.asyncio
async def test_classify_video_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "video", "hint": "None"})
    result = await classify_request(_Body(messages=[{"role": "user", "content": "生成视频"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "generation:video"


@pytest.mark.asyncio
async def test_classify_understanding_intent():
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "understanding", "hint": "None"})
    result = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=ic)
    assert result == "understanding"


@pytest.mark.asyncio
async def test_classify_no_intent_classifier_defaults_understanding():
    result = await classify_request(_Body(messages=[{"role": "user", "content": "你好"}]),
                                     MagicMock(), intent_classifier=None)
    assert result == "understanding"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_classifier_rewrite.py -v`
Expected: FAIL(`classify_request` 是同步 / 返回 "generation")

- [ ] **Step 3: 重写 classifier.py**

完全替换 `aigateway-core/src/aigateway_core/dispatch/classifier.py` 内容为:

```python
"""请求分类 —— 意图驱动路由.

classify_request 调 IntentClassifier(LLM 预判)输出带媒介 pipeline_kind:
"understanding" | "generation:image" | "generation:video".
取消 generation_intent 字段、模型名推断、auto 魔法字符串。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def classify_request(
    body: Any,
    config_manager: Any,
    intent_classifier: Optional[Any] = None,
) -> str:
    """把请求分类为 understanding | generation:image | generation:video.

    Args:
        body: ChatCompletionRequest(有 .model/.messages 属性)或 dict.
        config_manager: 配置管理器(保留参数, 当前未用).
        intent_classifier: IntentClassifier 实例. None 时默认 understanding.

    Returns:
        pipeline_kind 字符串.
    """
    messages = getattr(body, "messages", None)
    if messages is None and isinstance(body, dict):
        messages = body.get("messages")

    if intent_classifier is None:
        logger.debug("classify_request: 无 intent_classifier, 默认 understanding")
        return "understanding"

    model = getattr(body, "model", None)
    if model is None and isinstance(body, dict):
        model = body.get("model")

    try:
        result = await intent_classifier.classify(messages=messages or [], body_model=model)
    except Exception as exc:
        logger.warning("classify_request: intent_classifier 异常 %s, 默认 understanding", exc)
        return "understanding"

    generation = result.get("generation", "understanding")
    hint = result.get("hint", "None")

    # 把 hint 存到 body 上, 供 dispatcher 传给 bridge 作 model_hint
    try:
        setattr(body, "_intent_hint", hint if hint != "None" else None)
    except Exception:
        pass

    if generation == "image":
        return "generation:image"
    if generation == "video":
        return "generation:video"
    return "understanding"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_classifier_rewrite.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/classifier.py tests/test_classifier_rewrite.py
git commit -m "refactor(classifier): rewrite to async intent-driven routing returning understanding|generation:image|generation:video

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 6: dispatcher 接入异步 classify + 传 intent/model_hint

**Files:**
- Modify: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py:32,239-251,479-596,602-640,758-793`
- Modify: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py`(`__init__` 接收 intent_classifier)

**Interfaces:**
- Consumes: `classify_request`(Task 5, 现异步)、`LiteLLMBridge.completion(intent=..., model_hint=...)`(Task 3)。
- Produces: dispatcher 把 `pipeline_kind`(带媒介)和 `model_hint`(来自 `_intent_hint`)传给 bridge。

- [ ] **Step 1: 找 dispatcher.__init__ 与 classify_request 调用点**

Run: `grep -n "def __init__\|self.intent_classifier\|classify_request\|self.config_manager" aigateway-core/src/aigateway_core/dispatch/dispatcher.py | head`
确认 `__init__` 签名与 `classify_request` 调用行(239)。

- [ ] **Step 2: __init__ 接收 intent_classifier**

在 `dispatcher.py` 的 `RequestDispatcher.__init__` 参数列表末尾加 `intent_classifier: Optional[Any] = None`,并在方法体内 `self.intent_classifier = intent_classifier`。(若 __init__ 已有大量参数,加在最后即可。)

- [ ] **Step 3: 改 classify_request 调用为异步 + 传 intent_classifier**

定位 `dispatcher.py:239`:

```python
        pipeline_kind = classify_request(body, self.config_manager)
```

替换为:

```python
        pipeline_kind = await classify_request(
            body, self.config_manager, intent_classifier=self.intent_classifier
        )
```

- [ ] **Step 4: _dispatch_generation 接收并透传带媒介 pipeline_kind(三处 hardcode)**

`_dispatch_generation` 内有**三处**硬编码 `pipeline_kind="generation"`,语义不同但都应带媒介(已核实 dispatcher.py:512/587/595):

1. **行 512** —— 生成管道 `PipelineContext(pipeline_kind="generation", ...)`,改为 `pipeline_kind=pipeline_kind`(用方法参数)。
2. **行 587** —— 调 `_call_llm_stream(..., pipeline_kind="generation", ...)`,改为 `pipeline_kind=pipeline_kind`。
3. **行 595** —— 调 `_call_llm_nonstream(..., pipeline_kind="generation", ...)`,改为 `pipeline_kind=pipeline_kind`。

前置:给 `_dispatch_generation` 签名加 `pipeline_kind: str = "generation:image"` 参数(放末尾,在 `prefix` 后):

```python
    async def _dispatch_generation(
        self, body: Any, request: Request, engine: Any,
        user_id: Optional[str], key_hash: Optional[str], prefix: Dict[str, Any],
        pipeline_kind: str = "generation:image",
    ) -> JSONResponse:
```

并在 `dispatch()` 调用处(Step 6 改)传 `pipeline_kind=pipeline_kind`。

**注意**:这三处带媒介后,cache key v2 的 `pipeline_kind` 维度从 `generation` 变为 `generation:image`/`generation:video`(见 Task 9 Step 5 的 cache key 测试适配)。生成管道本就 `cache_key=None` 不回填,故不影响运行时缓存,仅影响 cache key 单元测试的断言字面值。

Run(改完验证三处已替换): `grep -n 'pipeline_kind="generation"' aigateway-core/src/aigateway_core/dispatch/dispatcher.py`
Expected: 无命中(全改为参数透传)。understanding 管道的 `pipeline_kind="understanding"`(行 313/412/466/472)**不动**。

- [ ] **Step 5: 把 _intent_hint 作为 model_hint 传给 bridge**

定位 `_call_llm_nonstream` 里调 `litellm_bridge.completion(...)`(行 627-640),在调用里加 `intent=` 和 `model_hint=` 参数。把:

```python
            result = await litellm_bridge.completion(
                messages=body.messages,
                model=body.model,
                user_id=user_id,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                top_p=body.top_p,
                frequency_penalty=body.frequency_penalty,
                presence_penalty=body.presence_penalty,
                tools=body.tools,
                tool_choice=body.tool_choice,
                stop=body.stop,
                pipeline_kind=pipeline_kind,
            )
```

替换为:

```python
            hint = getattr(body, "_intent_hint", None)
            result = await litellm_bridge.completion(
                messages=body.messages,
                model=body.model,
                user_id=user_id,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                top_p=body.top_p,
                frequency_penalty=body.frequency_penalty,
                presence_penalty=body.presence_penalty,
                tools=body.tools,
                tool_choice=body.tool_choice,
                stop=body.stop,
                intent=pipeline_kind,
                model_hint=hint,
            )
```

对 `_call_llm_stream`(行 758-793)做同样改动:把 `pipeline_kind=pipeline_kind` 改为 `intent=pipeline_kind, model_hint=getattr(body, "_intent_hint", None)`,并在其内部调 `litellm_bridge.completion` / `completion_stream` 处加这两个参数(若 stream 走 `completion_stream`,也加 `intent=`+`model_hint=`;若 `completion_stream` 签名暂无此参数,先加默认值参数 `intent: str = "understanding", model_hint: Optional[str] = None`)。

- [ ] **Step 6: 修 dispatch() 的 engine 选择逻辑**

定位 `dispatcher.py:247`:

```python
        engine = self.understanding_engine if pipeline_kind == "understanding" else self.generation_engine
```

保持不变(generation:image/generation:video 都走 generation_engine)。行 249-251:

```python
        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash, prefix)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash, prefix)
```

改为:

```python
        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash, prefix)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash, prefix, pipeline_kind)
```

并更新 `_dispatch_generation` 签名加 `pipeline_kind: str` 参数(放末尾)。

- [ ] **Step 7: 运行现有 dispatch 相关测试确认不破**

Run: `python3 -m pytest tests/test_runtime_skeleton_dispatch.py tests/test_runtime_skeleton_generation.py -v 2>&1 | tail -30`
Expected: 已有测试可能因签名变化失败 —— 记录失败, 在 Task 9 统一修。若仅因 `classify_request` 同步/异步或 `pipeline_kind` 参数失败, 先继续。

- [ ] **Step 8: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/dispatcher.py
git commit -m "refactor(dispatcher): async classify_request, thread intent+model_hint to bridge, generation pipeline_kind carries media

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 7: bridge 图片生成分支 `_do_image_generation`

**Files:**
- Modify: `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`(新增 `_do_image_generation`,在 `completion()` 分发)
- Test: `tests/test_image_generation.py`

**Interfaces:**
- Consumes: `generation.image` config(Task 1)、OpenAI Images API。
- Produces: `_do_image_generation(self, prompt, model, size, n, response_format, quality, extra_headers) -> Dict[str, Any]` 返回归一化的 chat completions 格式。

- [ ] **Step 1: 写失败测试**

Create `tests/test_image_generation.py`:

```python
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge():
    models_config = {
        "agnes": {
            "api_key": "k",
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [
                {"models": [{"name": "agnes-2.0-flash", "capabilities": ["text", "image"]}],
                 "fallback_models": [], "pricing": {}}
            ],
        }
    }
    b = LiteLLMBridge(config={"providers": models_config, "generation": {"image": {"default_size": "1024x1024", "response_format": "url", "quality": "auto"}}})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_image_endpoint_path():
    b = _bridge()
    captured = {}

    async def fake_post(url, headers, json):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"created": 1, "data": [{"url": "https://img/x.png"}], "usage": {"input_tokens": 5, "total_tokens": 5}}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_image_generation(prompt="a cat", model="agnes-2.0-flash")

    assert captured["url"].endswith("/images/generations")
    assert captured["json"]["model"] == "agnes-2.0-flash"
    assert captured["json"]["prompt"] == "a cat"
    assert captured["json"]["size"] == "1024x1024"
    assert captured["json"]["response_format"] == "url"
    # 归一为 chat completions
    assert "choices" in result
    assert "https://img/x.png" in result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_image_response_normalized_to_chat():
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"created": 1, "data": [{"b64_json": "AAAA"}]}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_image_generation(prompt="x", model="agnes-2.0-flash", response_format="b64_json")

    msg = result["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert "AAAA" in msg["content"]
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "usage" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_image_generation.py -v`
Expected: FAIL(`_do_image_generation` 不存在)

- [ ] **Step 3: 实现 _do_image_generation**

在 `litellm_bridge.py` 的 `_do_completion` 方法之后新增(需要 `import httpx` 在文件顶部,若已有则跳过):

```python
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

        严格遵循 OpenAI Images API 格式: 请求体 model/prompt/n/size/quality/response_format 顶级参数.
        """
        import httpx

        gen_cfg = (self.config.get("generation", {}) or {}).get("image", {}) or {}
        size = size or gen_cfg.get("default_size", "1024x1024")
        response_format = response_format or gen_cfg.get("response_format", "url")
        quality = quality or gen_cfg.get("quality", "auto")

        base_url, api_key = self._get_model_endpoint(model)
        endpoint = f"{base_url.rstrip('/')}/images/generations"

        body = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "size": size,
            "quality": quality,
        }
        if response_format:
            body["response_format"] = response_format

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        async with httpx.AsyncClient(timeout=120) as client:
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
```

- [ ] **Step 4: 加 _get_model_endpoint 辅助方法**

在 `_extract_provider` 附近新增:

```python
    def _get_model_endpoint(self, model: str) -> tuple[str, str]:
        """返回 (base_url, api_key) 供 Images/Video API 直调."""
        bare = model.split("/")[-1] if "/" in model else model
        providers = self.config.get("providers", {}) if isinstance(self.config, dict) else {}
        for provider_cfg in providers.values():
            if not isinstance(provider_cfg, dict):
                continue
            api_key = provider_cfg.get("api_key", "")
            base_url = provider_cfg.get("base_url", "")
            for group in provider_cfg.get("model_grouper", []) or []:
                for m in (group.get("models", []) if isinstance(group, dict) else []):
                    if isinstance(m, dict) and m.get("name") == bare:
                        return base_url, api_key
        # fallback: 第一个有 base_url 的 provider
        for provider_cfg in providers.values():
            if isinstance(provider_cfg, dict) and provider_cfg.get("base_url"):
                return provider_cfg["base_url"], provider_cfg.get("api_key", "")
        return "", ""
```

- [ ] **Step 5: 在 completion() 分发 intent**

定位 Task 3 改过的 `completion()` 方法体。**插入位置(问题 4 修正,精确锚点)**:在模型解析块(Step 6 的 `if explicit_model ... pass / else: _resolve_by_intent`)**之后**——此时 `model` 已是最终值(显式直连或池解析结果)、且已通过 capability 校验——在**现有 `_do_completion` 调用之前**插入意图分发。这样 image/video 意图直接 return,不进入后续 chat completions 路径。

具体:找到 Step 6 替换块里 `logger.info("bridge 意图解析: intent=%s → model=%s", intent, model)` 这行(或显式直连 `pass` 分支)之后、原 `# ===== 调 litellm =====` / `_do_completion` 调用之前,插入:

```python
        # ===== 按意图分发: image/video 走专门分支(不调 chat completions)=====
        if intent == "generation:image":
            # 从 messages 抽 prompt(若 ai_director 已改写, 取最后 user 文本)
            prompt_text = self._extract_prompt_from_messages(messages)
            img_result = await self._do_image_generation(
                prompt=prompt_text, model=model, extra_headers=extra_headers,
            )
            return {"data": img_result, "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": 0.0},
                    "usage": img_result.get("usage", {})}
        if intent == "generation:video":
            vid_result = await self._do_video_generation(
                messages=messages, model=model, extra_headers=extra_headers,
            )
            return {"data": vid_result, "_meta": {"routed_to": {"model": model, "intent": intent}, "cost": 0.0},
                    "usage": {}}
```

并新增辅助:

```python
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
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python3 -m pytest tests/test_image_generation.py -v`
Expected: 2 passed(`_do_video_generation` 在 Task 8 实现, 本任务先让它失败被 video 测试覆盖;此处 image 测试应过)

注: Step 5 引用了 `_do_video_generation`,若尚未定义会导致 `completion` 在 video 意图时报错——这是预期的,Task 8 补齐。image 测试不触发 video 分支,故通过。

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py tests/test_image_generation.py
git commit -m "feat(bridge): add _do_image_generation (OpenAI Images API) with chat-completions normalization

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 8: bridge 视频生成分支 `_do_video_generation` + 轮询 endpoint

**Files:**
- Modify: `aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py`(新增 `_do_video_generation` + `retrieve_video`)
- Create: `aigateway-api/src/aigateway_api/video_routes.py`
- Modify: `aigateway-api/src/aigateway_api/main.py:707`(注册 router)
- Test: `tests/test_video_async.py`

**Interfaces:**
- Consumes: OpenAI Videos API(`POST /v1/videos` 异步, `GET /v1/videos/{id}` 轮询)。
- Produces: `_do_video_generation(self, messages, model, extra_headers) -> Dict`(返回含 task_id 的 chat completions);`async def retrieve_video(self, video_id) -> Dict`(查任务状态)。

- [ ] **Step 1: 写失败测试**

Create `tests/test_video_async.py`:

```python
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge():
    models_config = {
        "agnes": {
            "api_key": "k",
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [
                {"models": [{"name": "agnes-video-v2.0", "capabilities": ["video"]}],
                 "fallback_models": [], "pricing": {}}
            ],
        }
    }
    b = LiteLLMBridge(config={"providers": models_config})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_video_submit_returns_task_id():
    b = _bridge()

    async def fake_post(url, headers, json):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_123", "object": "video", "status": "queued",
                                  "progress": 0, "created_at": 1, "model": "agnes-video-v2.0",
                                  "prompt": json["prompt"], "seconds": "4", "size": "720x1280"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        result = await b._do_video_generation(
            messages=[{"role": "user", "content": "生成一段跳舞视频"}], model="agnes-video-v2.0"
        )

    msg = result["choices"][0]["message"]["content"]
    assert "video_123" in msg
    assert "/v1/videos/video_123" in msg


@pytest.mark.asyncio
async def test_video_submit_endpoint_path():
    b = _bridge()
    captured = {}

    async def fake_post(url, headers, json):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_1", "object": "video", "status": "queued", "progress": 0}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = AsyncMock(side_effect=fake_post)
        MC.return_value = client
        await b._do_video_generation(messages=[{"role": "user", "content": "x"}], model="agnes-video-v2.0")

    assert captured["url"].endswith("/videos")
    assert captured["json"]["prompt"] == "x"
    assert captured["json"]["model"] == "agnes-video-v2.0"


@pytest.mark.asyncio
async def test_retrieve_video_polls_status():
    b = _bridge()

    async def fake_get(url, headers):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "video_123", "status": "in_progress", "progress": 50}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("aigateway_core.route.bridge.litellm_bridge.httpx.AsyncClient") as MC:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        MC.return_value = client
        result = await b.retrieve_video("video_123")

    assert result["status"] == "in_progress"
    assert result["progress"] == 50
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_video_async.py -v`
Expected: FAIL(`_do_video_generation`/`retrieve_video` 不存在)

- [ ] **Step 3: 实现 _do_video_generation + retrieve_video**

在 `litellm_bridge.py` 的 `_do_image_generation` 之后新增:

```python
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

        async with httpx.AsyncClient(timeout=120) as client:
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

        # 找一个有 base_url 的 provider
        providers = self.config.get("providers", {}) if isinstance(self.config, dict) else {}
        base_url = ""
        api_key = ""
        for provider_cfg in providers.values():
            if isinstance(provider_cfg, dict) and provider_cfg.get("base_url"):
                base_url = provider_cfg["base_url"]
                api_key = provider_cfg.get("api_key", "")
                break
        endpoint = f"{base_url.rstrip('/')}/videos/{video_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(endpoint, headers=headers)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_video_async.py -v`
Expected: 3 passed

- [ ] **Step 5: 创建 video_routes.py 轮询 endpoint**

Create `aigateway-api/src/aigateway_api/video_routes.py`:

```python
"""Video 轮询 endpoint —— GET /v1/videos/{id}.

对应 OpenAI Videos API 的 Retrieve a video, 供客户端轮询视频生成任务状态。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/videos/{video_id}")
async def retrieve_video(video_id: str, request: Request) -> JSONResponse:
    """轮询视频任务状态."""
    state = request.app.state
    bridge = getattr(state, "litellm_bridge", None)
    if bridge is None:
        return JSONResponse(
            content={"error": {"code": "internal_error", "message": "LiteLLM bridge not initialized"}},
            status_code=500,
        )
    try:
        result: Dict[str, Any] = await bridge.retrieve_video(video_id)
        return JSONResponse(content=result)
    except Exception as exc:
        return JSONResponse(
            content={"error": {"code": "video_retrieve_failed", "message": str(exc)}},
            status_code=502,
        )
```

- [ ] **Step 6: 在 main.py 注册 video_routes**

定位 `main.py:707`(`app.include_router(openai_compat.router, prefix="/v1", ...)`),在其后加:

```python
    from .video_routes import router as video_router
    app.include_router(video_router, prefix="/v1", tags=["Video"])
```

(若 main.py 顶部 import 区已有 router 导入模式,跟随该模式;否则用函数内 import。)

- [ ] **Step 7: 运行测试 + 验证路由注册**

Run: `python3 -m pytest tests/test_video_async.py -v && python3 -c "import sys; sys.path.insert(0,'aigateway-api/src'); from aigateway_api.video_routes import router; print('routes:', [r.path for r in router.routes])"`
Expected: 3 passed + `routes: ['/videos/{video_id}']`

- [ ] **Step 8: Commit**

```bash
git add aigateway-core/src/aigateway_core/route/bridge/litellm_bridge.py aigateway-api/src/aigateway_api/video_routes.py aigateway-api/src/aigateway_api/main.py tests/test_video_async.py
git commit -m "feat(bridge): add _do_video_generation (OpenAI Videos API async) + GET /v1/videos/{id} poll endpoint

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 9: openai_compat 删 generation_intent + main.py wiring + 修复现有测试

**Files:**
- Modify: `aigateway-api/src/aigateway_api/openai_compat.py:49-50`
- Modify: `aigateway-api/src/aigateway_api/main.py`(wiring IntentClassifier/ModelSelector 进 dispatcher)
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/_common/config.py:67,76`(model_modalities 注释废弃)
- Modify: `aigateway-core/src/aigateway_core/route/model_resolution/model_router.py`(`_build_model_list` 读 capabilities)
- Modify: 现有测试(`test_model_router_strategy.py`/`test_runtime_skeleton_dispatch.py` 等因签名变化失败者)

**Interfaces:**
- Consumes: Task 2-8 全部产出。
- Produces: 完整 wiring;`ChatCompletionRequest` 无 `generation_intent`;`ModelRouterConfig.model_modalities` 废弃。

- [ ] **Step 1: 删 generation_intent 字段**

定位 `openai_compat.py:49-50`,删除:

```python
    # 显式生成意图开关(classify_request 据此分流到 generation 管道)
    generation_intent: Optional[bool] = False
```

全文搜剩余引用:

Run: `grep -rn "generation_intent" aigateway-api/ aigateway-core/`
对每处命中删除(若有测试引用也一并改)。

- [ ] **Step 2: main.py wiring IntentClassifier + ModelSelector + 注入 dispatcher**

定位 `main.py` 里创建 `RequestDispatcher` 的地方(grep `RequestDispatcher(`)。在其前(bridge 已创建后,行 506 附近)加:

```python
    # ---- IntentClassifier + ModelSelector wiring ----
    intent_classifier = None
    try:
        from aigateway_core.dispatch.intent_classifier import IntentClassifier
        from aigateway_core.route.model_resolution.model_selector import ModelSelector
        ic_cfg = config_manager.get("intent_classifier", {}) or {}
        ms_cfg = config_manager.get("model_selector", {}) or {}
        model_selector = ModelSelector(
            bridge=litellm_bridge, config=ms_cfg,
            default_model=ic_cfg.get("model", "agnes-2.0-flash"),
        )
        intent_classifier = IntentClassifier(
            bridge=litellm_bridge, model_selector=model_selector, config=ic_cfg,
        )
        logger.info("IntentClassifier + ModelSelector 初始化完成")
    except Exception as exc:
        logger.warning("IntentClassifier 初始化失败: %s", exc)
```

把 `intent_classifier=intent_classifier` 加入 `RequestDispatcher(...)` 调用的参数。

- [ ] **Step 3: ModelRouterConfig 注释 model_modalities 废弃**

定位 `config.py:67,76`,把 `model_modalities` 字段的 docstring/注释改为"已废弃, 改用 providers.<p>.model_grouper[].models[].capabilities"。字段保留(避免破坏 load),但标注 deprecated。

- [ ] **Step 4: model_router._build_model_list 读 capabilities**

定位 `model_router.py:466-521`(读 `modality`/`model_modalities` 的块)。把读 `model_entry.get("modality")` 改为优先读 `model_entry.get("capabilities")`,回退 `modality`:

把:

```python
                        entry_modality = model_entry.get("modality")
```

改为:

```python
                        entry_caps = model_entry.get("capabilities")
                        entry_modality = entry_caps if entry_caps is not None else model_entry.get("modality")
```

(保持后续逻辑不变,只是数据源改为 capabilities 优先。)

- [ ] **Step 5: 跑全套单元测试, 修失败**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py --ignore=tests/e2e --ignore=tests/ui -x 2>&1 | tail -40`

对每个失败:
- 若是 `classify_request` 同步→异步:`await classify_request(...)`
- 若是 `pipeline_kind="generation"` 改媒介:测试里改 `generation:image` 或 mock intent_classifier
- 若是 `generation_intent` 字段删除:删测试里的该字段
- 若是 `model_modalities`→`capabilities`:测试 config 改 capabilities
- `test_model_router_strategy.py`:把 setup 的 `modality` 配置加 `capabilities`

**cache key v2 测试适配(问题 1 修正,必须显式处理)**:`tests/test_cache_key_v2.py:194` 的 `test_pipeline_kind_isolation` 用字面 `_key(pipeline_kind="generation")` 断言 understanding vs generation 隔离。pipeline_kind 现变为 `generation:image`/`generation:video`,需改测试:

- `k_g = self._key(pipeline_kind="generation")` → `k_g = self._key(pipeline_kind="generation:image")`
- 断言 `k_u != k_g` 仍成立(SHA-256 拼接,冒号安全,不同字面值必出不同 hash)。
- 另加一条断言:`self._key(pipeline_kind="generation:image") != self._key(pipeline_kind="generation:video")`(image/video 互相隔离)。

先确认 cache key 算法对带冒号的 pipeline_kind 不做切分:

Run: `grep -n "pipeline_kind" aigateway-core/src/aigateway_core/prefix/cache/cache_keys/*.py`
确认 `pipeline_kind` 作为整段拼入 SHA-256 输入(无按 `:` 拆分逻辑)。若有拆分需先修算法,但预期无(冒号在 SHA-256 输入里是普通字符)。

逐个修复直到全部通过。

- [ ] **Step 6: 验证关键路径测试通过**

Run: `python3 -m pytest tests/test_intent_classifier.py tests/test_model_selector.py tests/test_image_generation.py tests/test_video_async.py tests/test_classifier_rewrite.py tests/test_litellm_bridge.py tests/test_cache_key_v2.py tests/test_model_router_strategy.py tests/test_ai_director_strategy.py -v 2>&1 | tail -30`
Expected: 全 passed

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: wire IntentClassifier+ModelSelector into dispatcher, drop generation_intent, migrate model_router to capabilities, fix tests

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Task 10: 生成路由端到端测试 + CLAUDE.md 更新

**Files:**
- Create: `tests/test_generation_routing.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: 写端到端路由测试**

Create `tests/test_generation_routing.py`:

```python
"""生成路由端到端: 预判 hint 优先于 body.model, capabilities 池过滤."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge():
    models_config = {
        "agnes": {
            "api_key": "k", "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [{
                "models": [
                    {"name": "agnes-2.0-flash", "capabilities": ["text", "image", "video"]},
                    {"name": "agnes-image-2.1-flash", "capabilities": ["image"]},
                    {"name": "deepseek-v4-flash", "capabilities": ["text"]},
                ], "fallback_models": [], "pricing": {},
            }],
        }
    }
    b = LiteLLMBridge(config={"providers": models_config})
    b._build_model_list(models_config)
    b.router = MagicMock()
    return b


@pytest.mark.asyncio
async def test_hint_in_pool_preferred_over_body_model():
    """预判 hint 在池内优先; body.model 即使是别的也按 hint."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint="agnes-image-2.1-flash")
    assert resolved["model"] == "agnes-image-2.1-flash"


@pytest.mark.asyncio
async def test_hint_not_in_pool_ignored():
    """hint 是 text 模型但意图 image -> 忽略 hint 选 image 池."""
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint="deepseek-v4-flash")
    assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")


@pytest.mark.asyncio
async def test_no_hint_picks_image_pool():
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:image", model_hint=None)
    assert resolved["model"] in ("agnes-2.0-flash", "agnes-image-2.1-flash")


@pytest.mark.asyncio
async def test_understanding_pools_text_models():
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="understanding", model_hint=None)
    assert resolved["model"] in ("agnes-2.0-flash", "deepseek-v4-flash")


@pytest.mark.asyncio
async def test_video_intent_pools_video_models():
    b = _bridge()
    resolved = await b._resolve_by_intent(intent="generation:video", model_hint=None)
    assert resolved["model"] == "agnes-2.0-flash"  # 唯一含 video 能力


@pytest.mark.asyncio
async def test_polymorphic_model_selected_for_both_text_and_image():
    """agnes-2.0-flash 多态: understanding 选它(image 池也含它)."""
    b = _bridge()
    r1 = await b._resolve_by_intent(intent="understanding", model_hint="agnes-2.0-flash")
    r2 = await b._resolve_by_intent(intent="generation:image", model_hint="agnes-2.0-flash")
    assert r1["model"] == "agnes-2.0-flash"
    assert r2["model"] == "agnes-2.0-flash"
```

- [ ] **Step 2: 运行测试确认通过**

Run: `python3 -m pytest tests/test_generation_routing.py -v`
Expected: 6 passed

- [ ] **Step 3: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 "Known States & Gotchas" 与 Architecture 节更新:
- `classify_request` 现为**异步**,按 LLM 意图预判(非模态/模型名),返回 `understanding|generation:image|generation:video`。
- `generation_intent` 字段已删;`model=='auto'` 已取消,改客户端 model 作 hint。
- 模型配置 `modality`→`capabilities: [text,image,video]` 多选;撤销每模型 `base_url`。
- bridge 新增 `_do_image_generation`(/images/generations, OpenAI Images API)、`_do_video_generation`(/videos, 异步)、`GET /v1/videos/{id}` 轮询。
- 意图预判返回 `{"generation":"...","hint":"..."}` JSON;预判/ai_director 经 `ModelSelector` 选廉价文本模型显式传入,不触发智能路由。

按 CLAUDE.md rule 4(Trim, cap ~300 lines):若超长先 prune 再加。

- [ ] **Step 4: 跑 window-code-review 审 diff**

Run the `window-code-review` skill on the full diff(CLAUDE.md workflow rule 0)。修复 confirmed findings。

- [ ] **Step 5: 全套测试最终验证**

Run: `python3 -m pytest tests/ --ignore=tests/test_template_routes.py --ignore=tests/e2e --ignore=tests/ui -q 2>&1 | tail -15`
Expected: 全 passed 或仅已知 flaky 失败(记录原因)。

- [ ] **Step 6: Commit**

```bash
git add tests/test_generation_routing.py CLAUDE.md
git commit -m "test: end-to-end generation routing (hint priority, capabilities pool, polymorphic models) + update CLAUDE.md

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```

---

## Self-Review 结果

**1. Spec coverage:**
- 决策 1(取消 generation_intent): Task 9 Step 1 ✓
- 决策 2(取消模型名推断): Task 5 classifier 重写(不读 body.model) ✓
- 决策 3(取消 auto): Task 3 Step 5-6 ✓
- 决策 4(capabilities 多选): Task 1 + Task 3 ✓
- 决策 5(撤销每模型 base_url): Task 1 Step 1 ✓
- 决策 6(LLM 预判): Task 4 ✓
- 决策 7(bridge Images/Video 分支): Task 7 + Task 8 ✓
- 决策 8(ai_director prompt 复用): Task 7 Step 5 `_extract_prompt_from_messages` 从 ai_director 改写后的 messages 抽取 ✓
- 决策 9(响应归一): Task 7 Step 3 ✓
- 决策 10(视频异步任务 ID): Task 8 ✓
- 决策 11(预判/ai_director 用廉价文本模型): Task 2 ModelSelector + Task 4 显式传 model + Task 11 ai_director 接入 ✓
- 决策 12(预判输出含 hint): Task 4 ✓
- 异步调用: 全任务 async ✓
- OpenAI 格式: Task 7/8 严格遵循 ✓
- 测试覆盖(tests/ 复用/修改): Task 9 Step 5(含 cache key v2 适配) + 各任务测试 ✓

**2. Placeholder scan:** 无 TBD/TODO/"implement later"。Task 9 Step 5 "逐个修复直到全部通过" 是明确指令(非占位),因失败点依赖运行时发现。

**3. Type consistency:**
- `_resolve_by_intent(intent, model_hint)` 签名: Task 3 定义, Task 10 测试一致 ✓
- `completion(intent=..., model_hint=...)` 参数名: Task 3 定义, Task 4/6 调用一致 ✓
- `IntentClassifier.classify(messages, body_model)` 返回 `{generation, hint}`: Task 4 定义, Task 5 调用一致 ✓
- `ModelSelector.select_text_model()` 返回 str: Task 2 定义, Task 4 调用一致 ✓
- `_model_capabilities` 字段名: Task 3 定义, Task 2/4 mock 一致 ✓
- `generation` 字段值 `understanding|image|video`(非 `generation:image`): Task 4 定义, Task 5 映射到 pipeline_kind `generation:image` ✓

**4. 审核修正记录(5 处):**
- **问题 1(cache key 字面值)**: Task 9 Step 5 显式加 `test_cache_key_v2.py` 适配 —— `generation`→`generation:image` + image/video 互相隔离断言 + 确认 SHA-256 拼接不按冒号切分。
- **问题 2(dispatcher 三处 hardcode)**: Task 6 Step 4 改为列出 512/587/595 三处精确语义(ctx/传 stream/传 nonstream),各改为参数透传,并给 `_dispatch_generation` 加 `pipeline_kind` 参数。
- **问题 3(ModelRouterStrategy 取值不匹配)**: Task 3 Step 4 新增核对步骤,确认 `route(required_modality)` 仍用旧 `llm`/`generative`;`_resolve_by_intent` 不调 `_auto_resolver`(避免选不到模型),池内选首 + hint 优先。
- **问题 4(意图分发位置)**: Task 7 Step 5 给精确锚点 —— 在模型解析块(Step 6)之后、`_do_completion` 调用之前插入,image/video 直接 return。
- **问题 5(显式模型绕过 capability)**: Task 3 Step 6 显式模型直连前加 `required_capability in _model_capabilities[explicit_model]` 校验,不通过则降级池解析。

**注:** ai_director 改用 ModelSelector 已由 Task 11 覆盖(原 Self-Review 标注的偏差已补任务)。

---

## Task 11: ai_director 内部调用经 ModelSelector 选模型

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/director/ai_director.py:247-253`
- Modify: `aigateway-api/src/aigateway_api/main.py`(注入 model_selector 到 ai_director)
- Modify: `tests/test_ai_director_strategy.py`

**Interfaces:**
- Consumes: `ModelSelector.select_text_model()`(Task 2)。
- Produces: `AIDirectorStrategy` 接受可选 `model_selector`;`_do_optimize` 调 `selector.select_text_model()` 取代固定 `config.rewrite_model`。

- [ ] **Step 1: 写失败测试 —— ai_director 用 selector 选模型**

在 `tests/test_ai_director_strategy.py` 加测试:

```python
@pytest.mark.asyncio
async def test_uses_model_selector_when_provided(self, default_config, pipeline_ctx, mock_bridge):
    from aigateway_core.pipelines.generation.director.ai_director import AIDirectorStrategy
    selector = MagicMock()
    selector.select_text_model = AsyncMock(return_value="deepseek-v4-flash")
    strat = AIDirectorStrategy(config=default_config, litellm_bridge=mock_bridge, model_selector=selector)
    await strat.optimize_prompt("a cat", [], default_config, pipeline_ctx)
    call_kwargs = mock_bridge.completion.call_args.kwargs
    assert call_kwargs["model"] == "deepseek-v4-flash"
    assert call_kwargs["intent"] == "understanding"
```

(在文件顶部 import 加 `from unittest.mock import MagicMock` 若缺。)

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_ai_director_strategy.py::TestAIDirectorStrategyOptimizePrompt::test_uses_model_selector_when_provided -v`
Expected: FAIL(`__init__` 不接受 `model_selector`)

- [ ] **Step 3: 改 ai_director 接受 model_selector**

`ai_director.py:87-106` 的 `__init__` 加参数 `model_selector: Any = None`,存 `self._model_selector`。

`_do_optimize`(行 247-253)把:

```python
        response = await self._litellm_bridge.completion(
            messages=messages,
            model=config.rewrite_model,
            temperature=0.7,
            max_tokens=config.max_prompt_length,
            extra_headers=extra_headers,
        )
```

改为:

```python
        rewrite_model = config.rewrite_model
        if self._model_selector is not None:
            try:
                rewrite_model = await self._model_selector.select_text_model()
            except Exception as exc:
                logger.warning("ai_director: model_selector 失败 %s, 用 config.rewrite_model", exc)
        response = await self._litellm_bridge.completion(
            messages=messages,
            model=rewrite_model,
            temperature=0.7,
            max_tokens=config.max_prompt_length,
            extra_headers=extra_headers,
            intent="understanding",
        )
```

- [ ] **Step 4: main.py 注入 model_selector 到 ai_director**

定位 `main.py:508-515`(ai_director 绑定 bridge 处),在 `strategy._litellm_bridge = litellm_bridge` 后加:

```python
                    if hasattr(strategy, "_model_selector") and model_selector is not None:
                        strategy._model_selector = model_selector
```

(`model_selector` 变量来自 Task 9 Step 2 创建的;确保它在 main.py 该作用域可见,若不可见把 ModelSelector 创建提到 ai_director 绑定之前。)

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/test_ai_director_strategy.py -v 2>&1 | tail -20`
Expected: 全 passed(含新测试)

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/director/ai_director.py aigateway-api/src/aigateway_api/main.py tests/test_ai_director_strategy.py
git commit -m "refactor(ai_director): use ModelSelector for internal bridge call (explicit text model, no smart routing)

Co-Authored-By: CodeBuddy Opus 4.8 (1M context) <noreply@Tencent.com>"
```
