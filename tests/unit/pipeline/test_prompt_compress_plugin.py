"""PromptCompressPlugin request-time behavior."""

from unittest.mock import AsyncMock, patch

import pytest
from aigateway_core.dispatch.context import PipelineContext
from aigateway_core.pipelines.understanding.compression.plugin import (
    PromptCompressPlugin,
)


@pytest.mark.asyncio
async def test_execute_lazy_initializes_compressor_off_event_loop() -> None:
    plugin = PromptCompressPlugin()
    ctx = PipelineContext(
        request={"messages": [{"role": "user", "content": "hello world"}]},
        trace_id="trace-prompt-compress-cold",
    )

    async def _offload(func, **kwargs):
        return func(**kwargs)

    with (
        patch.object(plugin, "_init_compressor") as init,
        patch(
            "aigateway_core.pipelines.understanding.compression._plugin_impl.asyncio.to_thread",
            new=AsyncMock(side_effect=_offload),
        ) as to_thread,
    ):
        result = await plugin.execute(ctx)

    init.assert_called_once()
    to_thread.assert_awaited_once()
    assert result is ctx
    assert plugin._initialized is True
