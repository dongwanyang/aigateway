"""httpx clients for admin and test-user identities."""
import pytest
import httpx
from typing import Optional

from tests.conftest import BASE, ADMIN_KEY, AGNES_TEXT_MODEL


@pytest.fixture
def admin_client():
    """Admin-authenticated httpx client (Bearer <ADMIN_KEY>)."""
    c = httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    yield c
    c.close()


@pytest.fixture
def user_client(admin_client, unique_prefix):
    """Fresh non-admin API key for this test, cleaned up on teardown."""
    resp = admin_client.post(
        "/admin/api-keys",
        json={
            "user_id": f"{unique_prefix}user",
            "quotas": {
                "daily_tokens": 1000000,
                "monthly_cost": 50.0,
                "rate_limit_rpm": 60,
                "rate_limit_tpm": 100000,
            },
        },
    )
    if resp.status_code not in (200, 201):
        pytest.skip(f"Cannot create test user key: {resp.status_code} {resp.text}")
    data = resp.json()
    key_value = data.get("key") or data.get("api_key") or data.get("value")
    key_id = data.get("key_id") or data.get("id")
    assert key_value, f"Unexpected /admin/api-keys response shape: {data}"
    c = httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {key_value}"},
        timeout=60,
    )
    yield c
    c.close()
    if key_id:
        admin_client.delete(f"/admin/api-keys/{key_id}")


def chat(
    client: httpx.Client,
    prompt: str,
    model: str = AGNES_TEXT_MODEL,
    trace_id: Optional[str] = None,
    **extra_body,
) -> httpx.Response:
    """POST /v1/chat/completions with a single-user-message body.

    Additional body keys (generation_intent, stream, etc.) merged from **extra_body.
    Additional X-Request-ID header injected when trace_id is provided.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    body.update(extra_body)
    headers = {}
    if trace_id:
        headers["X-Request-ID"] = trace_id
    return client.post("/v1/chat/completions", json=body, headers=headers)
