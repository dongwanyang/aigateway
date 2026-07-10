"""GroupStore unit tests - group CRUD, members, migration.

Uses a minimal fake async redis (mirrors redis_client convenience method
names: set_group/get_group/set_api_key/get_api_key/set_quota/get_quota/...)
to avoid a live Redis dependency.
"""
import pytest
from aigateway_core.shared.auth.group_store import GroupStore, slugify
from aigateway_core.shared.auth.key_store import KeyStore


class FakeRedis:
    """Fake async redis: acts as both manager (convenience methods) and raw
    client (self.redis = self). Mirrors redis_client convenience method names
    (set_group/get_group/set_api_key/get_api_key/set_quota/get_quota/...)."""
    def __init__(self):
        self.store = {}  # full-key -> dict | set | str
        self.redis = self  # so mgr.redis.sadd AND mgr.set_group both work

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
        f = field if isinstance(field, str) else field.decode()
        d[f] = str(int(d.get(f, "0")) + amount)
        return int(d[f])

    async def hincrbyfloat(self, key, field, amount):
        k = key.decode() if isinstance(key, bytes) else key
        d = self.store.setdefault(k, {})
        f = field if isinstance(field, str) else field.decode()
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

    async def sadd(self, key, *members):
        s = self.store.setdefault(key.decode() if isinstance(key, bytes) else key, set())
        for m in members:
            s.add(m.decode() if isinstance(m, bytes) else m)
        return len(members)

    async def srem(self, key, *members):
        s = self.store.get(key.decode() if isinstance(key, bytes) else key)
        if not s:
            return 0
        n = 0
        for m in members:
            mm = m.decode() if isinstance(m, bytes) else m
            if mm in s:
                s.discard(mm)
                n += 1
        return n

    async def smembers(self, key):
        s = self.store.get(key.decode() if isinstance(key, bytes) else key)
        return set(s) if s else set()

    async def publish(self, channel, message):
        return 0

    async def scan(self, cursor, match=None, count=None):
        import fnmatch
        keys = [k for k in self.store.keys() if fnmatch.fnmatch(k, match or "*")]
        return 0, keys

    # ---- convenience methods mirroring redis_client.RedisClientManager ----
    async def set_group(self, gid, data):
        await self.hset(f"aigateway:group:{gid}", mapping=data)

    async def get_group(self, gid):
        raw = await self.hgetall(f"aigateway:group:{gid}")
        return raw or None

    async def delete_group(self, gid):
        return bool(await self.delete(f"aigateway:group:{gid}"))

    async def set_group_lookup(self, name, gid):
        await self.set(f"aigateway:group_lookup:{name}", gid)

    async def get_group_lookup(self, name):
        v = await self.get(f"aigateway:group_lookup:{name}")
        return v.decode() if isinstance(v, bytes) else v

    async def delete_group_lookup(self, name):
        await self.delete(f"aigateway:group_lookup:{name}")

    async def set_api_key(self, kh, data):
        await self.hset(f"aigateway:key:{kh}", mapping=data)

    async def get_api_key(self, kh):
        raw = await self.hgetall(f"aigateway:key:{kh}")
        return raw or None

    async def set_key_lookup(self, prefix, kh):
        await self.set(f"aigateway:key_lookup:{prefix}", kh)

    async def set_quota(self, ident, period, data):
        await self.hset(f"aigateway:quota:{ident}:{period}", mapping=data)

    async def get_quota(self, ident, period):
        raw = await self.hgetall(f"aigateway:quota:{ident}:{period}")
        return raw or None

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
                    before = len(s)
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

    async def pipeline(self, transaction=True):
        """Legacy: return a real-time pipeline stub that executes immediately."""
        return self


@pytest.fixture
def store():
    return GroupStore(redis=FakeRedis())


def test_slugify():
    assert slugify("Admin Team") == "admin-team"
    assert slugify("Dev/Ops 2") == "dev-ops-2"
    assert slugify("  中文 组 ") == "中文-组"  # space -> '-', CJK kept


@pytest.mark.asyncio
async def test_create_group_returns_id_and_persists(store):
    g = await store.create_group("Admin Team", {"daily_tokens": 5000, "monthly_cost": 100})
    assert g["group_id"] == "grp-admin-team"
    assert g["name"] == "Admin Team"
    fetched = await store.get_group("grp-admin-team")
    assert fetched["name"] == "Admin Team"
    assert fetched["daily_tokens_limit"] == "5000"
    assert fetched["status"] == "active"


@pytest.mark.asyncio
async def test_create_group_duplicate_name_rejected(store):
    await store.create_group("Admin Team", {})
    with pytest.raises(ValueError):
        await store.create_group("Admin Team", {})


@pytest.mark.asyncio
async def test_list_groups(store):
    await store.create_group("Alpha", {})
    await store.create_group("Beta", {})
    names = sorted(g["name"] for g in await store.list_groups())
    assert names == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_update_group(store):
    g = await store.create_group("G", {"daily_tokens": 100})
    await store.update_group(g["group_id"], quotas={"daily_tokens": 999}, status="suspended")
    fetched = await store.get_group(g["group_id"])
    assert fetched["daily_tokens_limit"] == "999"
    assert fetched["status"] == "suspended"


@pytest.mark.asyncio
async def test_delete_group(store):
    g = await store.create_group("G", {})
    assert await store.delete_group(g["group_id"]) is True
    assert await store.get_group(g["group_id"]) is None


@pytest.mark.asyncio
async def test_add_remove_member(store):
    g = await store.create_group("G", {})
    await store.add_member(g["group_id"], "keyhashA")
    await store.add_member(g["group_id"], "keyhashB")
    assert await store.get_member_count(g["group_id"]) == 2
    await store.remove_member(g["group_id"], "keyhashA")
    assert await store.get_member_count(g["group_id"]) == 1


@pytest.mark.asyncio
async def test_list_groups_includes_member_count(store):
    g = await store.create_group("G", {"daily_tokens": 5000})
    await store.add_member(g["group_id"], "kh1")
    groups = await store.list_groups()
    assert groups[0]["member_count"] == 1
    assert groups[0]["daily_tokens_limit"] == "5000"


@pytest.mark.asyncio
async def test_delete_group_with_members_rejected(store):
    g = await store.create_group("G", {})
    await store.add_member(g["group_id"], "kh1")
    with pytest.raises(ValueError):
        await store.delete_group(g["group_id"])


@pytest.mark.asyncio
async def test_default_group_cannot_be_deleted(store):
    await store.ensure_default_group()
    with pytest.raises(ValueError):
        await store.delete_group(GroupStore.DEFAULT_GROUP_ID)


@pytest.mark.asyncio
async def test_get_group_detail(store):
    g = await store.create_group("G", {})
    await store.add_member(g["group_id"], "kh1")
    detail = await store.get_group_detail(g["group_id"])
    assert detail["group_id"] == g["group_id"]
    assert detail["members"] == ["kh1"]


@pytest.mark.asyncio
async def test_migrate_groupless_keys_to_default(store):
    ks = KeyStore(redis=store.redis)
    await store.redis.set_api_key("deadbeef", {
        "key_id": "key_abc", "user_id": "u1", "status": "active",
        "key_prefix": "gw-deadbee",
    })
    await store.ensure_default_group()
    migrated = await ks.migrate_groups(store)
    assert migrated >= 1
    data = await store.redis.get_api_key("deadbeef")
    assert data["group_id"] == GroupStore.DEFAULT_GROUP_ID
    members = await store._get_members(GroupStore.DEFAULT_GROUP_ID)
    assert "deadbeef" in members


@pytest.mark.asyncio
async def test_migrate_groups_updates_key_and_membership_together(store):
    """A groupless key gets group_id AND membership in one atomic step."""
    ks = KeyStore(redis=store.redis)
    await store.redis.set_api_key("deadbeef", {
        "key_id": "key_abc", "user_id": "u1", "status": "active",
        "key_prefix": "gw-deadbee", "cache_scope": "group",
    })
    await store.ensure_default_group()
    await ks.migrate_groups(store)
    data = await store.redis.get_api_key("deadbeef")
    assert data["group_id"] == GroupStore.DEFAULT_GROUP_ID
    members = await store._get_members(GroupStore.DEFAULT_GROUP_ID)
    assert "deadbeef" in members
