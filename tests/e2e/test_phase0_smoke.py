"""Phase 0 smoke tests — 证明 fixtures/常量/健康检查都跑得起来."""
import httpx
import redis
from tests.conftest import BASE, REDIS_URL, QDRANT_URL, ADMIN_KEY


def test_gateway_health():
    r = httpx.get(f"{BASE}/health", timeout=15)
    assert r.status_code == 200


def test_admin_key_present():
    assert ADMIN_KEY and ADMIN_KEY.startswith("gw-")


def test_redis_reachable():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    assert r.ping()
    r.close()


def test_qdrant_reachable():
    r = httpx.get(f"{QDRANT_URL}/collections", timeout=3)
    assert r.status_code == 200


def test_unique_prefix_fixture(unique_prefix):
    assert unique_prefix.startswith("test-e2e-")
    assert len(unique_prefix) == len("test-e2e-XXXXXXXX-")
