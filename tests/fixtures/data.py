"""测试数据隔离:命名前缀 + teardown 精准清理.

规则:
- 每个 test function 拿一个独立 unique_prefix(如 test-e2e-a3f0b1c2-)
- test 用它去命名一切写入 redis/qdrant 的东西
- test 结束后 cleanup_test_data 直连 redis 和 qdrant 扫删该前缀
"""
import sys
import uuid
import pytest
import redis as _redis
import httpx

from tests.conftest import REDIS_URL, QDRANT_URL


@pytest.fixture
def unique_prefix():
    """每个测试独立前缀 test-e2e-<uuid8>-."""
    return f"test-e2e-{uuid.uuid4().hex[:8]}-"


def _scan_and_delete_redis_keys(pattern: str) -> int:
    """SCAN + DEL 匹配 pattern 的 redis key。返回删除数量。"""
    r = _redis.from_url(REDIS_URL, decode_responses=True)
    deleted = 0
    try:
        for key in r.scan_iter(match=pattern, count=200):
            r.delete(key)
            deleted += 1
    finally:
        r.close()
    return deleted


def _scan_and_delete_qdrant_points_by_prefix(prefix: str) -> int:
    """扫所有 collection,删 payload 里带 prefix 的 point。

    实现:list_collections → 每个 collection 用 scroll 拉 point,
    payload 里任一字段 startswith(prefix) 则 delete。
    """
    deleted = 0
    try:
        cols = httpx.get(f"{QDRANT_URL}/collections", timeout=5).json()
        for c in cols.get("result", {}).get("collections", []):
            name = c["name"]
            # scroll with a filter — qdrant supports payload-value match, but
            # startswith is not a native filter, so pull page and filter client-side
            offset = None
            while True:
                body = {"limit": 100, "with_payload": True, "with_vector": False}
                if offset is not None:
                    body["offset"] = offset
                resp = httpx.post(
                    f"{QDRANT_URL}/collections/{name}/points/scroll",
                    json=body, timeout=10,
                )
                if resp.status_code != 200:
                    break
                result = resp.json().get("result", {})
                points = result.get("points", [])
                if not points:
                    break
                to_del = []
                for p in points:
                    payload = p.get("payload") or {}
                    if any(
                        isinstance(v, str) and prefix in v
                        for v in payload.values()
                    ):
                        to_del.append(p["id"])
                if to_del:
                    httpx.post(
                        f"{QDRANT_URL}/collections/{name}/points/delete",
                        json={"points": to_del}, timeout=10,
                    )
                    deleted += len(to_del)
                offset = result.get("next_page_offset")
                if offset is None:
                    break
    except Exception as exc:
        # Log cleanup error but don't fail the test on teardown
        print(f"[WARN] Qdrant cleanup failed for prefix {unique_prefix}: {exc}", file=sys.stderr)
    return deleted


@pytest.fixture(autouse=True)
def cleanup_test_data(request, unique_prefix):
    """每个 e2e/ui 用例结束后扫删本 test 前缀的 redis + qdrant 数据.

    仅对 tests/e2e 和 tests/ui 下的用例生效,不干扰单元测试。
    """
    yield
    testpath = str(request.node.fspath)
    if "/tests/e2e/" not in testpath and "/tests/ui/" not in testpath:
        return
    _scan_and_delete_redis_keys(f"*{unique_prefix}*")
    _scan_and_delete_qdrant_points_by_prefix(unique_prefix)
