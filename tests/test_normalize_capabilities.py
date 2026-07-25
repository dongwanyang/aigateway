"""Coverage for LiteLLMBridge._normalize_model_capabilities — modality→capabilities fallback.

The branch moved capabilities normalization into a dedicated method that must:
- prefer explicit capabilities list
- fall back to legacy `modality` field (llm/mllm→text, generative→image|video by name)
- infer from model name when neither is present
- always return non-empty deduped list
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.route.bridge.litellm_bridge import LiteLLMBridge


def _bridge_with_models(models):
    """Build a bridge whose provider has the given model entries + register caps."""
    providers = {
        "agnes": {
            "api_key": "k",
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model_grouper": [{
                "models": models,
                "fallback_models": [],
                "pricing": {},
            }],
        }
    }
    b = LiteLLMBridge(config={"providers": providers})
    b._build_model_list(providers)
    return b


def test_generative_image_name_infers_image_only():
    """modality=['generative'] + name contains 'image' → ['image']."""
    b = _bridge_with_models([{"name": "agnes-image-2.1-flash", "modality": ["generative"]}])
    assert b._model_capabilities["agnes-image-2.1-flash"] == ["image"]


def test_generative_neither_image_nor_video_infers_both():
    """modality=['generative'] + generic name → ['image', 'video'] (polymorphic)."""
    b = _bridge_with_models([{"name": "agnes-creative-pro", "modality": ["generative"]}])
    assert b._model_capabilities["agnes-creative-pro"] == ["image", "video"]


def test_modality_as_string_normalized():
    """modality given as a bare string (not list) is still honored."""
    b = _bridge_with_models([{"name": "agnes-video-v2.0", "modality": "generative"}])
    assert b._model_capabilities["agnes-video-v2.0"] == ["video"]


def test_modality_llm_mllm_infers_text():
    """modality=['llm','mllm'] → ['text'] (no generative)."""
    b = _bridge_with_models([{"name": "agnes-2.0-flash", "modality": ["llm", "mllm"]}])
    assert b._model_capabilities["agnes-2.0-flash"] == ["text"]


def test_name_based_inference_no_modality():
    """No capabilities, no modality → infer from name (video/image/llm/model/flash)."""
    b = _bridge_with_models([
        {"name": "agnes-video-v2.0"},
        {"name": "agnes-image-2.1"},
        {"name": "agnes-2.0-flash"},
        {"name": "deepseek-llm-chat"},
    ])
    assert b._model_capabilities["agnes-video-v2.0"] == ["video"]
    assert b._model_capabilities["agnes-image-2.1"] == ["image"]
    assert b._model_capabilities["agnes-2.0-flash"] == ["text"]
    assert b._model_capabilities["deepseek-llm-chat"] == ["text"]


def test_empty_everything_defaults_text():
    """No caps, no modality, no inferable name → ['text']."""
    b = _bridge_with_models([{"name": "xyz"}])
    assert b._model_capabilities["xyz"] == ["text"]


def test_capabilities_list_takes_priority_over_modality():
    """When both capabilities and modality present, capabilities wins."""
    b = _bridge_with_models([{
        "name": "agnes-2.0-flash",
        "capabilities": ["text", "image", "video"],
        "modality": ["llm"],
    }])
    assert b._model_capabilities["agnes-2.0-flash"] == ["text", "image", "video"]


def test_capabilities_list_not_deduped_when_explicit():
    """Explicit capabilities list is returned as-is (dedup only applies to inferred path).

    Documents current behavior: callers should provide clean lists.
    """
    b = _bridge_with_models([{
        "name": "agnes-2.0-flash",
        "capabilities": ["text", "text", "image"],
    }])
    # Not deduped — returned verbatim. Test pins this so a future change is intentional.
    assert b._model_capabilities["agnes-2.0-flash"] == ["text", "text", "image"]


def test_inferred_capabilities_are_deduped():
    """The inferred (modality/name) path dedups via dict.fromkeys.

    Construct a case where inference could double-add: modality has both 'llm'
    and 'mllm' (only one 'text' append via OR) — combined with name inference
    that would also add 'text', dedup keeps it single.
    """
    # 'agnes-2.0-flash' name → 'flash' → text; modality generative → image+video
    # No overlap here, but verify the path produces a clean deduped list.
    b = _bridge_with_models([{
        "name": "agnes-2.0-flash",
        "modality": ["generative", "llm"],
    }])
    caps = b._model_capabilities["agnes-2.0-flash"]
    # generative + name has no video/image token → [image, video]; llm → text
    assert caps == ["text", "image", "video"]
    assert len(caps) == len(set(caps))


def test_non_list_capabilities_falls_back_to_modality():
    """capabilities set to a non-list (e.g. str) → warn + use modality fallback."""
    b = _bridge_with_models([{
        "name": "agnes-video-v2.0",
        "capabilities": "video",  # wrong type
        "modality": ["generative"],
    }])
    assert b._model_capabilities["agnes-video-v2.0"] == ["video"]


def test_empty_capabilities_list_falls_back_to_modality():
    """Empty capabilities list ([]) → fall back to modality (not [])."""
    b = _bridge_with_models([{
        "name": "agnes-video-v2.0",
        "capabilities": [],
        "modality": ["generative"],
    }])
    assert b._model_capabilities["agnes-video-v2.0"] == ["video"]


@pytest.mark.asyncio
async def test_resolve_video_intent_with_modality_only_config():
    """End-to-end: modality-only config still routes video intent to a video-capable model."""
    b = _bridge_with_models([
        {"name": "agnes-2.0-flash", "modality": ["llm", "mllm"]},
        {"name": "agnes-video-v2.0", "modality": ["generative"]},
    ])
    resolved = await b._resolve_by_intent(intent="generation:video", model_hint=None)
    assert resolved["model"] == "agnes-video-v2.0"
    assert resolved["meta"]["intent"] == "generation:video"
