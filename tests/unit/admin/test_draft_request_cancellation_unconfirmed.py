from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from aigateway_api.draft_request_routes import cancel_generation_request
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError


def _request(strategy):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(draft_strategy=strategy),
        )
    )


@pytest.mark.asyncio
async def test_cancel_route_reports_unconfirmed_comfyui_release() -> None:
    strategy = SimpleNamespace(
        resolve_request=AsyncMock(return_value=(
            None,
            {
                "draft_id": "",
                "user_id": "user-1",
                "group_id": None,
                "session_id": "session-1",
            },
        )),
        cancel_request=AsyncMock(
            side_effect=DraftWorkflowError(
                "comfyui_cancellation_unconfirmed"
            )
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await cancel_generation_request(
            "request-1",
            _request(strategy),
            chat_session_id="session-1",
            auth={"user_id": "user-1", "group_id": None},
        )

    assert raised.value.status_code == 503
    assert raised.value.detail["error"]["code"] == (
        "comfyui_cancellation_unconfirmed"
    )
    assert "继续跟踪" in raised.value.detail["error"]["message"]
