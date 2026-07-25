"""PromptCompressPlugin request-time behavior."""

from unittest.mock import patch

import pytest

from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.understanding.compression.plugin import (
    PromptCompressPlugin,
)


@pytest.mark.asyncio
async def test_execute_does_not_cold_initialize_compressor() -> None:
    plugin = PromptCompressPlugin()
    ctx = PipelineContext(
        request={"messages": [{"role": "user", "content": "hello world"}]},
        trace_id="trace-prompt-compress-cold",
    )

    with patch.object(plugin, "_init_compressor") as init:
        result = await plugin.execute(ctx)

    init.assert_not_called()
    assert result is ctx
    assert plugin._initialized is False
