"""Group + personal quota check/increment tests."""
import pytest
from aigateway_core.shared.auth.key_store import KeyStore
from aigateway_core.shared.auth.group_store import GroupStore


class FakeRedis:
    """Complete fake async redis (mirrors redis_client convenience methods).
    self.redis = self so both mgr.set_group and mgr.redis.sadd work."""
    def __init__(self):
        self.store = {}
        self.redis = self

    async def hset(self, key, mapping=None, **kw):
        k = key.decode() if isinstance(key, bytes) else key
        d = self.store.setdefault(k, {})
        if mapping:
            for kk, vv in mapping.items():
                d[kk.decode() if isinstance(kk, bytes) else kk] = vv
        return 1

    async def hgetall(self, key):
        k = key.decode() if isinstance(key, bytes) else key
        d = self.store.get(k)
        if not d or not isinstance(d, dict):
            return {}
        return {kk: (vv if isinstance(vv, str) else str(vv)) for kk, vv in d.items()}

    async def hincrby(self, key, field, amount):
        k = key.decode() if isinstance(key, bytes) else key
        d = self.store.setdefault(k, {})
        f = field.decode() if isinstance(field, bytes) else field
        d[f] = str(int(d.get(f, "0")) + amount)
        return int(d[f])

    async def hincrbyfloat(self, key, field, amount):
        k = key.decode() if isinstance(key, bytes) else key
        d = self.store.setdefault(k, {})
        f = field.decode() if isinstance(field, bytes) else field
        d[f] = str(float(d.get(f, "0.0")) + amount)
        return float(d[f])

    async def delete(self, *keys):
        n = 0
        for key in keys:
            k = key.decode() if isinstance(key, bytes) else key
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    async def set(self, key, value, ex=None):
        self.store[key.decode() if isinstance(key, bytes) else key] = value

    async def get(self, key):
        k = key.decode() if isinstance(key, bytes) else key
        v = self.store.get(k)
        return v.encode() if isinstance(v, str) else v

    async def sadd(self, key, *m):
        s = self.store.setdefault(key.decode() if isinstance(key, bytes) else key, set())
        for mm in m:
            s.add(mm.decode() if isinstance(mm, bytes) else mm)
        return len(m)

    async def srem(self, key, *m):
        s = self.store.get(key.decode() if isinstance(key, bytes) else key)
        if not s:
            return 0
        n = 0
        for mm in m:
            mm2 = mm.decode() if isinstance(mm, bytes) else mm
            if mm2 in s:
                s.discard(mm2)
                n += 1
        return n

    async def smembers(self, key):
        s = self.store.get(key.decode() if isinstance(key, bytes) else key)
        return set(s) if s else set()

    async def publish(self, ch, msg):
        return 0

    async def scan(self, cursor, match=None, count=None):
        import fnmatch
        keys = [k for k in self.store.keys() if fnmatch.fnmatch(k, match or "*")]
        return 0, keys

    # convenience methods mirroring redis_client
    async def set_group(self, gid, data):
        await self.hset(f"aigateway:group:{gid}", mapping=data)

    async def get_group(self, gid):
        return await self.hgetall(f"aigateway:group:{gid}") or None

    async def set_api_key(self, kh, data):
        await self.hset(f"aigateway:key:{kh}", mapping=data)

    async def get_api_key(self, kh):
        return await self.hgetall(f"aigateway:key:{kh}") or None

    async def set_key_lookup(self, prefix, kh):
        await self.set(f"aigateway:key_lookup:{prefix}", kh)

    async def set_group_lookup(self, name, gid):
        await self.set(f"aigateway:group_lookup:{name}", gid)

    async def get_group_lookup(self, name):
        v = await self.get(f"aigateway:group_lookup:{name}")
        return v.decode() if isinstance(v, bytes) else v

    async def set_quota(self, ident, period, data):
        await self.hset(f"aigateway:quota:{ident}:{period}", mapping=data)

    async def get_quota(self, ident, period):
        return await self.hgetall(f"aigateway:quota:{ident}:{period}") or None

    # ---- pipeline support for pipe_batch ---
    async def pipe_batch(self, fn):
        """Execute a list of commands built by fn(pipe) atomically."""
        results = []
        store_ref = self.store

        class _FakePipe:
            def __init__(self, owner):
                self._owner = owner

            def hset(self, key, mapping=None, **kw):
                k = key.decode() if isinstance(key, bytes) else key
                d = store_ref.setdefault(k, {})
                if mapping:
                    for kk, vv in mapping.items():
                        kk_key = kk.decode() if isinstance(kk, bytes) else kk
                        d[kk_key] = vv
                results.append(1)
                return results[-1]

            def set(self, key, value, ex=None):
                k = key.decode() if isinstance(key, bytes) else key
                store_ref[k] = value
                results.append(None)
                return results[-1]

            def sadd(self, key, *members):
                s = store_ref.setdefault(
                    key.decode() if isinstance(key, bytes) else key, set()
                )
                n = 0
                for m in members:
                    mm = m.decode() if isinstance(m, bytes) else m
                    s.add(mm)
                    n += 1
                results.append(n)
                return results[-1]

            def srem(self, key, *members):
                s = store_ref.get(
                    key.decode() if isinstance(key, bytes) else key
                )
                if not s:
                    results.append(0)
                    return 0
                n = 0
                for m in members:
                    mm = m.decode() if isinstance(m, bytes) else m
                    if mm in s:
                        s.discard(mm)
                        n += 1
                results.append(n)
                return results[-1]

            def delete(self, *keys):
                n = 0
                for key in keys:
                    k = key.decode() if isinstance(key, bytes) else key
                    if k in store_ref:
                        del store_ref[k]
                        n += 1
                results.append(n)
                return results[-1]

        fn(_FakePipe(self))
        return results


@pytest.fixture
def ks_and_gs():
    mgr = FakeRedis()
    return KeyStore(redis=mgr), GroupStore(redis=mgr)


@pytest.mark.asyncio
async def test_check_group_quota_rejects_when_over(ks_and_gs):
    """Group monthly cost exceeded → reject with 'Group ' prefix."""
    ks, gs = ks_and_gs
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "4999.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-g", "cache_scope": "group",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=10.0)
    assert ok is False
    assert reason.startswith("Group ")
    assert "Monthly" in reason


@pytest.mark.asyncio
async def test_check_personal_quota_when_group_ok(ks_and_gs):
    """Group OK but personal daily tokens exceeded → reject with personal reason."""
    ks, gs = ks_and_gs
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-g", "cache_scope": "group",
        "daily_tokens_limit": "100", "daily_tokens_used": "95",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=0.0)
    assert ok is False
    assert not reason.startswith("Group ")
    assert "Daily" in reason


@pytest.mark.asyncio
async def test_check_both_pass_when_under_limits(ks_and_gs):
    """Both group and key under limits → pass."""
    ks, gs = ks_and_gs
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-g", "cache_scope": "group",
        "daily_tokens_limit": "200", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=1.0)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_increment_syncs_group_and_key(ks_and_gs):
    """increment_usage increments both key and group counters."""
    ks, gs = ks_and_gs
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "5000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-g", "cache_scope": "group",
        "daily_tokens_limit": "200", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.increment_usage("kh1", tokens=50, cost=2.0, model="gpt-4o", tokens_in=40, tokens_out=10)
    kdata = await ks.redis.get_api_key("kh1")
    gdata = await ks.redis.get_group("grp-g")
    assert kdata["daily_tokens_used"] == "50"
    assert gdata["daily_tokens_used"] == "50"
    assert float(kdata["monthly_cost_used"]) == 2.0
    assert float(gdata["monthly_cost_used"]) == 2.0


@pytest.mark.asyncio
async def test_increment_group_failure_does_not_block(ks_and_gs):
    """Key-level increment still works when group doesn't exist."""
    ks, gs = ks_and_gs
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-missing", "cache_scope": "group",
        "daily_tokens_limit": "200", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.increment_usage("kh1", tokens=5, cost=0.1, model="gpt-4o", tokens_in=4, tokens_out=1)
    kdata = await ks.redis.get_api_key("kh1")
    assert kdata["daily_tokens_used"] == "5"  # key still incremented


@pytest.mark.asyncio
async def test_create_with_group_and_cache_scope_persists(ks_and_gs):
    """create() writes group_id + cache_scope and membership atomically."""
    ks, gs = ks_and_gs
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    result = await ks.create(user_id="u1", group_id="grp-g", cache_scope="private")
    kh = ks._hash_key(result["key"])
    kdata = await ks.redis.get_api_key(kh)
    assert kdata["group_id"] == "grp-g"
    assert kdata["cache_scope"] == "private"
    members = await gs._get_members("grp-g")
    assert kh in members


@pytest.mark.asyncio
async def test_assign_key_to_group_migrates_usage(ks_and_gs):
    """Moving a key between groups transfers its used counters."""
    ks, gs = ks_and_gs
    # Create two groups
    g1 = await gs.create_group("Alpha", {"daily_tokens": 10000, "monthly_cost": 100})
    g2 = await gs.create_group("Beta", {"daily_tokens": 10000, "monthly_cost": 100})
    # Create key in group Alpha with some usage
    await ks.redis.set_group(g1["group_id"], {
        "name": "Alpha", "status": "active",
        "daily_tokens_limit": "10000", "daily_tokens_used": "100",
        "monthly_cost_limit": "100", "monthly_cost_used": "5.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_group(g2["group_id"], {
        "name": "Beta", "status": "active",
        "daily_tokens_limit": "10000", "daily_tokens_used": "0",
        "monthly_cost_limit": "100", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": g1["group_id"], "cache_scope": "group",
        "daily_tokens_limit": "5000", "daily_tokens_used": "100",
        "monthly_cost_limit": "50", "monthly_cost_used": "5.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # Assign key to Beta
    await gs.assign_key_to_group("kh1", g2["group_id"])
    # Key now belongs to Beta
    kdata = await ks.redis.get_api_key("kh1")
    assert kdata["group_id"] == g2["group_id"]
    # Alpha lost the key's usage
    g1data = await ks.redis.get_group(g1["group_id"])
    assert g1data["daily_tokens_used"] == "0"
    assert float(g1data["monthly_cost_used"]) == 0.0
    # Beta gained the key's usage
    g2data = await ks.redis.get_group(g2["group_id"])
    assert g2data["daily_tokens_used"] == "100"
    assert float(g2data["monthly_cost_used"]) == 5.0
    # Member sets updated
    assert "kh1" in await gs._get_members(g2["group_id"])
    assert "kh1" not in await gs._get_members(g1["group_id"])


@pytest.mark.asyncio
async def test_assign_key_preserves_other_members_usage(ks_and_gs):
    """Moving one key of many must not wipe the source group's other usage."""
    ks, gs = ks_and_gs
    # Source group with aggregate usage reflecting TWO keys
    await ks.redis.set_group("grp-src", {"name": "Src", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "300",
        "monthly_cost_limit": "5000", "monthly_cost_used": "15.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_group("grp-dst", {"name": "Dst", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # The key being moved personally accounts for 100 tokens / $5 of the group's 300/$15
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-src", "cache_scope": "group",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "100",
        "monthly_cost_limit": "200", "monthly_cost_used": "5.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await gs.assign_key_to_group("kh1", "grp-dst")
    src = await ks.redis.get_group("grp-src")
    dst = await ks.redis.get_group("grp-dst")
    # Source keeps the OTHER member's usage (300 - 100 = 200 tokens, $15 - $5 = $10)
    assert src["daily_tokens_used"] == "200"
    assert float(src["monthly_cost_used"]) == 10.0
    # Destination gained only the moved key's usage
    assert dst["daily_tokens_used"] == "100"
    assert float(dst["monthly_cost_used"]) == 5.0
