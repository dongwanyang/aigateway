import pytest
from aigateway_api.openai_compat import ChatCompletionRequest
from pydantic import ValidationError


@pytest.mark.parametrize("session_id", [".hidden", "-leading", "_leading", ":leading"])
def test_chat_session_id_rejects_non_alphanumeric_prefix(session_id):
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            model="auto",
            messages=[{"role": "user", "content": "hello"}],
            chat_session_id=session_id,
        )


@pytest.mark.parametrize("session_id", ["a", "session-1", "grp.team:session_1"])
def test_chat_session_id_accepts_strategy_safe_values(session_id):
    request = ChatCompletionRequest(
        model="auto",
        messages=[{"role": "user", "content": "hello"}],
        chat_session_id=session_id,
    )

    assert request.chat_session_id == session_id
