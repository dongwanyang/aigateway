"""Verify aigateway_core.pipelines.generation re-exports."""
import importlib


SUBPACKAGES = {
    "director": [
        "aigateway_core.pipelines.generation.director.ai_director",
        "aigateway_core.pipelines.generation.director.ai_director_plugin",
    ],
    "intent": [
        "aigateway_core.pipelines.generation.intent.intent_evaluator",
        "aigateway_core.pipelines.generation.intent.intent_evaluator_plugin",
    ],
    "token": [
        "aigateway_core.pipelines.generation.token.token_compressor",
        "aigateway_core.pipelines.generation.token.feature_cache",
        "aigateway_core.pipelines.generation.token.prompt_confirmation",
        "aigateway_core.pipelines.generation.token.prompt_template_manager",
        "aigateway_core.pipelines.generation.token.video_preview",
        "aigateway_core.pipelines.generation.token.token_compressor_plugin",
    ],
    "draft": [
        "aigateway_core.pipelines.generation.draft.draft_generator",
        "aigateway_core.pipelines.generation.draft.draft_generator_plugin",
    ],
    "cost": [
        "aigateway_core.pipelines.generation.cost.cost_tracker_plugin",
        "aigateway_core.pipelines.generation._common.metrics",
        "aigateway_core.pipelines.generation._common.models",
        "aigateway_core.pipelines.generation._common.api_key_groups",
    ],
    "routing_signals": [
        "aigateway_core.pipelines.generation.routing_signals.gen_model_router_plugin",
    ],
}


def test_generation_subpackages_reexport_expected_sources():
    for subname, sources in SUBPACKAGES.items():
        sub = importlib.import_module(f"aigateway_core.pipelines.generation.{subname}")
        for src_path in sources:
            src_mod = importlib.import_module(src_path)
            for name in dir(src_mod):
                if name.startswith("_"):
                    continue
                assert hasattr(sub, name), (
                    f"aigateway_core.pipelines.generation.{subname} missing "
                    f"{name!r} from {src_path}"
                )
                # Strategy modules take precedence over plugin modules on collisions;
                # verify that the exported object is the same as *some* source's copy.
                assert any(
                    getattr(sub, name) is getattr(importlib.import_module(s), name, object())
                    for s in sources
                    if hasattr(importlib.import_module(s), name)
                ), f"aigateway_core.pipelines.generation.{subname}.{name} matches none of {sources}"


def test_generation_top_level_lists_all_subpackages():
    from aigateway_core.pipelines import generation

    for subname in SUBPACKAGES:
        assert subname in generation.__all__
        assert hasattr(generation, subname)
