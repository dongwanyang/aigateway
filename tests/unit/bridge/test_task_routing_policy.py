import pytest
from aigateway_core.route.model_resolution.policy_engine import (
    NoModelSatisfiesPolicy,
    RoutingPolicyConfigError,
    RoutingPolicyEngine,
)
from aigateway_core.route.model_resolution.runtime_router import RuntimeModelRouter
from aigateway_core.route.model_resolution.task_classifier import (
    TaskClassifier,
    TaskProfile,
)


def _profile(operation: str, confidence: float = 0.9, complexity: int = 50):
    return TaskProfile(
        operation=operation,
        confidence=confidence,
        complexity=complexity,
        source="test",
    )


def test_classifier_recognizes_coding_in_chinese():
    result = TaskClassifier().classify(
        [{"role": "user", "content": "帮我写一个 Python 函数并补充单元测试"}]
    )
    assert result.operation == "coding"
    assert result.domain == "software"
    assert result.confidence >= 0.8


def test_classifier_builds_multidimensional_vision_profile():
    result = TaskClassifier().classify([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "解释这里的内容"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ],
        }
    ])
    assert result.operation == "vision"
    assert "image" in result.modalities
    assert "vision" in result.requirements


def test_summary_operation_wins_over_code_subject():
    result = TaskClassifier().classify(
        [{"role": "user", "content": "请总结这段 Python 代码的主要逻辑"}]
    )
    assert result.operation == "summary"
    assert result.domain == "software"


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("请分析这份财报中的主要风险并给出依据", "reasoning"),
        ("求解这个方程：2x + 3 = 11", "reasoning"),
        ("比较两个方案并说明哪个更合理", "reasoning"),
        ("阅读合同并找出逻辑矛盾", "reasoning"),
        ("What is an API gateway?", "general"),
    ],
)
def test_adversarial_task_profiles(prompt, expected):
    assert TaskClassifier().classify(
        [{"role": "user", "content": prompt}]
    ).operation == expected


def test_policy_only_returns_constraints_not_final_model():
    engine = RoutingPolicyEngine({
        "enabled": True,
        "model_preferences": {"coding": ["qwen-coder", "deepseek-coder"]},
    })
    constraints = engine.constrain(
        profile=_profile("coding"),
        candidates=["gpt-4", "deepseek-coder", "qwen-coder"],
        model_hint="gpt-4",
        model_tasks={
            "gpt-4": ["general"],
            "deepseek-coder": ["coding"],
            "qwen-coder": ["coding"],
        },
    )
    assert constraints.eligible_models == ("deepseek-coder", "qwen-coder")
    assert constraints.preferred_models == ("qwen-coder", "deepseek-coder")
    assert not hasattr(constraints, "model")


def test_runtime_router_excludes_open_preferred_model():
    constraints = RoutingPolicyEngine({
        "enabled": True,
        "model_preferences": {"coding": ["qwen-coder", "deepseek-coder"]},
    }).constrain(
        profile=_profile("coding"),
        candidates=["qwen-coder", "deepseek-coder"],
        model_hint=None,
        model_tasks={"qwen-coder": ["coding"], "deepseek-coder": ["coding"]},
    )
    decision = RuntimeModelRouter().route(
        constraints,
        health={
            "qwen-coder": {"state": "OPEN", "failure_count": 5},
            "deepseek-coder": {"state": "CLOSED", "failure_count": 0},
        },
        pricing={},
        capability_scores={},
        latency_ms={},
    )
    assert decision.model == "deepseek-coder"
    assert decision.excluded_unhealthy == ("qwen-coder",)


def test_runtime_router_prefers_complexity_adequacy_over_static_preference():
    constraints = RoutingPolicyEngine({
        "enabled": True,
        "model_preferences": {"reasoning": ["small", "large"]},
    }).constrain(
        profile=_profile("reasoning", complexity=90),
        candidates=["small", "large"],
        model_hint=None,
        model_tasks={"small": ["reasoning"], "large": ["reasoning"]},
    )
    decision = RuntimeModelRouter().route(
        constraints,
        health={},
        pricing={"small": {"prompt": 0.01}, "large": {"prompt": 0.1}},
        capability_scores={"small": 50, "large": 95},
        latency_ms={"small": 10, "large": 100},
    )
    assert decision.model == "large"
    assert decision.fallback_models == ("small",)


def test_strict_mode_preserves_explicit_model_contract():
    constraints = RoutingPolicyEngine({
        "enabled": True,
        "model_selection_mode": "strict",
    }).constrain(
        profile=_profile("coding"),
        candidates=["general", "coder"],
        model_hint="general",
        model_tasks={"general": ["general"], "coder": ["coding"]},
    )
    assert constraints.eligible_models == ("general",)
    assert constraints.reason == "strict_model_contract"


def test_low_confidence_keeps_full_pool_and_hint_preference():
    constraints = RoutingPolicyEngine({
        "enabled": True,
        "min_confidence": 0.8,
    }).constrain(
        profile=_profile("reasoning", confidence=0.6),
        candidates=["gpt-4", "reasoner"],
        model_hint="gpt-4",
        model_tasks={"gpt-4": ["general"], "reasoner": ["reasoning"]},
    )
    assert constraints.eligible_models == ("gpt-4", "reasoner")
    assert constraints.preferred_models == ("gpt-4",)


def test_hard_feature_requirement_filters_before_task_preference():
    profile = TaskProfile(
        operation="coding",
        modalities=("text", "image"),
        requirements=("vision",),
        confidence=0.9,
    )
    constraints = RoutingPolicyEngine({
        "enabled": True,
        "model_preferences": {"coding": ["coder"]},
    }).constrain(
        profile=profile,
        candidates=["coder", "vision-general"],
        model_hint=None,
        model_tasks={"coder": ["coding"], "vision-general": ["vision"]},
        model_features={"coder": [], "vision-general": ["vision"]},
    )
    assert constraints.eligible_models == ("vision-general",)


def test_missing_hard_feature_rejects_instead_of_guessing():
    with pytest.raises(NoModelSatisfiesPolicy):
        RoutingPolicyEngine({"enabled": True}).constrain(
            profile=TaskProfile(
                operation="coding",
                requirements=("tool_calling",),
                confidence=0.9,
            ),
            candidates=["model-a"],
            model_hint=None,
            model_tasks={"model-a": ["coding"]},
            model_features={"model-a": []},
        )


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "maybe"},
        {"min_confidence": 2},
        {"model_selection_mode": "surprise"},
        {"model_preferences": {"unknown": ["model-a"]}},
    ],
)
def test_policy_rejects_invalid_configuration(config):
    with pytest.raises(RoutingPolicyConfigError):
        RoutingPolicyEngine(config)


def test_policy_rejects_unregistered_preference_at_startup_validation():
    engine = RoutingPolicyEngine({
        "enabled": True,
        "model_preferences": {"coding": ["missing-coder"]},
    })
    with pytest.raises(RoutingPolicyConfigError, match="unregistered"):
        engine.validate_models(
            registered_models=["general"],
            model_tasks={"general": ["general"]},
            model_features={"general": []},
        )


def test_task_profile_parses_clamps_and_serializes_untrusted_llm_payload():
    profile = TaskProfile.from_dict(
        {
            "task": "CODING",
            "domain": "  Software " + "x" * 80,
            "modalities": ["TEXT", "image", "image", "unsupported"],
            "requirements": ["VISION", "vision", "unknown"],
            "complexity": 999,
            "confidence": -2,
        },
        source="llm-test",
    )

    assert profile.task == "coding"
    assert profile.modalities == ("text", "image")
    assert profile.requirements == ("vision",)
    assert profile.complexity == 100
    assert profile.confidence == 0.0
    assert profile.source == "llm-test"
    serialized = profile.as_dict()
    assert serialized["task"] == "coding"
    assert serialized["modalities"] == ["text", "image"]
    assert serialized["requirements"] == ["vision"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "object"),
        ({"operation": "unknown"}, "unsupported operation"),
        ({"modalities": "text"}, "modalities must be a list"),
        ({"requirements": "vision"}, "requirements must be a list"),
        ({"complexity": True}, "must be numeric"),
        ({"confidence": "not-a-number"}, "must be numeric"),
    ],
)
def test_task_profile_rejects_malformed_llm_payload(payload, message):
    with pytest.raises(ValueError, match=message):
        TaskProfile.from_dict(payload)


def test_classifier_extracts_recent_multimodal_user_content_and_requirements():
    profile = TaskClassifier().classify(
        [
            "not a message",
            {"role": "assistant", "content": "write code"},
            {"role": "user", "content": {"unexpected": "shape"}},
            {
                "role": "user",
                "content": [
                    None,
                    {"type": "input_text", "text": "Please analyze this diagram"},
                    {"type": "input_image"},
                    {"type": "input_audio"},
                    {"type": "video_url"},
                    {"type": "unknown"},
                ],
            },
        ],
        tools=[{"type": "function"}],
        structured_output=True,
    )

    assert profile.modalities == ("text", "image", "audio", "video")
    assert profile.requirements == (
        "vision",
        "tool_calling",
        "structured_output",
    )
    assert profile.operation == "vision"
    assert TaskClassifier().is_high_confidence(profile, threshold=0.5)
    assert not TaskClassifier().is_high_confidence(profile, threshold=1.0)


def test_policy_disabled_and_auto_mode_have_distinct_hint_behavior():
    profile = _profile("coding")
    disabled = RoutingPolicyEngine().constrain(
        profile,
        candidates=["general", "coder", "coder"],
        model_hint="general",
        model_tasks={},
    )
    assert disabled.as_dict() == {
        "eligible_models": ["general", "coder"],
        "preferred_models": ["general"],
        "policy_reason": "policy_disabled",
        "unmet_requirements": [],
    }

    automatic = RoutingPolicyEngine(
        {
            "enabled": "true",
            "expose_debug_metadata": "false",
            "model_selection_mode": "auto",
            "model_preferences": {"coding": ["coder", "coder"]},
        }
    ).constrain(
        profile,
        candidates=["general", "coder"],
        model_hint="general",
        model_tasks={"general": ["*"], "coder": ["coding"]},
    )
    assert automatic.eligible_models == ("general", "coder")
    assert automatic.preferred_models == ("coder",)


def test_policy_handles_unconfigured_task_metadata_and_ignores_unknown_requirement():
    profile = TaskProfile(
        operation="coding",
        confidence=0.9,
        requirements=("future_requirement",),
    )
    constraints = RoutingPolicyEngine({"enabled": True}).constrain(
        profile,
        candidates=["general"],
        model_hint="general",
        model_tasks={"general": []},
    )
    assert constraints.reason == "task_metadata_unconfigured"
    assert constraints.eligible_models == ("general",)
    assert constraints.preferred_models == ("general",)


def test_policy_rejects_empty_pool_and_invalid_model_metadata():
    engine = RoutingPolicyEngine({"enabled": True})
    with pytest.raises(ValueError, match="must not be empty"):
        engine.constrain(_profile("general"), [], None, {})
    with pytest.raises(RoutingPolicyConfigError, match="unknown tasks"):
        engine.validate_models(["m"], {"m": ["future-task"]}, {"m": []})
    with pytest.raises(RoutingPolicyConfigError, match="unknown features"):
        engine.validate_models(["m"], {"m": ["*"]}, {"m": ["future-feature"]})


@pytest.mark.parametrize(
    "config",
    [
        {"version": "  "},
        {"min_confidence": True},
        {"min_confidence": "not-a-number"},
        {"expose_debug_metadata": 1},
        {"model_preferences": []},
        {"model_preferences": {"coding": "coder"}},
        {"model_preferences": {"coding": [""]}},
    ],
)
def test_policy_rejects_additional_unsafe_configuration(config):
    with pytest.raises(RoutingPolicyConfigError):
        RoutingPolicyEngine(config)


@pytest.mark.asyncio
async def test_bridge_applies_profile_policy_and_runtime_router():
    from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge

    bridge = LiteLLMBridge({
        "task_routing": {
            "enabled": True,
            "model_preferences": {"coding": ["coder"]},
            "expose_debug_metadata": True,
        }
    })
    bridge._model_alias_map = {"general": "openai/general", "coder": "openai/coder"}
    bridge._model_capabilities = {"general": ["text"], "coder": ["text"]}
    bridge._model_tasks = {"general": ["general"], "coder": ["coding"]}
    bridge._model_features = {"general": [], "coder": []}

    resolved = await bridge._resolve_by_intent(
        intent="understanding",
        model_hint="general",
        messages=[{"role": "user", "content": "帮我写代码"}],
        apply_task_policy=True,
    )

    assert resolved["model"] == "coder"
    assert resolved["meta"]["task"]["operation"] == "coding"
    assert resolved["meta"]["policy_applied"] is True


@pytest.mark.asyncio
async def test_bridge_normalizes_full_model_cooldown_status():
    from aigateway_core.route.bridge.cooldown import ProviderCooldownTracker
    from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge

    bridge = LiteLLMBridge({
        "task_routing": {
            "enabled": True,
            "model_preferences": {"coding": ["coder-a", "coder-b"]},
        }
    })
    bridge._model_alias_map = {
        "coder-a": "openai/coder-a",
        "coder-b": "openai/coder-b",
    }
    bridge._model_capabilities = {"coder-a": ["text"], "coder-b": ["text"]}
    bridge._model_tasks = {"coder-a": ["coding"], "coder-b": ["coding"]}
    bridge._model_features = {"coder-a": [], "coder-b": []}
    tracker = ProviderCooldownTracker(allowed_fails=1)
    tracker.on_failure("openai/coder-a")
    bridge._cooldown_tracker = tracker

    resolved = await bridge._resolve_by_intent(
        intent="understanding",
        model_hint=None,
        task_profile=_profile("coding"),
        apply_task_policy=True,
    )
    assert resolved["model"] == "coder-b"
    assert resolved["meta"]["excluded_unhealthy"] == ["coder-a"]


@pytest.mark.asyncio
async def test_stream_first_chunk_contains_routing_metadata():
    from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge

    class _Stream:
        sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return {
                "model": "openai/coder",
                "choices": [{"index": 0, "delta": {"content": "ok"}}],
            }

    class _Router:
        async def acompletion(self, **kwargs):
            return _Stream()

    bridge = LiteLLMBridge({
        "task_routing": {
            "enabled": True,
            "model_preferences": {"coding": ["coder"]},
            "expose_debug_metadata": True,
        }
    })
    bridge.router = _Router()
    bridge._model_alias_map = {"general": "openai/general", "coder": "openai/coder"}
    bridge._model_capabilities = {"general": ["text"], "coder": ["text"]}
    bridge._model_tasks = {"general": ["general"], "coder": ["coding"]}
    bridge._model_features = {"general": [], "coder": []}

    chunks = [
        chunk
        async for chunk in bridge.completion_stream(
            messages=[{"role": "user", "content": "帮我写代码"}],
            model="general",
            apply_task_routing=True,
        )
    ]
    meta = chunks[0]["_meta"]
    assert meta["routed_to"]["model"] == "coder"
    assert meta["model_router"]["task"]["operation"] == "coding"
