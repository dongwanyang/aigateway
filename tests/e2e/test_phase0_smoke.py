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


def test_admin_client_fixture(admin_client):
    r = admin_client.get("/admin/config/debug")
    assert r.status_code == 200
    data = r.json()
    # 5 维度 debug 段应有 5 个 bool 字段(frontend/entry/cache/bridge/plugins_enabled)
    assert isinstance(data, dict)


def test_prom_scrape_parses(prom_scrape):
    snap = prom_scrape.snapshot()
    # gateway 一直有 up gauge 和请求耗时 histogram HELP 行,至少 gateway_ 前缀的 metric 数据点存在
    assert "gateway_up" in snap or \
           "gateway_request_duration_seconds_count" in snap or \
           "gateway_request_duration_seconds_bucket" in snap or \
           any(k.startswith("gateway_") for k in snap), \
           f"No gateway_ metric found. Sample keys: {list(snap.keys())[:10]}"
