"""Drain-key ownership contract for GPU generation claims (needs Redis Stack).

The rule lives in the Lua body of
``GpuResourceCoordinator._redis_claim_generation``, so it can only be verified by
executing the script. A fake Redis would have to reimplement the comparison and
would therefore assert its own behavior instead of the scheduler's.

Regression: while a post-generation ``comfyui_idle`` reservation was treated as a
foreign owner, every queued generation on a single-GPU host waited out the full
``comfyui_idle_reservation_seconds`` window, and queued drafts failed with
``generation_wait_timeout`` while ComfyUI kept producing results nobody polled.
"""
from __future__ import annotations

import inspect
import re

import pytest
import redis
from aigateway_core.shared.gpu_scheduler import GpuResourceCoordinator

from tests.conftest import REDIS_URL

CLAIM_UNOWNED = 1
CLAIM_DRAINING = 2
CLAIM_REJECTED = 0


def _claim_script() -> str:
    source = inspect.getsource(GpuResourceCoordinator._redis_claim_generation)
    match = re.search(r'script = """(.*?)"""', source, re.DOTALL)
    assert match, "claim script body not found"
    return match.group(1)


@pytest.fixture()
def claim(unique_prefix):
    client = redis.from_url(REDIS_URL, decode_responses=True)
    script = client.register_script(_claim_script())
    leases_key = f"{unique_prefix}leases:dev"
    drain_key = f"{unique_prefix}drain:dev"
    lease_prefix = f"{unique_prefix}lease:"

    def _run(ticket: str, *, owner: str | None = None) -> int:
        if owner is None:
            client.delete(drain_key)
        else:
            client.set(drain_key, owner, ex=60)
        return int(
            script(keys=[leases_key, drain_key], args=[lease_prefix, ticket, 15])
        )

    _run.client = client
    _run.keys = (leases_key, drain_key, lease_prefix)
    try:
        yield _run
    finally:
        client.delete(leases_key, drain_key, f"{lease_prefix}L1")
        client.close()


def test_unowned_device_is_claimable(claim):
    assert claim("ticket-a") == CLAIM_UNOWNED


def test_idle_reservation_is_claimable_by_a_queued_generation(claim):
    """The core fix: keeping weights warm must not delay the next generation."""
    assert claim("ticket-b", owner="comfyui_idle") == CLAIM_UNOWNED
    _, drain_key, _ = claim.keys
    assert claim.client.get(drain_key) == "ticket-b"


def test_another_live_generation_still_blocks(claim):
    assert claim("ticket-c", owner="ticket-b") == CLAIM_REJECTED


def test_in_flight_model_unload_still_blocks(claim):
    """An eviction holds the key under comfyui_release:*; do not race it."""
    assert claim("ticket-d", owner="comfyui_release:abc") == CLAIM_REJECTED


def test_owner_can_renew_its_own_claim(claim):
    assert claim("ticket-e", owner="ticket-e") == CLAIM_UNOWNED


def test_active_gateway_lease_reports_draining(claim):
    leases_key, _drain_key, lease_prefix = claim.keys
    claim.client.sadd(leases_key, "L1")
    claim.client.set(f"{lease_prefix}L1", "held", ex=30)
    assert claim("ticket-f") == CLAIM_DRAINING
