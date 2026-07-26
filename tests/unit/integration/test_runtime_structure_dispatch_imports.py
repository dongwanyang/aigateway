from unittest.mock import AsyncMock, MagicMock

import pytest

from aigateway_core.dispatch.classifier import classify_request
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.dispatch.pipeline_engine import PipelineEngine


class DummyConfig:
    def get(self, key, default=None):
        if key != "providers":
            return default
        return {
            "mock": {
                "model_grouper": [
                    {
                        "models": [
                            {"name": "image-gen", "modalities": ["generative"]},
                            {"name": "text-model", "modalities": ["llm"]},
                        ]
                    }
                ]
            }
        }


def test_dispatch_modules_import_from_new_paths():
    ctx = PipelineContext(request={"messages": []}, trace_id="trace-1", pipeline_kind="understanding")
    engine = PipelineEngine(registry=None, pipeline_kind="understanding")
    assert ctx.pipeline_kind == "understanding"
    assert engine.pipeline_kind == "understanding"


@pytest.mark.asyncio
async def test_classify_request_prefers_generation_modalities():
    body = {
        "model": "image-gen",
        "messages": [{"role": "user", "content": "draw a cat"}],
    }
    ic = MagicMock()
    ic.classify = AsyncMock(return_value={"generation": "image", "hint": "None"})
    kind, hint = await classify_request(body, DummyConfig(), intent_classifier=ic)
    assert kind == "generation:image"
    assert hint is None


from aigateway_core.dispatch.dispatcher import RequestDispatcher as CoreRequestDispatcher
from aigateway_api.dispatcher import RequestDispatcher as ApiRequestDispatcher


def test_api_dispatcher_aliases_core_dispatcher():
    assert ApiRequestDispatcher is CoreRequestDispatcher
