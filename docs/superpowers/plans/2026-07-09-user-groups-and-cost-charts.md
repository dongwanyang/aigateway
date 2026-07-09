# 用户组 + 成本图表修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user groups (group-level + personal quotas, group-shared caching) to the AI Gateway, fix the cache MISS bar color, switch cost distribution to by-group, and replace the fabricated 7-day cost trend with real per-day Prometheus data.

**Architecture:** New `GroupStore` (Redis-backed, mirrors `KeyStore`'s hash-counter quota model) holds group records + members + group-level quota counters. `KeyStore.check_quota`/`increment_usage` gain a group dimension (group checked first, then key; both incremented atomically). Cache scope expands from `shared/private` to `private/group/public` with `group_id` replacing the unused `tenant_id` slot. A `gateway_cost_by_group` Prometheus counter + a generic `/admin/metrics-query` Prom range-query proxy feed the cost charts. Group management UI is a new Tab inside the existing Quotas page.

**Tech Stack:** Python 3 / FastAPI / redis-py (async) / prometheus_client / pytest (backend); React + TypeScript + Vite + recharts (frontend). Run backend tests with `python3 -m pytest`. Run frontend typecheck+build with `npm run build` (`tsc -b && vite build`).

## Global Constraints

- Backend tests use `python3` (no `python` alias). No `conftest.py`/`pytest.ini`. Run: `python3 -m pytest tests/<file> -v`.
- Config writes must be atomic (tempfile + `os.replace`) — see CLAUDE.md gotcha. (Only relevant if a task edits config-writing endpoints; this plan does not.)
- Single-worker deployment; in-process Prometheus counters are fine.
- `group_id` format: `grp-{slug}` where slug = lowercase, non-alphanumeric → `-`, collapse repeats, strip leading/trailing `-`. System default group: `group_id="grp-default"`, `name="default"` (refines the spec's literal `"default"` to match the `grp-` scheme). The default group is immutable (cannot be deleted).
- `cache_scope` values are exactly `private` | `group` | `public`. The legacy `"shared"` is removed; `shared` semantics map to `public` (no user/group segment in the cache key).
- Quota rate-limiting uses hash counters (`rpm_window_count`/`tpm_window_count` stored in the key/group hash), NOT the ZSet/String `aigateway:ratelimit:*` keys (those exist in `redis_client` but are not used by `check_quota`). Group rate-limiting mirrors this: counters live in the group hash. (This refines the spec's §1.2 `aigateway:ratelimit:{group_id}:*` key list — not needed.)
- Group quota counters may diverge slightly from `SUM(members)` on partial write failure; divergence is under-counting (safe). No reconciliation task (YAGNI).
- Conventional commit prefixes: `feat:` / `fix:` / `refactor:` / `docs:` / `test:`. Commit after every task.
- After backend Python/Dockerfile changes: rebuild + verify (`docker compose up -d --build gateway` then `curl -sf localhost:8000/health`). Frontend changes: `npm run build`. Pure docs/tests need no rebuild. (Per CLAUDE.md workflow rule 2.)

---

## File Structure

**New files:**
- `aigateway-core/src/aigateway_core/shared/auth/group_store.py` — `GroupStore`: group CRUD, members, index, pub/sub, `assign_key_to_group` migration.
- `tests/test_group_store.py` — GroupStore unit tests (uses a fake redis).
- `tests/test_group_quota.py` — group+personal quota check/increment tests.

**Modified backend:**
- `aigateway-core/src/aigateway_core/shared/redis_client.py` — add group convenience methods (`set_group`/`get_group`/`delete_group`/lookup/members/index).
- `aigateway-core/src/aigateway_core/shared/auth/key_store.py` — add `group_id`+`cache_scope` to create/seed; `check_quota`/`increment_usage` group dimension (extract pure helpers); `assign_key_to_group` (delegates to GroupStore).
- `aigateway-core/src/aigateway_core/shared/metrics.py` — `gateway_cost_by_group` Counter; `record_cost(group=...)`.
- `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` — `generate_cache_key` three-tier (drop `tenant_id`, add `group_id`).
- `aigateway-core/src/aigateway_core/dispatch/context.py` — add `group_id` field to `PipelineContext`.
- `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` — `_resolve_identity` returns group_id+cache_scope; `_resolve_cache_scope` three-tier; thread group_id into ctx + record_cost/increment callers.
- `aigateway-core/src/aigateway_core/prefix/cache/plugin.py` — pass `group_id` + use ctx scope.
- `aigateway-core/src/aigateway_core/pipelines/generation/token/feature_cache.py` — per-scope owner key.
- `aigateway-core/src/aigateway_core/pipelines/generation/token/prompt_template_manager.py` — per-scope owner key.
- `aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py` — `record_cost(group=...)`.
- `aigateway-api/src/aigateway_api/admin_routes.py` — group CRUD endpoints, `/admin/api-keys/{key_id}/group`, `metrics-query` proxy, `CreateApiKeyRequest` group_id+cache_scope, `_format_quota_item` group fields.
- `aigateway-api/src/aigateway_api/main.py` — construct `GroupStore`, ensure default group + migrate keys at startup, attach to `app.state`.
- `config.yaml` / `config.yaml.template` — seed `group` field now creates a real group; comment update.

**Modified frontend:**
- `control-panel/src/types.ts` — `Group`/`CreateGroupRequest`/`UpdateGroupRequest`; `ApiKeyItem`+`CreateApiKeyRequest` gain `group_id`/`group_name`/`cache_scope`.
- `control-panel/src/api/client.ts` — group CRUD fns + `metricsQuery` + key group/scope fields.
- `control-panel/src/pages/Quotas.tsx` — Tab (API Keys / 用户组) + group management + key forms gain group/scope.
- `control-panel/src/pages/Cache.tsx` — MISS bar red.
- `control-panel/src/pages/Costs.tsx` — pie by group + trend via `metricsQuery`.

**Docs:** `docs/DB_SCHEMA.md`, `CLAUDE.md`.

---

## Task Dependency Order

Tasks 1→2→3 (group store, then key fields). 4→5→6 (quota, needs 3). 7→8 (cache scope, needs 3). 9 (metrics, needs 3). 10 (admin API, needs 1-3). 11 (proxy, independent). 12 (config seed, needs 1-3). 13 (feature/template, needs 7-8). 14→15→16 (frontend, needs 9-11). 17 (docs, last).

---

### Task 1: GroupStore — group CRUD + slug/id helpers

**Files:**
- Create: `aigateway-core/src/aigateway_core/shared/auth/group_store.py`
- Modify: `aigateway-core/src/aigateway_core/shared/redis_client.py` (add `set_group`/`get_group`/`delete_group`/`set_group_lookup`/`get_group_lookup`)
- Test: `tests/test_group_store.py`

**Interfaces:**
- Consumes: `RedisClientManager` (the same `redis` instance `KeyStore` uses).
- Produces: `GroupStore` class with `create_group(name, quotas) -> dict`, `get_group(group_id) -> dict|None`, `list_groups() -> list[dict]`, `update_group(group_id, quotas=None, status=None) -> dict`, `delete_group(group_id) -> bool`; constants `GROUP_NAMESPACE`, `GROUP_LOOKUP_PREFIX`, `GROUP_MEMBERS_SUFFIX`, `GROUPS_INDEX`, `PUBSUB_CHANNEL`, `DEFAULT_GROUP_ID`, `DEFAULT_GROUP_NAME`; module function `slugify(name)->str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_store.py`:

```python
"""GroupStore unit tests - group CRUD, members, migration.

Uses a minimal fake async redis to avoid a live Redis dependency.
"""
import pytest
from aigateway_core.shared.auth.group_store import GroupStore, slugify


class FakeRedis:
    """Fake async redis: acts as both manager (convenience methods) and raw
    client (self.redis = self). Mirrors redis_client convenience method names
    (set_group/get_group/set_api_key/get_api_key/set_quota/get_quota/...)."""
    def __init__(self):
        self.store = {}  # full-key -> dict | set | str
        self.redis = self  # so mgr.redis.sadd AND mgr.set_group both work

    async def hset(self, key, mapping=None, **kw):
        d = self.store.setdefault(key if isinstance(key, str) else key.decode(), {})
        if mapping:
            for k, v in mapping.items():
                d[k if isinstance(k, str) else k.decode()] = v
        return 1

    async def hgetall(self, key):
        k = key if isinstance(key, str) else key.decode()
        d = self.store.get(k)
        if not d or not isinstance(d, dict):
            return {}
        return {kk: (vv if isinstance(vv, str) else str(vv)) for kk, vv in d.items()}

    async def hincrby(self, key, field, amount):
        k = key if isinstance(key, str) else key.decode()
        d = self.store.setdefault(k, {})
        f = field if isinstance(field, str) else field.decode()
        d[f] = str(int(d.get(f, "0")) + amount)
        return int(d[f])

    async def hincrbyfloat(self, key, field, amount):
        k = key if isinstance(key, str) else key.decode()
        d = self.store.setdefault(k, {})
        f = field if isinstance(field, str) else field.decode()
        d[f] = str(float(d.get(f, "0.0")) + amount)
        return float(d[f])

    async def delete(self, *keys):
        n = 0
        for key in keys:
            k = key if isinstance(key, str) else key.decode()
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    async def set(self, key, value, ex=None):
        self.store[key if isinstance(key, str) else key.decode()] = value

    async def get(self, key):
        k = key if isinstance(key, str) else key.decode()
        v = self.store.get(k)
        return v.encode() if isinstance(v, str) else v

    async def sadd(self, key, *members):
        s = self.store.setdefault(key if isinstance(key, str) else key.decode(), set())
        for m in members:
            s.add(m if isinstance(m, str) else m.decode())
        return len(members)

    async def srem(self, key, *members):
        s = self.store.get(key if isinstance(key, str) else key.decode())
        if not s:
            return 0
        n = 0
        for m in members:
            mm = m if isinstance(m, str) else m.decode()
            if mm in s:
                s.discard(mm)
                n += 1
        return n

    async def smembers(self, key):
        s = self.store.get(key if isinstance(key, str) else key.decode())
        return set(s) if s else set()

    async def publish(self, channel, message):
        return 0

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_core.shared.auth.group_store'`.

- [ ] **Step 3: Add group convenience methods to redis_client**

In `aigateway-core/src/aigateway_core/shared/redis_client.py`, append after `get_key_lookup` (after line 229) a new section:

```python
    # ------------------------------------------------------------------
    # 便捷操作 - 用户组存储 (GroupStore)
    # ------------------------------------------------------------------

    async def set_group(self, group_id: str, data: dict) -> None:
        """写入用户组 Hash。Key 格式: aigateway:group:{group_id}"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.hset(f"aigateway:group:{group_id}", mapping=data)

    async def get_group(self, group_id: str) -> dict | None:
        """读取用户组 Hash。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        raw = await self.redis.hgetall(f"aigateway:group:{group_id}")
        if not raw:
            return None
        return {k.decode(): v.decode() for k, v in raw.items()}

    async def delete_group(self, group_id: str) -> bool:
        """删除用户组主记录（不含 members/lookup，由 GroupStore 统一清理）。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        return bool(await self.redis.delete(f"aigateway:group:{group_id}"))

    async def set_group_lookup(self, name: str, group_id: str) -> None:
        """组名 -> group_id 反查。Key: aigateway:group_lookup:{name}"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.set(f"aigateway:group_lookup:{name}", group_id)

    async def get_group_lookup(self, name: str) -> str | None:
        """通过组名反查 group_id。"""
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        val = await self.redis.get(f"aigateway:group_lookup:{name}")
        return val.decode() if val else None

    async def delete_group_lookup(self, name: str) -> None:
        if self.redis is None:
            raise RuntimeError("Redis 尚未连接")
        await self.redis.delete(f"aigateway:group_lookup:{name}")
```

- [ ] **Step 4: Write GroupStore (CRUD part)**

Create `aigateway-core/src/aigateway_core/shared/auth/group_store.py`:

```python
"""User group storage and group-level quota management - GroupStore.

Mirrors KeyStore's hash-counter quota model: each group has a Redis Hash
`aigateway:group:{group_id}` holding limits + used counters (daily_tokens,
monthly_cost, rpm/tpm windows), isomorphic to `aigateway:key:{key_hash}`.

Per the user-groups design (docs/superpowers/specs/2026-07-09-...):
- group quota = shared pool for all member keys
- personal (key) quota = sub-limit within the group
- both checked (group first, then key) and both incremented per request
- group_id replaces the unused tenant_id slot in cache keys
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """Lowercase, non-alphanumeric -> '-', collapse repeats, strip ends.

    CJK characters are alphanumeric under Python str.isalnum() and are kept.
    """
    s = name.strip().lower()
    out: List[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-")


class GroupStore:
    """用户组存储 + 组级配额管理器。

    所有数据存 Redis，经 redis_client.RedisClientManager 访问。
    组 CRUD 事件通过 aigateway:groups:sync Pub/Sub 跨实例同步。
    """

    # Redis key prefixes
    GROUP_NAMESPACE = "aigateway:group:"
    GROUP_LOOKUP_PREFIX = "aigateway:group_lookup:"
    GROUP_MEMBERS_SUFFIX = ":members"
    GROUPS_INDEX = "aigateway:groups:index"
    PUBSUB_CHANNEL = "aigateway:groups:sync"

    # System default group (receives all pre-existing groupless keys on migrate)
    DEFAULT_GROUP_ID = "grp-default"
    DEFAULT_GROUP_NAME = "default"

    # Default group quota limits (mirror KeyStore defaults)
    DEFAULT_DAILY_TOKENS = 1_000_000
    DEFAULT_MONTHLY_COST = 50.0
    DEFAULT_RATE_LIMIT_RPM = 60
    DEFAULT_RATE_LIMIT_TPM = 100_000

    def __init__(self, redis) -> None:
        self.redis = redis

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _now_unix() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _default_group_fields(self, name: str, quotas: Optional[Dict[str, Any]]) -> Dict[str, str]:
        q = quotas or {}
        now_u = self._now_unix()
        return {
            "name": name,
            "status": "active",
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "daily_tokens_limit": str(q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)),
            "daily_tokens_used": "0",
            "monthly_cost_limit": str(q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)),
            "monthly_cost_used": "0.0",
            "rate_limit_rpm": str(q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)),
            "rate_limit_tpm": str(q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)),
            "rpm_window_start": str(now_u),
            "rpm_window_count": "0",
            "tpm_window_start": str(now_u),
            "tpm_window_count": "0",
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_group(self, name: str, quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create a group. Raises ValueError if name already exists."""
        if not name or not name.strip():
            raise ValueError("group name is required")
        name = name.strip()

        existing = await self.redis.get_group_lookup(name)
        if existing:
            raise ValueError(f"group '{name}' already exists")

        # Resolve a non-colliding group_id
        group_id = f"grp-{slugify(name)}" or "grp-group"
        if not group_id.startswith("grp-") or group_id == "grp-":
            group_id = "grp-group"
        suffix = 2
        while await self.redis.get_group(group_id) is not None:
            group_id = f"grp-{slugify(name)}-{suffix}"
            suffix += 1

        fields = self._default_group_fields(name, quotas)
        await self.redis.set_group(group_id, fields)
        await self.redis.set_group_lookup(name, group_id)
        await self._add_to_index(group_id)
        await self._init_group_quota_periods(group_id)

        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_created", "group_id": group_id,
            "name": name, "timestamp": self._now_iso(),
        })
        logger.info("Group 创建: group_id=%s name=%s", group_id, name)
        return {"group_id": group_id, "name": name, **{k: v for k, v in fields.items()}}

    async def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        return await self.redis.get_group(group_id)

    async def list_groups(self) -> List[Dict[str, Any]]:
        ids = await self._all_group_ids()
        out: List[Dict[str, Any]] = []
        for gid in ids:
            g = await self.redis.get_group(gid)
            if g:
                g["group_id"] = gid
                out.append(g)
        return out

    async def update_group(
        self, group_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self.redis.get_group(group_id)
        if not data:
            raise ValueError(f"group {group_id} not found")
        if quotas:
            if "daily_tokens" in quotas:
                data["daily_tokens_limit"] = str(quotas["daily_tokens"])
            if "monthly_cost" in quotas:
                data["monthly_cost_limit"] = str(quotas["monthly_cost"])
            if "rate_limit_rpm" in quotas:
                data["rate_limit_rpm"] = str(quotas["rate_limit_rpm"])
            if "rate_limit_tpm" in quotas:
                data["rate_limit_tpm"] = str(quotas["rate_limit_tpm"])
        if status:
            data["status"] = status
        data["updated_at"] = self._now_iso()
        await self.redis.set_group(group_id, data)
        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_updated", "group_id": group_id,
            "timestamp": self._now_iso(),
        })
        return data

    async def delete_group(self, group_id: str) -> bool:
        if group_id == self.DEFAULT_GROUP_ID:
            raise ValueError("default group cannot be deleted")
        data = await self.redis.get_group(group_id)
        if not data:
            return False
        members = await self._get_members(group_id)
        if members:
            raise ValueError(f"group {group_id} still has {len(members)} members; reassign first")
        name = data.get("name", "")
        await self.redis.delete_group(group_id)
        if name:
            await self.redis.delete_group_lookup(name)
        await self._remove_from_index(group_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_deleted", "group_id": group_id,
            "timestamp": self._now_iso(),
        })
        return True

    # ------------------------------------------------------------------
    # index + quota period init (members added in Task 2)
    # ------------------------------------------------------------------

    async def _add_to_index(self, group_id: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.sadd(self.GROUPS_INDEX, group_id)

    async def _remove_from_index(self, group_id: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.srem(self.GROUPS_INDEX, group_id)

    async def _all_group_ids(self) -> List[str]:
        if self.redis.redis is None:
            return []
        raw = await self.redis.redis.smembers(self.GROUPS_INDEX)
        return sorted(m.decode() if isinstance(m, bytes) else m for m in raw)

    async def _init_group_quota_periods(self, group_id: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        base = {"tokens_in": "0", "tokens_out": "0", "cost_usd": "0.0",
                "request_count": "0", "model_usage": "{}"}
        await self.redis.set_quota(group_id, f"daily:{today}", base)
        await self.redis.set_quota(group_id, f"monthly:{month}", base)


__all__ = ["GroupStore", "slugify"]
```

Note: `redis.get_group_lookup`/`set_group_lookup` were added in Step 3. The members methods (`_get_members`) are stubbed here and implemented in Task 2 — but the test in Step 1 already calls `delete_group` which calls `_get_members`. To keep Task 1 self-contained, add a minimal `_get_members` now (Task 2 expands it):

Add to `GroupStore` in Task 1:

```python
    async def _get_members(self, group_id: str) -> List[str]:
        if self.redis.redis is None:
            return []
        raw = await self.redis.redis.smembers(f"{self.GROUP_NAMESPACE}{group_id}{self.GROUP_MEMBERS_SUFFIX}")
        return sorted(m.decode() if isinstance(m, bytes) else m for m in raw)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_store.py -v`
Expected: PASS (all 6 tests). If `pytest-asyncio` is missing, check `aigateway-api/requirements.txt` — it is a transitive dep; if not installed, `pip install pytest-asyncio` and add `asyncio_mode = auto` is NOT set (no pytest.ini), so the `@pytest.mark.asyncio` decorator requires the plugin. Verify it's present: `python3 -c "import pytest_asyncio"`.

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/group_store.py \
        aigateway-core/src/aigateway_core/shared/redis_client.py \
        tests/test_group_store.py
git commit -m "feat(groups): add GroupStore group CRUD + redis group helpers"
```

---

### Task 2: GroupStore — members SET + member counts + list enrichment

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/group_store.py`
- Modify: `aigateway-core/src/aigateway_core/shared/redis_client.py` (members helpers if needed — use raw `self.redis.redis.sadd/srem/smembers` directly in GroupStore)
- Test: `tests/test_group_store.py` (extend)

**Interfaces:**
- Produces: `GroupStore.add_member(group_id, key_hash)`, `remove_member(group_id, key_hash)`, `get_member_count(group_id) -> int`, `list_groups()` now includes `member_count` and quota `used` fields; `get_group_detail(group_id) -> dict` (group + members list).

- [ ] **Step 1: Write the failing tests (append to test_group_store.py)**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_store.py -v`
Expected: FAIL with `AttributeError: 'GroupStore' object has no attribute 'add_member'`.

- [ ] **Step 3: Implement members + list enrichment + ensure_default_group**

Add these methods to `GroupStore` (replace the Task-1 `_get_members` stub with the fuller set):

```python
    # ------------------------------------------------------------------
    # members
    # ------------------------------------------------------------------

    def _members_key(self, group_id: str) -> str:
        return f"{self.GROUP_NAMESPACE}{group_id}{self.GROUP_MEMBERS_SUFFIX}"

    async def add_member(self, group_id: str, key_hash: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.sadd(self._members_key(group_id), key_hash)

    async def remove_member(self, group_id: str, key_hash: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.srem(self._members_key(group_id), key_hash)

    async def _get_members(self, group_id: str) -> List[str]:
        if self.redis.redis is None:
            return []
        raw = await self.redis.redis.smembers(self._members_key(group_id))
        return sorted(m.decode() if isinstance(m, bytes) else m for m in raw)

    async def get_member_count(self, group_id: str) -> int:
        return len(await self._get_members(group_id))

    async def get_group_detail(self, group_id: str) -> Optional[Dict[str, Any]]:
        data = await self.redis.get_group(group_id)
        if not data:
            return None
        data["group_id"] = group_id
        data["members"] = await self._get_members(group_id)
        data["member_count"] = len(data["members"])
        return data

    async def list_groups(self) -> List[Dict[str, Any]]:
        ids = await self._all_group_ids()
        out: List[Dict[str, Any]] = []
        for gid in ids:
            g = await self.redis.get_group(gid)
            if g:
                g["group_id"] = gid
                g["member_count"] = await self.get_member_count(gid)
                out.append(g)
        return out

    async def ensure_default_group(self) -> str:
        """Create the system default group if absent. Returns its group_id."""
        existing = await self.redis.get_group_lookup(self.DEFAULT_GROUP_NAME)
        if existing:
            return existing
        try:
            g = await self.create_group(self.DEFAULT_GROUP_NAME, {})
        except ValueError:
            return self.DEFAULT_GROUP_ID
        # Force the canonical id regardless of slug (name 'default' -> grp-default)
        return g["group_id"]
```

Note: `create_group("default")` already produces `grp-default` via slugify, so `ensure_default_group` returns `grp-default`. `delete_group` already guards `DEFAULT_GROUP_ID`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_store.py -v`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/group_store.py tests/test_group_store.py
git commit -m "feat(groups): group members SET, member counts, default group guard"
```

---

### Task 3: KeyStore — add group_id + cache_scope to key; default group + migration at startup

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/key_store.py` (`create`, `seed_from_config`)
- Modify: `aigateway-api/src/aigateway_api/main.py` (construct GroupStore, ensure default + migrate keys)
- Test: `tests/test_group_store.py` (extend with migration test)

**Interfaces:**
- Consumes: `GroupStore` (Task 1-2).
- Produces: `KeyStore.create(user_id, quotas, group_id, cache_scope)` writes `group_id`+`cache_scope` fields; `seed_from_config` reads `group` field and assigns; `KeyStore.migrate_groups(group_store)` scans groupless keys → default group.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.asyncio
async def test_migrate_groupless_keys_to_default(store):
    # Simulate a pre-existing key hash with no group_id field.
    from aigateway_core.shared.auth.key_store import KeyStore
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_store.py::test_migrate_groupless_keys_to_default -v`
Expected: FAIL with `AttributeError: 'KeyStore' object has no attribute 'migrate_groups'`.

- [ ] **Step 3: Add group_id+cache_scope to KeyStore.create**

In `key_store.py`, change the `create` signature and `key_data` dict (around lines 197-250):

```python
    async def create(
        self,
        user_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        group_id: str = "",
        cache_scope: str = "group",
    ) -> Dict[str, Any]:
        """Create a new API Key and store in Redis.

        Args:
            user_id: associated user ID.
            quotas: quota config {daily_tokens, monthly_cost, rate_limit_rpm, rate_limit_tpm}.
            group_id: required group id (empty -> assigned to default group by caller/migrate).
            cache_scope: default cache scope for this key (private/group/public).
        """
        if not user_id:
            raise ValueError("user_id is required")

        _ALPHABET = string.ascii_letters + string.digits
        raw_key = f"gw-{''.join(secrets.choice(_ALPHABET) for _ in range(32))}"

        key_hash = self._hash_key(raw_key)
        key_prefix = self._prefix_key(raw_key)
        now_iso = self._now_iso()

        q = quotas or {}
        daily_tokens = q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)
        monthly_cost = q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)
        rate_rpm = q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)
        rate_tpm = q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)

        key_id = f"key_{uuid.uuid4().hex[:8]}"
        await self._check_duplicate_user_key(user_id)

        key_data: Dict[str, str] = {
            "key_id": key_id,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "status": "active",
            "created_at": now_iso,
            "last_used_at": "",
            "group_id": group_id or "",
            "cache_scope": cache_scope or "group",
            "daily_tokens_limit": str(daily_tokens),
            "daily_tokens_used": "0",
            "monthly_cost_limit": str(monthly_cost),
            "monthly_cost_used": "0.0",
            "rate_limit_rpm": str(rate_rpm),
            "rate_limit_tpm": str(rate_tpm),
            "rpm_window_start": str(self._now_unix()),
            "rpm_window_count": "0",
            "tpm_window_start": str(self._now_unix()),
            "tpm_window_count": "0",
        }
        await self.redis.set_api_key(key_hash, key_data)
        await self.redis.set_key_lookup(key_prefix, key_hash)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        quota_base = {"tokens_in": "0", "tokens_out": "0", "cost_usd": "0.0",
                      "request_count": "0", "model_usage": "{}"}
        await self.redis.set_quota(key_hash, f"daily:{today}", quota_base)
        await self.redis.set_quota(key_hash, f"monthly:{month}", quota_base)

        pub_msg = self._build_pubsub_message("key_created", key_id, user_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        logger.info("API Key 创建成功: user_id=%s, key_id=%s, group_id=%s", user_id, key_id, group_id or "(default)")

        return {
            "id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "group_id": group_id or "",
            "cache_scope": cache_scope or "group",
            "created_at": now_iso,
            "status": "active",
            "quotas": {
                "daily_tokens": daily_tokens,
                "monthly_cost": monthly_cost,
                "rate_limit_rpm": rate_rpm,
                "rate_limit_tpm": rate_tpm,
            },
        }
```

- [ ] **Step 4: Add group_id+cache_scope to seed_from_config**

In `seed_from_config`, the existing-key branch (around line 340-352) and new-key branch (around 356-374) must set `group_id`+`cache_scope`. Replace the existing-key update block's structural-field section with:

```python
            if existing:
                existing["user_id"] = user_id
                existing["status"] = "active"
                existing["is_admin"] = str(is_admin)
                # group: config 'group' field -> group_id (group auto-created by caller)
                cfg_group = cfg.get("group") or ""
                if cfg_group:
                    existing["group_id"] = cfg_group  # GroupStore resolves name->id at startup
                if "group_id" not in existing:
                    existing["group_id"] = ""
                if "cache_scope" not in existing:
                    existing["cache_scope"] = "group"
                # (existing quota-limit-only-if-missing logic unchanged)
                if "daily_tokens_limit" not in existing:
                    existing["daily_tokens_limit"] = str(quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS))
                if "monthly_cost_limit" not in existing:
                    existing["monthly_cost_limit"] = str(quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST))
                if "rate_limit_rpm" not in existing:
                    existing["rate_limit_rpm"] = str(quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM))
                if "rate_limit_tpm" not in existing:
                    existing["rate_limit_tpm"] = str(quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM))
                await self.redis.set_api_key(key_hash, existing)
                logger.info("API Key 已更新: user_id=%s, key_hash=%s", user_id, key_hash)
```

And in the new-key `key_data` dict (around line 356-374), add two fields:

```python
                key_data: Dict[str, str] = {
                    "key_id": key_id,
                    "key_prefix": key_prefix,
                    "user_id": user_id,
                    "status": "active",
                    "created_at": now_iso,
                    "last_used_at": "",
                    "group_id": cfg.get("group") or "",
                    "cache_scope": "group",
                    "daily_tokens_limit": str(quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)),
                    "daily_tokens_used": "0",
                    "monthly_cost_limit": str(quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST)),
                    "monthly_cost_used": "0.0",
                    "rate_limit_rpm": str(quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)),
                    "rate_limit_tpm": str(quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)),
                    "rpm_window_start": str(self._now_unix()),
                    "rpm_window_count": "0",
                    "tpm_window_start": str(self._now_unix()),
                    "tpm_window_count": "0",
                    "is_admin": str(is_admin),
                }
```

(Add `cfg_group = cfg.get("group") or ""` once near the top of the loop body, around line 333, and reference it in both branches.)

- [ ] **Step 5: Add migrate_groups + assign_key_to_group stub to KeyStore**

Add to `KeyStore`:

```python
    async def migrate_groups(self, group_store) -> int:
        """Assign groupless keys to the default group. Call once at startup.

        Returns the number of keys migrated.
        """
        if self.redis is None or self.redis.redis is None:
            return 0
        default_id = await group_store.ensure_default_group()
        migrated = 0
        cursor = 0
        while True:
            cursor, keys = await self.redis.redis.scan(cursor, match="aigateway:key:*", count=100)
            for raw_key in keys:
                kh = raw_key.decode().split(":")[-1] if isinstance(raw_key, bytes) else raw_key.split(":")[-1]
                data = await self.redis.get_api_key(kh)
                if not data:
                    continue
                gid = data.get("group_id") or ""
                if not gid:
                    data["group_id"] = default_id
                    if "cache_scope" not in data:
                        data["cache_scope"] = "group"
                    await self.redis.set_api_key(kh, {"group_id": default_id,
                                                      "cache_scope": data.get("cache_scope", "group")})
                    await group_store.add_member(default_id, kh)
                    migrated += 1
                else:
                    # ensure membership tracked even for already-grouped keys
                    await group_store.add_member(gid, kh)
            if cursor == 0:
                break
        if migrated:
            logger.info("迁移 %d 个无组 Key 到默认组 %s", migrated, default_id)
        return migrated
```

- [ ] **Step 6: Wire GroupStore into main.py lifespan**

In `aigateway-api/src/aigateway_api/main.py`, after the KeyStore block (around lines 304-314), add:

```python
    # 初始化 GroupStore + 默认组 + 迁移无组 Key
    group_store: Optional["GroupStore"] = None  # type: ignore[name-defined]
    try:
        from aigateway_core.shared.auth.group_store import GroupStore
        group_store = GroupStore(redis=redis_mgr)
        await group_store.ensure_default_group()
        # 为 config 中声明的 group 名自动建组
        if config_manager:
            for cfg_key in (config_manager.get("auth", {}) or {}).get("api_keys", []) or []:
                gname = (cfg_key or {}).get("group") or ""
                if gname:
                    try:
                        await group_store.create_group(gname, {})
                    except ValueError:
                        pass  # already exists
        await key_store.migrate_groups(group_store)
        logger.info("GroupStore 初始化完成")
    except Exception as exc:
        logger.warning("GroupStore 初始化失败: %s", exc)
```

And in the `app.state` assignments (around line 504), add:

```python
    app.state.group_store = group_store
```

Also add the import near line 45 (`from aigateway_core.shared.auth.key_store import KeyStore`):

```python
from aigateway_core.shared.auth.group_store import GroupStore
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_store.py -v`
Expected: PASS (all tests including migration).

- [ ] **Step 8: Run full existing test suite to check no regressions**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS (no new failures; `test_cache_key_v2.py` still passes — it does not touch key fields).

- [ ] **Step 9: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/key_store.py \
        aigateway-api/src/aigateway_api/main.py tests/test_group_store.py
git commit -m "feat(groups): persist group_id+cache_scope on keys; default-group migration at startup"
```

---

### Task 4: KeyStore.check_quota — group-level check (extract _check_dims helper)

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/key_store.py`
- Create: `tests/test_group_quota.py`

**Interfaces:**
- Consumes: `GroupStore` constants (`GROUP_NAMESPACE`) — import `from aigateway_core.shared.auth.group_store import GroupStore`.
- Produces: `KeyStore._check_dims(data, tokens, cost, now_unix) -> (passed, reason, retry_after, resets)` pure helper; `check_quota` checks group first (fail_msg prefixed `"Group "`), then key.

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_quota.py`:

```python
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


@pytest.fixture
def ks_and_gs():
    mgr = FakeRedis()
    return KeyStore(redis=mgr), GroupStore(redis=mgr)


@pytest.mark.asyncio
async def test_check_dims_group_first_then_key(ks_and_gs):
    ks, gs = ks_and_gs
    # group with monthly_cost limit 5000, used 4999
    await ks.redis.set_group("grp-g", {"name": "G", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "4999.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # key with personal monthly_cost limit 200, used 0, belongs to grp-g
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-g", "cache_scope": "group",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # cost 10 -> group 4999+10=5009 > 5000 -> group rejects (group checked first)
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=10.0)
    assert ok is False
    assert reason.startswith("Group ")
    assert "Monthly" in reason or "monthly" in reason


@pytest.mark.asyncio
async def test_personal_limit_rejects_when_group_ok(ks_and_gs):
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
    # tokens 10 -> key daily 95+10=105 > 100 -> personal rejects
    ok, reason, retry = await ks.check_quota("kh1", tokens=10, cost=0.0)
    assert ok is False
    assert not reason.startswith("Group ")
    assert "Daily" in reason or "daily" in reason


@pytest.mark.asyncio
async def test_both_pass_when_under_limits(ks_and_gs):
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_quota.py -v`
Expected: FAIL — current `check_quota` has no group logic; `test_check_dims_group_first_then_key` will pass (no group check) instead of failing, so the group-overflow case returns True wrongly. The test asserts `ok is False` -> FAIL.

- [ ] **Step 3: Extract _check_dims + add group check**

Add the import at top of `key_store.py`:

```python
from aigateway_core.shared.auth.group_store import GroupStore
```

Add the pure helper method to `KeyStore` (before `check_quota`):

```python
    @staticmethod
    def _check_dims(
        data: Dict[str, Any], tokens: int, cost: float, now_unix: int,
        default_rpm: int = DEFAULT_RATE_LIMIT_RPM,
        default_tpm: int = DEFAULT_RATE_LIMIT_TPM,
        default_daily: int = DEFAULT_DAILY_TOKENS,
        default_monthly: float = DEFAULT_MONTHLY_COST,
    ) -> Tuple[bool, Optional[str], int, Dict[str, str]]:
        """Check RPM/TPM/daily/monthly against a data dict.

        Returns (passed, reason, retry_after, resets) where `resets` holds
        window fields to write back when a window expired (caller persists).
        """
        resets: Dict[str, str] = {}

        rpm_limit = int(data.get("rate_limit_rpm", default_rpm))
        rpm_window_start = int(data.get("rpm_window_start", "0"))
        rpm_window_count = int(data.get("rpm_window_count", "0"))
        if now_unix - rpm_window_start >= 60:
            resets["rpm_window_start"] = str(now_unix)
            resets["rpm_window_count"] = "0"
            rpm_window_count = 0
            rpm_window_start = now_unix
        elif rpm_window_count >= rpm_limit:
            return False, f"RPM limit exceeded: {rpm_window_count}/{rpm_limit}", rpm_window_start + 60 - now_unix, resets

        tpm_limit = int(data.get("rate_limit_tpm", default_tpm))
        tpm_window_start = int(data.get("tpm_window_start", "0"))
        tpm_window_count = int(data.get("tpm_window_count", "0"))
        if now_unix - tpm_window_start >= 60:
            resets["tpm_window_start"] = str(now_unix)
            resets["tpm_window_count"] = "0"
            tpm_window_count = 0
            tpm_window_start = now_unix
        elif tpm_window_count + tokens > tpm_limit:
            return False, f"TPM limit exceeded: {tpm_window_count + tokens}/{tpm_limit}", tpm_window_start + 60 - now_unix, resets

        daily_limit = int(data.get("daily_tokens_limit", default_daily))
        daily_used = int(data.get("daily_tokens_used", "0"))
        if daily_used + tokens > daily_limit:
            return False, f"Daily token limit exceeded: {daily_used}/{daily_limit}", 0, resets

        monthly_limit = float(data.get("monthly_cost_limit", default_monthly))
        monthly_used = float(data.get("monthly_cost_used", "0.0"))
        if monthly_used + cost > monthly_limit:
            return False, f"Monthly cost limit exceeded: ${monthly_used:.2f}/${monthly_limit:.2f}", 0, resets

        return True, None, 0, resets
```

Replace the body of `check_quota` (lines 512-564) with:

```python
    async def check_quota(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
    ) -> Tuple[bool, Optional[str], int]:
        """Check group-level (if group_id set) then key-level quotas."""
        data = await self.redis.get_api_key(key_hash)
        if not data:
            return False, "API Key does not exist", 0

        now_unix = self._now_unix()
        group_id = data.get("group_id") or ""

        # ---- Group-level check (first) ----
        if group_id:
            gdata = await self.redis.get_group(group_id)
            if gdata:
                gok, greason, gretry, gresets = self._check_dims(gdata, tokens, cost, now_unix)
                if gresets:
                    await self.redis.set_group(group_id, gresets)
                if not gok:
                    return False, f"Group {greason}", gretry

        # ---- Key-level check ----
        ok, reason, retry, resets = self._check_dims(data, tokens, cost, now_unix)
        if resets:
            await self.redis.set_api_key(key_hash, resets)
        if not ok:
            return False, reason, retry

        return True, None, 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_quota.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Dispatcher error-code dispatch for group failures**

`check_quota` now returns `"Group {reason}"` for group-level failures. The dispatcher's keyword dispatch (in both `_dispatch_understanding` ~line 373-380 and `_dispatch_generation` ~line 525-533) checks `"RPM"/"TPM"/"Daily"/"Monthly" in fail_msg`, which would match a group message too (since it contains e.g. "Monthly"). Add a `"Group "` prefix check FIRST so group failures get group-specific codes.

In `dispatcher.py`, in both quota-failure blocks, insert before the existing `if "RPM" in fail_msg:` chain:

```python
                code = "quota_exceeded"
                is_group = fail_msg.startswith("Group ")
                if "RPM" in fail_msg:
                    code = "rate_limit_group_rpm" if is_group else "rate_limit_rpm"
                elif "TPM" in fail_msg:
                    code = "rate_limit_group_tpm" if is_group else "rate_limit_tpm"
                elif "Daily" in fail_msg:
                    code = "quota_exceeded_group_daily_tokens" if is_group else "quota_exceeded_daily_tokens"
                elif "Monthly" in fail_msg:
                    code = "quota_exceeded_group_monthly_cost" if is_group else "quota_exceeded_monthly_cost"
```

(Replace the existing 4-branch `if/elif` chain that assigns `code` in both methods with the above.)

- [ ] **Step 6: Run full suite for regressions**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/key_store.py \
        aigateway-core/src/aigateway_core/dispatch/dispatcher.py tests/test_group_quota.py
git commit -m "feat(groups): check_quota group-first + dispatcher group error codes"
```

---

### Task 5: KeyStore.increment_usage — group-level sync (extract helpers)

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/key_store.py`
- Test: `tests/test_group_quota.py` (extend)

**Interfaces:**
- Produces: pure helpers `_compute_usage_updates(data, tokens, cost, now_unix) -> dict`, `_accumulate_quota_record(quota, tokens, cost, model, tokens_in, tokens_out) -> dict`; `increment_usage` updates key + group (group wrapped in try/except, non-blocking).

- [ ] **Step 1: Write the failing test (append to test_group_quota.py)**

```python
@pytest.mark.asyncio
async def test_increment_syncs_group_and_key(ks_and_gs):
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
    ks, gs = ks_and_gs
    # key has group_id pointing to a nonexistent group -> group write path no-ops/caught
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-missing", "cache_scope": "group",
        "daily_tokens_limit": "200", "daily_tokens_used": "0",
        "monthly_cost_limit": "200", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # must not raise
    await ks.increment_usage("kh1", tokens=5, cost=0.1, model="gpt-4o", tokens_in=4, tokens_out=1)
    kdata = await ks.redis.get_api_key("kh1")
    assert kdata["daily_tokens_used"] == "5"  # key still incremented
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_quota.py -v`
Expected: FAIL — `test_increment_syncs_group_and_key` asserts group `daily_tokens_used == "50"` but group is not incremented by current code.

- [ ] **Step 3: Extract helpers + add group sync**

Add two pure helpers to `KeyStore` (before `increment_usage`):

```python
    @staticmethod
    def _compute_usage_updates(data: Dict[str, Any], tokens: int, cost: float, now_unix: int) -> Dict[str, str]:
        """Pure: compute RPM/TPM/daily/monthly counter updates from a data dict."""
        updates: Dict[str, str] = {}
        rpm_window_start = int(data.get("rpm_window_start", "0"))
        rpm_window_count = int(data.get("rpm_window_count", "0")) + 1
        if now_unix - rpm_window_start >= 60:
            rpm_window_start = now_unix
            rpm_window_count = 1
        updates["rpm_window_count"] = str(rpm_window_count)
        updates["rpm_window_start"] = str(rpm_window_start)

        tpm_window_start = int(data.get("tpm_window_start", "0"))
        tpm_window_count = int(data.get("tpm_window_count", "0")) + tokens
        if now_unix - tpm_window_start >= 60:
            tpm_window_start = now_unix
            tpm_window_count = tokens
        updates["tpm_window_count"] = str(tpm_window_count)
        updates["tpm_window_start"] = str(tpm_window_start)

        updates["daily_tokens_used"] = str(int(data.get("daily_tokens_used", "0")) + tokens)
        updates["monthly_cost_used"] = str(float(data.get("monthly_cost_used", "0.0")) + cost)
        return updates

    @staticmethod
    def _accumulate_quota_record(quota: Dict[str, Any], tokens: int, cost: float,
                                 model: str, tokens_in: int, tokens_out: int) -> Dict[str, Any]:
        """Pure: accumulate one request into a quota record dict (DB_SCHEMA §2)."""
        if not quota:
            quota = {"tokens_in": "0", "tokens_out": "0", "cost_usd": "0.0",
                     "request_count": "0", "model_usage": "{}"}
        quota["tokens_in"] = str(int(quota.get("tokens_in", "0")) + tokens_in)
        quota["tokens_out"] = str(int(quota.get("tokens_out", "0")) + tokens_out)
        quota["cost_usd"] = str(float(quota.get("cost_usd", "0.0")) + cost)
        quota["request_count"] = str(int(quota.get("request_count", "0")) + 1)
        try:
            model_usage = json.loads(quota.get("model_usage", "{}")) if isinstance(quota.get("model_usage"), str) else quota.get("model_usage", {})
        except (json.JSONDecodeError, TypeError):
            model_usage = {}
        entry = model_usage.get(model, {"in": 0, "out": 0})
        if isinstance(entry, dict):
            entry["in"] = entry.get("in", 0) + tokens_in
            entry["out"] = entry.get("out", 0) + tokens_out
        else:
            entry = {"in": tokens_in, "out": tokens_out}
        model_usage[model] = entry
        quota["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
        return quota
```

Replace the body of `increment_usage` (lines 587-665) with:

```python
    async def increment_usage(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Accumulate usage into the key AND its group (sync)."""
        data = await self.redis.get_api_key(key_hash)
        if not data:
            logger.warning("increment_usage: key_hash=%s 不存在", key_hash)
            return

        now_unix = self._now_unix()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        # ---- Key-level ----
        updates = self._compute_usage_updates(data, tokens, cost, now_unix)
        await self.redis.set_api_key(key_hash, updates)

        daily_quota = await self.redis.get_quota(key_hash, f"daily:{today}")
        await self.redis.set_quota(key_hash, f"daily:{today}",
                                   self._accumulate_quota_record(daily_quota, tokens, cost, model, tokens_in, tokens_out))
        monthly_quota = await self.redis.get_quota(key_hash, f"monthly:{month}")
        await self.redis.set_quota(key_hash, f"monthly:{month}",
                                   self._accumulate_quota_record(monthly_quota, tokens, cost, model, tokens_in, tokens_out))

        # ---- Group-level (sync, non-blocking on failure) ----
        group_id = data.get("group_id") or ""
        if group_id:
            try:
                gdata = await self.redis.get_group(group_id)
                if gdata:
                    gupdates = self._compute_usage_updates(gdata, tokens, cost, now_unix)
                    await self.redis.set_group(group_id, gupdates)
                    gdaily = await self.redis.get_quota(group_id, f"daily:{today}")
                    await self.redis.set_quota(group_id, f"daily:{today}",
                                               self._accumulate_quota_record(gdaily, tokens, cost, model, tokens_in, tokens_out))
                    gmonthly = await self.redis.get_quota(group_id, f"monthly:{month}")
                    await self.redis.set_quota(group_id, f"monthly:{month}",
                                               self._accumulate_quota_record(gmonthly, tokens, cost, model, tokens_in, tokens_out))
            except Exception as exc:
                logger.warning("组级 increment_usage 失败 group=%s: %s", group_id, exc)

        logger.debug("Usage incremented: key_hash=%s tokens=%d cost=$%.4f group=%s",
                     key_hash, tokens, cost, group_id or "-")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_quota.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/key_store.py tests/test_group_quota.py
git commit -m "feat(groups): increment_usage syncs group counters (non-blocking)"
```

---

### Task 6: GroupStore.assign_key_to_group — usage migration on group change

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/group_store.py`
- Test: `tests/test_group_quota.py` (extend)

**Interfaces:**
- Produces: `GroupStore.assign_key_to_group(key_hash, new_group_id) -> dict` — moves the key's current-period daily/monthly used from old group to new group, updates key's `group_id`, updates both groups' members SETs and live counters.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.asyncio
async def test_assign_key_migrates_usage(ks_and_gs):
    ks, gs = ks_and_gs
    # two groups
    await ks.redis.set_group("grp-a", {"name": "A", "status": "active",
        "daily_tokens_limit": "5000", "daily_tokens_used": "50",
        "monthly_cost_limit": "5000", "monthly_cost_used": "2.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    await ks.redis.set_group("grp-b", {"name": "B", "status": "active",
        "daily_tokens_limit": "5000", "daily_tokens_used": "0",
        "monthly_cost_limit": "5000", "monthly_cost_used": "0.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    # key in grp-a with 50 daily / 2.0 monthly used
    await ks.redis.set_api_key("kh1", {"key_id": "k1", "user_id": "u1", "status": "active",
        "group_id": "grp-a", "cache_scope": "group",
        "daily_tokens_limit": "200", "daily_tokens_used": "50",
        "monthly_cost_limit": "200", "monthly_cost_used": "2.0",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
        "rpm_window_start": "0", "rpm_window_count": "0",
        "tpm_window_start": "0", "tpm_window_count": "0"})
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    month = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m")
    await ks.redis.set_quota("kh1", f"daily:{today}", {"tokens_in": "40", "tokens_out": "10",
        "cost_usd": "2.0", "request_count": "1", "model_usage": "{}"})

    await gs.assign_key_to_group("kh1", "grp-b")

    kdata = await ks.redis.get_api_key("kh1")
    assert kdata["group_id"] == "grp-b"
    ga = await ks.redis.get_group("grp-a")
    gb = await ks.redis.get_group("grp-b")
    assert int(ga["daily_tokens_used"]) == 0      # 50 - 50
    assert int(gb["daily_tokens_used"]) == 50     # 0 + 50
    assert float(ga["monthly_cost_used"]) == 0.0
    assert float(gb["monthly_cost_used"]) == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_group_quota.py::test_assign_key_migrates_usage -v`
Expected: FAIL with `AttributeError: 'GroupStore' object has no attribute 'assign_key_to_group'`.

- [ ] **Step 3: Implement assign_key_to_group**

Add to `GroupStore`:

```python
    async def assign_key_to_group(self, key_hash: str, new_group_id: str) -> Dict[str, Any]:
        """Move a key from its current group to new_group_id, migrating the
        key's current daily/monthly used from old group -> new group.

        RPM/TPM windows are NOT migrated (short-lived; natural expiry).
        """
        if self.redis is None or self.redis.redis is None:
            raise RuntimeError("Redis not connected")
        data = await self.redis.get_api_key(key_hash)
        if not data:
            raise ValueError(f"key_hash {key_hash} not found")
        new_group = await self.redis.get_group(new_group_id)
        if not new_group:
            raise ValueError(f"group {new_group_id} not found")

        old_group_id = data.get("group_id") or ""

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        # Migrate live counters + quota records only if changing groups
        if old_group_id and old_group_id != new_group_id:
            k_daily_used = int(data.get("daily_tokens_used", "0"))
            k_monthly_used = float(data.get("monthly_cost_used", "0.0"))
            redis = self.redis.redis
            # live counters: subtract from old, add to new
            if k_daily_used:
                await redis.hincrby(f"aigateway:group:{old_group_id}", "daily_tokens_used", -k_daily_used)
                await redis.hincrby(f"aigateway:group:{new_group_id}", "daily_tokens_used", k_daily_used)
            if k_monthly_used:
                await redis.hincrbyfloat(f"aigateway:group:{old_group_id}", "monthly_cost_used", -k_monthly_used)
                await redis.hincrbyfloat(f"aigateway:group:{new_group_id}", "monthly_cost_used", k_monthly_used)
            # quota records: move daily + monthly
            for period in (f"daily:{today}", f"monthly:{month}"):
                kq = await self.redis.get_quota(key_hash, period)
                if kq:
                    await self._move_quota_record(old_group_id, new_group_id, period, kq, redis)

        # update key group_id + membership
        await self.redis.set_api_key(key_hash, {"group_id": new_group_id})
        if old_group_id and old_group_id != new_group_id:
            await self.remove_member(old_group_id, key_hash)
        await self.add_member(new_group_id, key_hash)

        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "key_reassigned", "key_hash": key_hash,
            "old_group_id": old_group_id, "new_group_id": new_group_id,
            "timestamp": self._now_iso(),
        })
        return {"key_hash": key_hash, "old_group_id": old_group_id, "new_group_id": new_group_id}

    @staticmethod
    @staticmethod
    async def _move_quota_record(old_group_id: str, new_group_id: str, period: str,
                                  kq: Dict[str, Any], redis) -> None:
        """Subtract kq fields from old group's quota record, add to new (HINCRBY)."""
        old_key = f"aigateway:quota:{old_group_id}:{period}"
        new_key = f"aigateway:quota:{new_group_id}:{period}"
        ti = int(kq.get("tokens_in", "0"))
        to = int(kq.get("tokens_out", "0"))
        cost = float(kq.get("cost_usd", "0.0"))
        rc = int(kq.get("request_count", "0"))
        if ti:
            await redis.hincrby(old_key, "tokens_in", -ti)
            await redis.hincrby(new_key, "tokens_in", ti)
        if to:
            await redis.hincrby(old_key, "tokens_out", -to)
            await redis.hincrby(new_key, "tokens_out", to)
        if cost:
            await redis.hincrbyfloat(old_key, "cost_usd", -cost)
            await redis.hincrbyfloat(new_key, "cost_usd", cost)
        if rc:
            await redis.hincrby(old_key, "request_count", -rc)
            await redis.hincrby(new_key, "request_count", rc)
```

The call site in `assign_key_to_group` (the `for period in ...` loop) must `await` it:

```python
            for period in (f"daily:{today}", f"monthly:{month}"):
                kq = await self.redis.get_quota(key_hash, period)
                if kq:
                    await self._move_quota_record(old_group_id, new_group_id, period, kq, redis)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_group_quota.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/group_store.py tests/test_group_quota.py
git commit -m "feat(groups): assign_key_to_group migrates used counters + quota records"
```

---

### Task 7: cache_manager.generate_cache_key — three-tier scope (drop tenant_id, add group_id)

**Files:**
- Modify: `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` (`generate_cache_key`, lines 476-555)
- Modify: `tests/test_cache_key_v2.py`

**Interfaces:**
- Produces: `generate_cache_key(normalized_prompt, model, pipeline_kind="understanding", cache_scope="group", user_id="", group_id="", **params) -> str`. `tenant_id` removed. Scope segments: `public`→none, `group`→`g={group_id}`, `private`→`u={user_id}`.

- [ ] **Step 1: Update tests first (TDD)**

In `tests/test_cache_key_v2.py`:
- Replace `test_scope_shared_ignores_user_id` with a public-scope test:

```python
    def test_scope_public_ignores_user_and_group(self):
        """public: same prompt different user/group share key."""
        k_alice = self._key(cache_scope="public", user_id="alice", group_id="grp-a")
        k_bob = self._key(cache_scope="public", user_id="bob", group_id="grp-b")
        k_none = self._key(cache_scope="public", user_id="", group_id="")
        assert k_alice == k_bob == k_none
```

- Replace `test_scope_shared_vs_private_different` with:

```python
    def test_scope_group_isolates_by_group(self):
        """group: different group_id -> different key; same group -> same key."""
        k_a1 = self._key(cache_scope="group", user_id="alice", group_id="grp-a")
        k_a2 = self._key(cache_scope="group", user_id="bob", group_id="grp-a")
        k_b = self._key(cache_scope="group", user_id="alice", group_id="grp-b")
        assert k_a1 == k_a2          # same group, different user -> shared
        assert k_a1 != k_b           # different group -> isolated
```

- Replace `test_tenant_isolation` with a group-isolation test (already covered above) — remove the tenant_id test:

```python
    def test_scope_private_isolates_by_user(self):
        """private: different user_id -> different key."""
        k_alice = self._key(cache_scope="private", user_id="alice", group_id="grp-a")
        k_bob = self._key(cache_scope="private", user_id="bob", group_id="grp-a")
        assert k_alice != k_bob

    def test_scopes_mutually_isolated(self):
        """public/group/private for same user+group all differ."""
        k_pub = self._key(cache_scope="public", user_id="alice", group_id="grp-a")
        k_grp = self._key(cache_scope="group", user_id="alice", group_id="grp-a")
        k_priv = self._key(cache_scope="private", user_id="alice", group_id="grp-a")
        assert len({k_pub, k_grp, k_priv}) == 3
```

- Update the file's docstring bullet 8 from "tenant_id 隔离" to "cache_scope 三档隔离 (public/group/private)" and bullet 5 from "shared/private" to "public/group/private".

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_cache_key_v2.py -v`
Expected: FAIL — `generate_cache_key` still uses `tenant_id`/`shared`, `group_id` kwarg rejected, `cache_scope="public"` treated as shared (no user_id) so group-isolation test fails.

- [ ] **Step 3: Rewrite generate_cache_key**

Replace the signature + parts assembly (lines 476-555) with:

```python
    def generate_cache_key(
        normalized_prompt: str,
        model: str,
        pipeline_kind: str = "understanding",
        cache_scope: str = "group",
        user_id: str = "",
        group_id: str = "",
        **params: Any,
    ) -> str:
        """Generate cache key v2 (SHA-256).

        Scope tiers (replaces the old shared/private + tenant_id design):
        - public:  no identity segment — shared across the whole deployment.
        - group:   `g={group_id}` — shared within a group, isolated between groups.
        - private: `u={user_id}`  — strict per-user isolation (e.g. PII requests).

        Args:
            normalized_prompt: pre-normalized prompt (system + tail N turns).
            model: model name; internally converted to family.
            pipeline_kind: "understanding" | "generation", default understanding.
            cache_scope: "public" | "group" | "private", default group.
            user_id: included only when scope=private.
            group_id: included only when scope=group.
            **params: temperature / max_tokens / top_p (top_p ignored).

        Returns:
            64-hex-char SHA-256 hash. Prefix `aigateway:cache:v2:` prepended by l2_set/l2_get.
        """
        temperature = params.pop("temperature", None)
        max_tokens = params.pop("max_tokens", None)
        params.pop("top_p", None)

        temp_bucket = _bucket_temperature(temperature)
        mt_bucket = _bucket_max_tokens(max_tokens)
        family = "auto" if model == "auto" else _model_family(model)

        parts: List[str] = [
            "v2",
            pipeline_kind or "understanding",
            family,
            temp_bucket,
            mt_bucket,
        ]
        scope = (cache_scope or "group").lower()
        if scope == "private" and user_id:
            parts.append(f"u={user_id}")
        elif scope == "group" and group_id:
            parts.append(f"g={group_id}")
        # public: no identity segment
        for k in sorted(params.keys()):
            v = params[k]
            if v is not None:
                parts.append(f"{k}={v}")
        parts.append(normalized_prompt or "")

        key_string = "|".join(parts)
        return hashlib.sha256(key_string.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cache_key_v2.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py tests/test_cache_key_v2.py
git commit -m "feat(cache): three-tier scope (public/group/private) replacing tenant_id"
```

---

### Task 8: dispatcher — three-tier scope resolution + thread group_id into context

**Files:**
- Modify: `aigateway-core/src/aigateway_core/dispatch/context.py` (add `group_id` field)
- Modify: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` (`_resolve_identity`, `_resolve_cache_scope`, ctx construction, `generate_cache_key` call site, `record_cost`/`increment_usage` group threading)
- Modify: `aigateway-core/src/aigateway_core/prefix/cache/plugin.py` (pass `group_id`, use ctx scope)

**Interfaces:**
- Consumes: `request.state.api_key_data` (now carries `group_id` + `cache_scope` from Task 3).
- Produces: `PipelineContext.group_id`; `_resolve_cache_scope(request, pii_meta, key_data)` returns `public|group|private`; cache key + record_cost receive group_id.

- [ ] **Step 1: Add group_id to PipelineContext**

In `context.py`, add a field to the dataclass (after `user_id`, line 60):

```python
    user_id: Optional[str] = None
    group_id: Optional[str] = None
```

- [ ] **Step 2: Extend _resolve_identity to return group_id + cache_scope**

In `dispatcher.py`, replace `_resolve_identity` (lines 256-269) with:

```python
    @staticmethod
    def _resolve_identity(request: Request) -> tuple:
        user_id: Optional[str] = None
        key_hash: Optional[str] = None
        group_id: Optional[str] = None
        cache_scope: str = "group"
        if hasattr(request.state, "api_key_data"):
            key_data = request.state.api_key_data
            if key_data:
                user_id = key_data.get("user_id") or None
                group_id = key_data.get("group_id") or None
                cache_scope = key_data.get("cache_scope") or "group"
                raw_key = getattr(request.state, "api_key_value", "")
                if raw_key:
                    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        if not user_id and hasattr(request.state, "user_id"):
            user_id = request.state.user_id
        return user_id, key_hash, group_id, cache_scope
```

- [ ] **Step 3: Update the dispatch() caller + both _dispatch_* signatures**

In `dispatch()` (lines 188-250), update the identity unpacking and the two dispatch calls:

```python
        user_id, key_hash, group_id, cache_scope = self._resolve_identity(request)
```

(pass `group_id` and `cache_scope` into `_dispatch_understanding` / `_dispatch_generation` — add them as params to those methods and forward to the cache-key + ctx sites.)

For each of `_dispatch_understanding` and `_dispatch_generation`, add `group_id: Optional[str] = None, cache_scope: str = "group"` parameters (after `key_hash`), and update their `dispatch()` call sites:

```python
        if pipeline_kind == "understanding":
            return await self._dispatch_understanding(body, request, engine, user_id, key_hash, prefix, group_id, cache_scope)
        return await self._dispatch_generation(body, request, engine, user_id, key_hash, prefix, group_id, cache_scope)
```

- [ ] **Step 4: Rewrite _resolve_cache_scope to three-tier**

Replace `_resolve_cache_scope` (lines 67-88) with:

```python
def _resolve_cache_scope(request: Request, pii_meta: Optional[dict], key_default_scope: str = "group") -> str:
    """Decide this request's cache_scope.

    Priority:
    1. Explicit header X-Cache-Scope=private|group|public
    2. PII detected -> forced private (safety)
    3. Key's configured default scope (aigateway:key.cache_scope)
    4. Fallback "group"
    """
    hdr = (request.headers.get("X-Cache-Scope") or "").strip().lower()
    if hdr in ("private", "group", "public"):
        return hdr
    if pii_meta and pii_meta.get("detected_categories"):
        return "private"
    return key_default_scope or "group"
```

- [ ] **Step 5: Update the cache-key call site (understanding path, ~line 304-313)**

```python
        resolved_scope = _resolve_cache_scope(request, pii_meta, cache_scope)
        cache_key = cache_manager.generate_cache_key(
            normalized_prompt=normalized_messages,
            model=body.model,
            pipeline_kind="understanding",
            cache_scope=resolved_scope,
            user_id=user_id or "",
            group_id=group_id or "",
            temperature=body.temperature if body.temperature is not None else 1.0,
            max_tokens=body.max_tokens,
            top_p=body.top_p,
        )
```

- [ ] **Step 6: Update ctx construction(s) to set group_id**

Grep for `PipelineContext(` in dispatcher.py: `grep -n "PipelineContext(" aigateway-core/src/aigateway_core/dispatch/dispatcher.py`. At each construction site (e.g. ~line 398 in `_dispatch_understanding`, and any in `_dispatch_generation`), add `group_id=group_id` and ensure `cache_scope` is stored in `ctx.extra["cache_scope"]`:

```python
            ctx = PipelineContext(
                request={"messages": body.messages, "model": body.model, "stream": getattr(body, "stream", False)},
                trace_id=request.state.trace_id,
                user_id=user_id or "",
                group_id=group_id or "",
                pipeline_kind="understanding",
                extra={"cache_scope": resolved_scope},
            )
```

- [ ] **Step 7: Update the cache plugin to pass group_id + use ctx scope**

In `prefix/cache/plugin.py` (lines 45-55), change:

```python
        cache_scope = (ctx.extra.get("cache_scope") or "group") if isinstance(ctx.extra, dict) else "group"
        cache_key = cm.generate_cache_key(
            normalized_prompt=normalized,
            model=ctx.request.get("model", ""),
            pipeline_kind=ctx.pipeline_kind or "understanding",
            cache_scope=cache_scope,
            user_id=ctx.user_id or "",
            group_id=ctx.group_id or "",
            temperature=ctx.request.get("temperature", 1.0),
            max_tokens=ctx.request.get("max_tokens"),
            top_p=ctx.request.get("top_p"),
        )
```

- [ ] **Step 8: Build + run test suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS. (No new tests here — behavior covered by test_cache_key_v2.py + manual.)

Also run: `python3 -c "from aigateway_core.dispatch.dispatcher import RequestDispatcher"` to catch import errors.

- [ ] **Step 9: Commit**

```bash
git add aigateway-core/src/aigateway_core/dispatch/context.py \
        aigateway-core/src/aigateway_core/dispatch/dispatcher.py \
        aigateway-core/src/aigateway_core/prefix/cache/plugin.py
git commit -m "feat(cache): resolve three-tier scope + thread group_id into PipelineContext"
```

---

### Task 9: metrics — gateway_cost_by_group Counter + record_cost(group=…)

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/metrics.py`
- Modify: `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` (record_cost callers)
- Modify: `aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py`
- Test: `tests/test_metrics.py` (create or extend)

**Interfaces:**
- Produces: `MetricsCollector.record_cost(cost_usd, model, user_id, group="")` increments `gateway_cost_by_group{group}`; callers pass `group`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_metrics.py`:

```python
"""Metrics tests - gateway_cost_by_group label."""
from aigateway_core.shared.metrics import MetricsCollector


def test_record_cost_by_group_increments_counter():
    mc = MetricsCollector(enabled=True)
    mc.initialize()
    mc.record_cost(1.5, model="gpt-4o", user_id="u1", group="grp-a")
    mc.record_cost(2.0, model="gpt-4o", user_id="u2", group="grp-b")
    # the counter should have two label series
    series = mc._cost_by_group_counter.collect()
    samples = []
    for s in series.samples:
        if s.name.endswith("total"):
            samples.append((s.labels.get("group"), s.value))
    by_group = {g: v for g, v in samples if g}
    assert by_group.get("grp-a") == 1.5
    assert by_group.get("grp-b") == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: FAIL — `record_cost` has no `group` param / `_cost_by_group_counter` missing.

- [ ] **Step 3: Add the Counter + extend record_cost**

In `metrics.py`, add to `__init__` (after `_cost_by_user_counter`, line 100):

```python
        self._cost_by_group_counter: Any = None
```

In `initialize()` (after the `_cost_by_user_counter` Counter creation, ~line 196), add:

```python
        self._cost_by_group_counter = Counter(
            "gateway_cost_by_group",
            "Total cost by user group",
            labelnames=["group"],
            registry=registry,
        )
```

Replace `record_cost` (lines 340-359) with:

```python
    def record_cost(self, cost_usd: float, model: str = "unknown", user_id: str = "", group: str = "") -> None:
        """Record request cost.

        Args:
            cost_usd: cost in USD.
            model: model name.
            user_id: user ID.
            group: group id (label for gateway_cost_by_group).
        """
        if not self.enabled:
            return

        if self._cost_total_gauge:
            self._cost_total_gauge.inc(cost_usd)

        if self._cost_by_model_counter:
            self._cost_by_model_counter.labels(model=model).inc(cost_usd)

        if self._cost_by_user_counter and user_id:
            self._cost_by_user_counter.labels(user_id=user_id).inc(cost_usd)

        if self._cost_by_group_counter and group:
            self._cost_by_group_counter.labels(group=group).inc(cost_usd)
```

- [ ] **Step 4: Pass group at dispatcher record_cost call sites**

In `dispatcher.py`, the non-streaming record_cost (~line 674) and streaming record_cost (~line 836) currently pass `user_id=user_id or ""`. Change both to also pass `group=group_id or ""`. Example (non-streaming):

```python
                metrics_collector.record_cost(final_cost, model=body.model, user_id=user_id or "", group=group_id or "")
```

For the streaming path (~line 836), `group_id` must be in scope — it is, because `_dispatch_generation`/the streaming helper now receives `group_id` (Task 8 Step 3). If the streaming record_cost lives in a nested helper, pass `group_id` through. Grep: `grep -n "record_cost" aigateway-core/src/aigateway_core/dispatch/dispatcher.py`.

- [ ] **Step 5: Pass group in streaming metrics_wrapper**

In `route/streaming/metrics_wrapper.py`, add `group: str = ""` param to `_wrap_stream_for_metrics` and pass it to `record_cost`:

```python
async def _wrap_stream_for_metrics(
    completion_gen: Any,
    metrics_collector: Any,
    model: str,
    user_id: str = "",
    group: str = "",
) -> Any:
    ...
            metrics_collector.record_cost(cost, model=model, user_id=user_id, group=group)
```

(Grep for callers of `_wrap_stream_for_metrics` and pass `group`. If the core dispatcher uses its own inlined streaming metrics (per the module docstring), this wrapper may have no live callers — still update it for parity and update any caller found via grep.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_metrics.py tests/test_cache_key_v2.py tests/test_group_quota.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/metrics.py \
        aigateway-core/src/aigateway_core/dispatch/dispatcher.py \
        aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py \
        tests/test_metrics.py
git commit -m "feat(metrics): gateway_cost_by_group counter + record_cost(group=)"
```

---

### Task 10: admin_routes — group CRUD endpoints + key/group assignment + key fields

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py`

**Interfaces:**
- Produces endpoints: `GET/POST /admin/groups`, `GET/PUT/DELETE /admin/groups/{group_id}`, `PUT /admin/api-keys/{key_id}/group`. `CreateApiKeyRequest` gains `group_id`+`cache_scope`. `_format_quota_item` returns `group_id`/`group_name`/`cache_scope`.

- [ ] **Step 1: Add request models**

In `admin_routes.py`, after `UpdateQuotaRequest` (line 245), add:

```python
class CreateGroupRequest(BaseModel):
    """POST /admin/groups 请求体。"""
    name: str = Field(..., min_length=1, max_length=64, description="组名")
    daily_tokens: Optional[int] = Field(default=None, ge=1)
    monthly_cost: Optional[float] = Field(default=None, gt=0)
    rate_limit_rpm: Optional[int] = Field(default=None, ge=1)
    rate_limit_tpm: Optional[int] = Field(default=None, ge=1)


class UpdateGroupRequest(BaseModel):
    """PUT /admin/groups/{group_id} 请求体。"""
    daily_tokens: Optional[int] = Field(default=None, ge=1)
    monthly_cost: Optional[float] = Field(default=None, gt=0)
    rate_limit_rpm: Optional[int] = Field(default=None, ge=1)
    rate_limit_tpm: Optional[int] = Field(default=None, ge=1)
    status: Optional[str] = Field(default=None, pattern="^(active|suspended)$")


class AssignGroupRequest(BaseModel):
    """PUT /admin/api-keys/{key_id}/group 请求体。"""
    group_id: str = Field(..., min_length=1, description="目标 group_id")
```

Extend `CreateApiKeyRequest` (line 229) with two fields:

```python
class CreateApiKeyRequest(BaseModel):
    """POST /admin/api-keys 请求体。"""
    user_id: str = Field(..., min_length=1, description="关联的用户 ID")
    group_id: str = Field(..., min_length=1, description="所属用户组 group_id")
    cache_scope: Optional[str] = Field(default="group", pattern="^(private|group|public)$")
    daily_tokens: Optional[int] = Field(default=None, description="每日 token 上限")
    monthly_cost: Optional[float] = Field(default=None, description="每月成本上限（美元）")
    rate_limit_rpm: Optional[int] = Field(default=None, description="每分钟请求数上限")
    rate_limit_tpm: Optional[int] = Field(default=None, description="每分钟 token 数上限")
```

- [ ] **Step 2: Add a group_store helper + extend _format_quota_item**

Near `_get_keystore_and_metrics` (line 253), add:

```python
def _get_group_store(request: Request) -> Any:
    from aigateway_api.main import app
    return getattr(app.state, "group_store", None)
```

Extend `_format_quota_item` (line 304) to include group fields — add to the returned dict:

```python
        "group_id": key_data.get("group_id", ""),
        "cache_scope": key_data.get("cache_scope", "group"),
```

- [ ] **Step 3: Update create_api_key to pass group_id + cache_scope**

In `create_api_key` (line 398-433), change the `key_store.create(...)` call:

```python
        result = await key_store.create(
            user_id=body.user_id, quotas=quotas,
            group_id=body.group_id, cache_scope=body.cache_scope or "group",
        )
```

And after creation, add the key to the group's members (so member counts stay accurate). Add a helper on GroupStore if not present — `assign_key_to_group` handles membership, but for a fresh key use `add_member`:

```python
        group_store = _get_group_store(request)
        if group_store:
            try:
                # key_hash returned via key_store.create? It returns key_id not hash;
                # recompute hash from the returned raw key.
                from aigateway_core.shared.auth.key_store import KeyStore as _KS
                kh = _KS._hash_key(result["key"])
                await group_store.add_member(body.group_id, kh)
            except Exception as exc:
                logger.warning("add_member 失败: %s", exc)
```

- [ ] **Step 4: Add group CRUD endpoints**

Append to `admin_routes.py` (after the api-key endpoints):

```python
# ------------------------------------------------------------------
# /admin/groups
# ------------------------------------------------------------------

@router.get("/groups")
async def list_groups(
    request: Request,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """列出所有用户组。"""
    group_store = _get_group_store(request)
    if not group_store:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "GroupStore not initialized"}})
    groups = await group_store.list_groups()
    return {"data": {"items": groups}, "message": "success"}


@router.post("/groups")
async def create_group(
    request: Request,
    body: CreateGroupRequest,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """创建用户组。"""
    group_store = _get_group_store(request)
    if not group_store:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "GroupStore not initialized"}})
    quotas = {
        "daily_tokens": body.daily_tokens, "monthly_cost": body.monthly_cost,
        "rate_limit_rpm": body.rate_limit_rpm, "rate_limit_tpm": body.rate_limit_tpm,
    }
    try:
        g = await group_store.create_group(body.name, quotas)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": {"code": "conflict", "message": str(exc)}})
    return {"data": g, "message": "success"}


@router.get("/groups/{group_id}")
async def get_group(
    request: Request,
    group_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    group_store = _get_group_store(request)
    detail = await group_store.get_group_detail(group_id) if group_store else None
    if not detail:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"group '{group_id}' not found"}})
    return {"data": detail, "message": "success"}


@router.put("/groups/{group_id}")
async def update_group(
    request: Request,
    group_id: str,
    body: UpdateGroupRequest,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    group_store = _get_group_store(request)
    if not group_store:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "GroupStore not initialized"}})
    quotas = {
        "daily_tokens": body.daily_tokens, "monthly_cost": body.monthly_cost,
        "rate_limit_rpm": body.rate_limit_rpm, "rate_limit_tpm": body.rate_limit_tpm,
    }
    quotas = {k: v for k, v in quotas.items() if v is not None}
    try:
        g = await group_store.update_group(group_id, quotas=quotas or None, status=body.status)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": str(exc)}})
    return {"data": g, "message": "success"}


@router.delete("/groups/{group_id}")
async def delete_group(
    request: Request,
    group_id: str,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    group_store = _get_group_store(request)
    if not group_store:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "GroupStore not initialized"}})
    try:
        ok = await group_store.delete_group(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": {"code": "conflict", "message": str(exc)}})
    if not ok:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"group '{group_id}' not found"}})
    return {"data": {"id": group_id, "status": "deleted"}, "message": "success"}


@router.put("/api-keys/{key_id}/group")
async def assign_key_group(
    request: Request,
    key_id: str,
    body: AssignGroupRequest,
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """将 API Key 分配/更换到指定组（迁移已用量）。"""
    key_store, _ = _get_keystore_and_metrics(request)
    group_store = _get_group_store(request)
    if not group_store:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": "GroupStore not initialized"}})
    key_hashes = await key_store._find_key_hashes_by_id(key_id)
    if not key_hashes:
        raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": f"API key '{key_id}' not found"}})
    try:
        result = await group_store.assign_key_to_group(key_hashes[0], body.group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"code": "validation_error", "message": str(exc)}})
    return {"data": result, "message": "success"}
```

- [ ] **Step 5: Build + smoke test**

Run: `python3 -c "from aigateway_api import admin_routes"` (catch import errors). Then run the backend test suite:

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS.

- [ ] **Step 6: Rebuild Docker + verify**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway && sleep 3 && curl -sf localhost:8000/health`
Expected: health OK. Then `docker compose logs --tail=50 gateway | grep -i error` — no errors.

- [ ] **Step 7: Commit**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py
git commit -m "feat(admin): group CRUD endpoints + key/group assignment + key group/scope fields"
```

---

### Task 11: admin_routes — metrics-query proxy endpoint (Prometheus range query)

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py`

**Interfaces:**
- Produces: `GET /admin/metrics-query?query=&start=&end=&step=` proxying to Prometheus `/api/v1/query_range`. Returns the Prom JSON envelope. Uses `PROMETHEUS_URL` env (default `http://prometheus:9090`).

- [ ] **Step 1: Add the endpoint**

Append to `admin_routes.py`:

```python
# ------------------------------------------------------------------
# /admin/metrics-query - Prometheus range query proxy
# ------------------------------------------------------------------

@router.get("/metrics-query")
async def metrics_query(
    request: Request,
    query: str = Query(..., description="PromQL expression"),
    start: Optional[str] = Query(default=None, description="start (unix ts or RFC3339)"),
    end: Optional[str] = Query(default=None, description="end (unix ts or RFC3339)"),
    step: str = Query(default="3600", description="step (duration or seconds)"),
    _auth: Dict[str, Any] = Depends(authenticate_admin),
):
    """Proxy a Prometheus range query (/api/v1/query_range).

    Used by the control panel to fetch real per-day cost trends
    (e.g. increase(gateway_cost_total[24h]) over 7 days).
    """
    import os
    import httpx
    prom_url = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    now = time.time()
    params = {"query": query, "step": step}
    params["start"] = start if start else str(now - 7 * 86400)
    params["end"] = end if end else str(now)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{prom_url}/api/v1/query_range", params=params)
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"error": {"code": "prometheus_unreachable", "message": f"Prometheus query failed: {exc}"}})
```

Ensure `time` is imported at the top of `admin_routes.py` (grep: `grep -n "^import time\|^from time" aigateway-api/src/aigateway_api/admin_routes.py`; add `import time` if missing). `httpx` is available transitively via litellm; verify: `python3 -c "import httpx"`.

- [ ] **Step 2: Rebuild + verify**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway && sleep 3 && curl -sf localhost:8000/health`
Expected: OK.

- [ ] **Step 3: Commit**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py
git commit -m "feat(admin): /admin/metrics-query Prometheus range-query proxy"
```

---

### Task 12: config.yaml seed migration (group field) + template comments

**Files:**
- Modify: `config.yaml.template`, `config.yaml` (comment only — the `group` field already exists; seed behavior changed in Task 3/Task 10 wiring)
- Verify: `aigateway-api/src/aigateway_api/main.py` (Task 3 Step 6 already auto-creates groups from config `group` names)

**Interfaces:** none new.

- [ ] **Step 1: Update config.yaml.template comment**

In `config.yaml.template`, change the `group:` comment (around line 36) from "OPTIONAL: API Key group label (for cost-tracking metric aggregation, does NOT affect resource isolation)" to:

```yaml
      group: admin-team      # 用户组名:启动时自动建组(若不存在),key 归入该组。
                            # 组级配额共享 + 组内缓存共享(private/group/public scope)。
```

- [ ] **Step 2: Mirror the comment in real config.yaml**

In `config.yaml` (line 12 area), ensure the `group: admin-team` line has the same updated comment (or leave as-is if comments are minimal — at minimum keep `group: admin-team`).

- [ ] **Step 3: Commit**

```bash
git add config.yaml config.yaml.template
git commit -m "docs(config): update group field comment (now creates real group)"
```

---

### Task 13: feature_cache + prompt_template — per-scope owner key

**Files:**
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/token/feature_cache.py`
- Modify: `aigateway-core/src/aigateway_core/pipelines/generation/token/prompt_template_manager.py`

**Note:** These subsystems are partially-wired placeholders (per CLAUDE.md: TokenCompressorStrategy is a placeholder; ai_director has a placeholder for PromptTemplateManager). This task makes the key-building scope-aware so group-shared caching works when these paths activate. Keep changes minimal and mechanical.

**Interfaces:**
- Produces: both managers' `_build_key(owner_id, ...)` where `owner_id` is `""` (public) / `group_id` (group) / `user_id` (private). Callers compute `owner_id` from scope.

- [ ] **Step 1: feature_cache — rename api_key_id → owner_id**

In `feature_cache.py`, rename the first param of `_build_key`, `get_feature`, `store_feature`, `extend_ttl` from `api_key_id` to `owner_id` (mechanical rename). Update `_build_key`:

```python
    def _build_key(self, owner_id: str, character_id: str, model_version: str) -> str:
        """Build Redis key. owner_id = '' (public) | group_id (group) | user_id (private)."""
        return f"{self.KEY_PREFIX}:{owner_id}:{character_id}:{model_version}"
```

(public → `aigateway:feature::{character}:{model}` — empty owner_id segment, matches spec §1.5.)

- [ ] **Step 2: Update the feature_cache caller (token_compressor_plugin)**

In `pipelines/generation/token/token_compressor_plugin.py` (~line 353 `self._cache.get_feature(...)`), compute owner_id from the ctx scope/group/user and pass it. Grep: `grep -n "get_feature\|store_feature\|api_key_id" aigateway-core/src/aigateway_core/pipelines/generation/token/token_compressor_plugin.py`. Replace the `api_key_id` argument with a computed `owner_id`:

```python
            # owner: group_id (group scope) | user_id (private) | "" (public)
            scope = (ctx.extra.get("cache_scope") or "group") if ctx else "group"
            if scope == "private":
                owner_id = ctx.user_id or ""
            elif scope == "public":
                owner_id = ""
            else:
                owner_id = ctx.group_id or ""
            vector = await self._cache.get_feature(owner_id, character_id, model_version)
```

(If the plugin does not have `ctx` in scope at that call site, thread the `owner_id`/scope through the plugin's `execute` from `ctx`. Adapt to the actual local variable names found by the grep.)

- [ ] **Step 3: prompt_template_manager — rename api_key_id → owner_id**

In `prompt_template_manager.py`, rename `api_key_id` → `owner_id` in `_build_key`, `_build_index_key`, `get`, `save`/`store`, `list`, `delete`. Update docstrings' key format comment (lines 8, 46) to note owner_id semantics. `_build_key`:

```python
    def _build_key(self, owner_id: str, name: str) -> str:
        return f"{self.KEY_PREFIX}:{owner_id}:{name}"
```

- [ ] **Step 4: Update template_routes callers**

In `aigateway-api/src/aigateway_api/template_routes.py`, the endpoints pass `api_key_id` to the manager. Grep: `grep -n "api_key_id\|prompt_template_manager" aigateway-api/src/aigateway_api/template_routes.py`. These endpoints resolve the caller's key; compute `owner_id` from the requesting key's `group_id`/`cache_scope`/`user_id` (from `request.state.api_key_data`) the same way as Step 2, and pass `owner_id` instead of `api_key_id`.

- [ ] **Step 5: Build + test**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS.

Run: `python3 -c "from aigateway_core.pipelines.generation.token.feature_cache import FeatureCacheManager; from aigateway_core.pipelines.generation.token.prompt_template_manager import PromptTemplateManager"`
Expected: no import error.

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/pipelines/generation/token/feature_cache.py \
        aigateway-core/src/aigateway_core/pipelines/generation/token/prompt_template_manager.py \
        aigateway-core/src/aigateway_core/pipelines/generation/token/token_compressor_plugin.py \
        aigateway-api/src/aigateway_api/template_routes.py
git commit -m "refactor(cache): feature/template caches use scope-derived owner_id"
```

---

### Task 14: Frontend — types.ts + api/client.ts (groups CRUD + metricsQuery + key fields)

**Files:**
- Modify: `control-panel/src/types.ts`
- Modify: `control-panel/src/api/client.ts`

**Interfaces:**
- Produces TS types: `Group`, `GroupQuotas`, `CreateGroupRequest`, `UpdateGroupRequest`; `ApiKeyItem`+`CreateApiKeyRequest` gain `group_id`/`group_name`/`cache_scope`. Client fns: `listGroups`, `createGroup`, `getGroup`, `updateGroup`, `deleteGroup`, `assignKeyGroup`, `metricsQuery`.

- [ ] **Step 1: Add types to types.ts**

Append to `control-panel/src/types.ts`:

```typescript
// ------------------------------------------------------------------
// User Groups
// ------------------------------------------------------------------

export interface GroupQuotas {
  daily_tokens_limit: number
  daily_tokens_used: number
  monthly_cost_limit: number
  monthly_cost_used: number
  rate_limit_rpm: number
  rate_limit_tpm: number
}

export interface Group {
  group_id: string
  name: string
  status: 'active' | 'suspended'
  created_at: string
  updated_at: string
  member_count: number
  daily_tokens_limit: number
  daily_tokens_used: number
  monthly_cost_limit: number
  monthly_cost_used: number
  rate_limit_rpm: number
  rate_limit_tpm: number
}

export interface GroupListData {
  items: Group[]
}

export interface CreateGroupRequest {
  name: string
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
}

export interface UpdateGroupRequest {
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
  status?: 'active' | 'suspended'
}

export interface AssignGroupRequest {
  group_id: string
}

export type CacheScope = 'private' | 'group' | 'public'
```

Extend `ApiKeyItem` (add fields) and `CreateApiKeyRequest`:

```typescript
export interface ApiKeyItem {
  id: string
  key_prefix: string
  user_id: string
  group_id: string
  group_name?: string
  cache_scope: CacheScope
  created_at: string
  last_used_at: string | null
  status: 'active' | 'revoked' | 'suspended'
  quotas: ApiKeyQuotas
  usage_percentage: ApiKeyUsagePercentage
}

export interface CreateApiKeyRequest {
  user_id: string
  group_id: string
  cache_scope?: CacheScope
  daily_tokens?: number
  monthly_cost?: number
  rate_limit_rpm?: number
  rate_limit_tpm?: number
}
```

- [ ] **Step 2: Add client functions to api/client.ts**

Append to `control-panel/src/api/client.ts`:

```typescript
// ------------------------------------------------------------------
// User Groups
// ------------------------------------------------------------------

export async function listGroups(): Promise<ApiResponse<GroupListData>> {
  return fetchJson<GroupListData>('/admin/groups')
}

export async function createGroup(body: CreateGroupRequest): Promise<ApiResponse<Group>> {
  return fetchJson<Group>('/admin/groups', { method: 'POST', body: JSON.stringify(body) })
}

export async function getGroup(groupId: string): Promise<ApiResponse<Group>> {
  return fetchJson<Group>(`/admin/groups/${encodeURIComponent(groupId)}`)
}

export async function updateGroup(groupId: string, body: UpdateGroupRequest): Promise<ApiResponse<Group>> {
  return fetchJson<Group>(`/admin/groups/${encodeURIComponent(groupId)}`, { method: 'PUT', body: JSON.stringify(body) })
}

export async function deleteGroup(groupId: string): Promise<ApiResponse<{ id: string; status: string }>> {
  return fetchJson<{ id: string; status: string }>(`/admin/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' })
}

export async function assignKeyGroup(keyId: string, groupId: string): Promise<ApiResponse<unknown>> {
  return fetchJson<unknown>(`/admin/api-keys/${encodeURIComponent(keyId)}/group`, {
    method: 'PUT', body: JSON.stringify({ group_id: groupId } as AssignGroupRequest),
  })
}

// ------------------------------------------------------------------
// Prometheus range-query proxy (real per-day cost trend)
// ------------------------------------------------------------------

export interface PromQueryResult {
  status: string
  data: {
    resultType: string
    result: Array<{ metric: Record<string, string>; values: Array<[number, string]> }>
  }
}

export async function metricsQuery(params: {
  query: string
  start?: string
  end?: string
  step?: string
}): Promise<PromQueryResult> {
  const qs = new URLSearchParams({ query: params.query, step: params.step || '3600' })
  if (params.start) qs.set('start', params.start)
  if (params.end) qs.set('end', params.end)
  const resp = await fetchJson<PromQueryResult>(`/admin/metrics-query?${qs}`)
  return resp.data
}
```

> `/admin/metrics-query` returns the raw Prom envelope `{status, data:{resultType, result}}`. `fetchJson` wraps every response in `{data, message}`, so `resp.data` is the Prom envelope (matching `PromQueryResult`). Callers read `resp.data.data.result[0].values`.

Add the needed type imports at the top of client.ts (grep existing imports from `@/types` and append `Group, GroupListData, CreateGroupRequest, UpdateGroupRequest, AssignGroupRequest`).

- [ ] **Step 3: Typecheck + build**

Run: `cd control-panel && npm run build`
Expected: PASS (tsc + vite build, no type errors).

- [ ] **Step 4: Commit**

```bash
git add control-panel/src/types.ts control-panel/src/api/client.ts
git commit -m "feat(panel): group CRUD client fns + metricsQuery + key group/scope types"
```

---

### Task 15: Frontend — Quotas.tsx Tab + group management + key forms

**Files:**
- Modify: `control-panel/src/pages/Quotas.tsx`

**Goal:** Add a `[API Keys] [用户组]` Tab at the top. The 用户组 Tab shows a group list + create/edit form. The API Keys Tab's create form gains 用户组 + 缓存共享范围 dropdowns; the key list gains 用户组 + 缓存范围 columns; the edit form gains 缓存共享范围 (and group reassignment via the Eye/details path is out of scope — keep the existing edit form but add a scope field).

- [ ] **Step 1: Add Tab state + group state + group fetch**

At the top of `Quotas()` (after the existing state, ~line 29), add:

```tsx
  const [activeTab, setActiveTab] = useState<'keys' | 'groups'>('keys')
  const [groups, setGroups] = useState<Group[]>([])
  const [showCreateGroup, setShowCreateGroup] = useState(false)
  const [createGroupForm, setCreateGroupForm] = useState<CreateGroupRequest>({
    name: '', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000,
  })
  const [editingGroup, setEditingGroup] = useState<Group | null>(null)
  const [editGroupForm, setEditGroupForm] = useState<UpdateGroupRequest & { name?: string }>({})
```

Update the imports (line 4) to add group client fns + types:

```tsx
import {
  listApiKeys, deleteApiKey, createApiKey, updateApiKeyQuota,
  listGroups, createGroup, updateGroup, deleteGroup,
} from '@/api/client'
import type { ApiKeyItem, CreateApiKeyRequest, CreateApiKeyData, Group, CreateGroupRequest, UpdateGroupRequest } from '@/types'
```

Extend `createForm` default with `group_id` + `cache_scope`:

```tsx
  const [createForm, setCreateForm] = useState<CreateApiKeyRequest>({
    user_id: '',
    group_id: '',
    cache_scope: 'group',
    daily_tokens: 1_000_000,
    monthly_cost: 50,
    rate_limit_rpm: 60,
    rate_limit_tpm: 100_000,
  })
```

Add a `loadGroups` + extend the mount effect:

```tsx
  const loadGroups = () => listGroups().then(r => setGroups(r.data.items)).catch(() => {})

  useEffect(() => {
    listApiKeys().then(r => { setKeys(r.data.items); setLoading(false) }).catch(() => { setLoading(false) })
    loadGroups()
  }, [])
```

Refresh `loadGroups()` after create/edit/delete group and after create key.

- [ ] **Step 2: Add group handlers**

```tsx
  const handleCreateGroup = async () => {
    if (!createGroupForm.name.trim()) return
    try {
      await createGroup(createGroupForm)
      setCreateGroupForm({ name: '', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000 })
      setShowCreateGroup(false)
      loadGroups()
    } catch { alert('创建用户组失败') }
  }

  const handleStartEditGroup = (g: Group) => {
    setEditingGroup(g)
    setEditGroupForm({
      daily_tokens: g.daily_tokens_limit, monthly_cost: g.monthly_cost_limit,
      rate_limit_rpm: g.rate_limit_rpm, rate_limit_tpm: g.rate_limit_tpm, status: g.status, name: g.name,
    })
  }

  const handleSaveGroup = async () => {
    if (!editingGroup) return
    try {
      await updateGroup(editingGroup.group_id, {
        daily_tokens: editGroupForm.daily_tokens,
        monthly_cost: editGroupForm.monthly_cost,
        rate_limit_rpm: editGroupForm.rate_limit_rpm,
        rate_limit_tpm: editGroupForm.rate_limit_tpm,
        status: editGroupForm.status,
      })
      setEditingGroup(null)
      loadGroups()
    } catch { alert('修改用户组失败') }
  }

  const handleDeleteGroup = async (groupId: string) => {
    if (!confirm('确定删除该用户组?需先迁移组内所有 Key。')) return
    try {
      await deleteGroup(groupId)
      loadGroups()
    } catch (e: any) { alert(e?.message || '删除失败(可能组内仍有成员)') }
  }
```

- [ ] **Step 3: Render the Tab + group UI**

Wrap the existing content. Insert a Tab bar right after `<h2>`:

```tsx
      <div className="flex gap-2 mb-4">
        <button className={`btn ${activeTab === 'keys' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setActiveTab('keys')}>API Keys</button>
        <button className={`btn ${activeTab === 'groups' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setActiveTab('groups')}>用户组</button>
      </div>

      {activeTab === 'groups' ? (
        <>
          <div className="flex justify-between items-center mb-4">
            <h3 className="text-md font-semibold">用户组管理</h3>
            <button className="btn btn-primary" onClick={() => setShowCreateGroup(true)}>+ 创建组</button>
          </div>
          {showCreateGroup && (
            <Card>
              <h3 className="text-md font-semibold mb-4">创建用户组</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>组名 *</label>
                  <input className="input w-full" value={createGroupForm.name} onChange={(e) => setCreateGroupForm(f => ({ ...f, name: e.target.value }))} />
                </div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label><input className="input w-full" type="number" value={createGroupForm.daily_tokens} onChange={(e) => setCreateGroupForm(f => ({ ...f, daily_tokens: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label><input className="input w-full" type="number" step={0.01} value={createGroupForm.monthly_cost} onChange={(e) => setCreateGroupForm(f => ({ ...f, monthly_cost: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label><input className="input w-full" type="number" value={createGroupForm.rate_limit_rpm} onChange={(e) => setCreateGroupForm(f => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label><input className="input w-full" type="number" value={createGroupForm.rate_limit_tpm} onChange={(e) => setCreateGroupForm(f => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} /></div>
              </div>
              <div className="flex gap-2 mt-4">
                <button className="btn btn-primary" onClick={handleCreateGroup}>创建</button>
                <button className="btn btn-secondary" onClick={() => setShowCreateGroup(false)}>取消</button>
              </div>
            </Card>
          )}
          {editingGroup && (
            <Card>
              <h3 className="text-md font-semibold mb-4">修改用户组 - {editingGroup.name}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>日 token 上限</label><input className="input w-full" type="number" value={editGroupForm.daily_tokens} onChange={(e) => setEditGroupForm(f => ({ ...f, daily_tokens: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>月成本上限 ($)</label><input className="input w-full" type="number" step={0.01} value={editGroupForm.monthly_cost} onChange={(e) => setEditGroupForm(f => ({ ...f, monthly_cost: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>RPM 限制</label><input className="input w-full" type="number" value={editGroupForm.rate_limit_rpm} onChange={(e) => setEditGroupForm(f => ({ ...f, rate_limit_rpm: Number(e.target.value) }))} /></div>
                <div><label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>TPM 限制</label><input className="input w-full" type="number" value={editGroupForm.rate_limit_tpm} onChange={(e) => setEditGroupForm(f => ({ ...f, rate_limit_tpm: Number(e.target.value) }))} /></div>
                <div>
                  <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>状态</label>
                  <select className="input w-full" value={editGroupForm.status} onChange={(e) => setEditGroupForm(f => ({ ...f, status: e.target.value as 'active' | 'suspended' }))}>
                    <option value="active">active</option>
                    <option value="suspended">suspended</option>
                  </select>
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <button className="btn btn-primary" onClick={handleSaveGroup}>保存</button>
                <button className="btn btn-secondary" onClick={() => setEditingGroup(null)}>取消</button>
              </div>
            </Card>
          )}
          <Card>
            <div className="table-container">
              <table>
                <thead><tr><th>组名</th><th>状态</th><th>日 Token</th><th>月成本</th><th>成员数</th><th>操作</th></tr></thead>
                <tbody>
                  {groups.length === 0 ? (
                    <tr><td colSpan={6} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>暂无用户组</td></tr>
                  ) : groups.map(g => (
                    <tr key={g.group_id}>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{g.name}</td>
                      <td><span className={`badge ${g.status === 'active' ? 'badge-success' : 'badge-warning'}`}>{g.status}</span></td>
                      <td>{g.daily_tokens_used} / {g.daily_tokens_limit}</td>
                      <td>${g.monthly_cost_used.toFixed(2)} / ${g.monthly_cost_limit.toFixed(2)}</td>
                      <td>{g.member_count}</td>
                      <td>
                        <div className="flex gap-1">
                          <button className="p-1.5 rounded cursor-pointer" style={{ color: 'var(--color-text-tertiary)' }} title="编辑" onClick={() => handleStartEditGroup(g)}><Edit3 size={16} /></button>
                          {g.group_id !== 'grp-default' && (
                            <button className="p-1.5 rounded cursor-pointer" style={{ color: 'var(--color-danger)' }} title="删除" onClick={() => handleDeleteGroup(g.group_id)}><Trash2 size={16} /></button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      ) : (
        <>
          {/* ===== existing API Keys content (search, create form, edit form, list table) goes here ===== */}
        </>
      )}
```

Move the existing search/create/edit/list JSX into the `: (` else branch (wrap existing blocks).

- [ ] **Step 4: Add 用户组 + 缓存共享范围 to the create-key form**

In the create form grid, add a group `<select>` (populated from `groups`) and a cache_scope `<select>`:

```tsx
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>用户组 *</label>
              <select className="input w-full" value={createForm.group_id} onChange={(e) => setCreateForm((f) => ({ ...f, group_id: e.target.value }))}>
                <option value="">请选择组</option>
                {groups.map(g => <option key={g.group_id} value={g.group_id}>{g.name}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>缓存共享范围</label>
              <select className="input w-full" value={createForm.cache_scope} onChange={(e) => setCreateForm((f) => ({ ...f, cache_scope: e.target.value as any }))}>
                <option value="private">private (仅本人)</option>
                <option value="group">group (组内共享)</option>
                <option value="public">public (全系统)</option>
              </select>
            </div>
```

Update `handleCreate` to validate `group_id`:

```tsx
  const handleCreate = async () => {
    if (!createForm.user_id.trim()) return
    if (!createForm.group_id) { alert('请选择用户组'); return }
    try {
      const resp = await createApiKey(createForm)
      setJustCreatedKey(resp.data)
      setCreateForm({ user_id: '', group_id: '', cache_scope: 'group', daily_tokens: 1_000_000, monthly_cost: 50, rate_limit_rpm: 60, rate_limit_tpm: 100_000 })
      const r = await listApiKeys(); setKeys(r.data.items)
      loadGroups()
    } catch { alert('创建 API Key 失败') }
  }
```

- [ ] **Step 5: Add 用户组 + 缓存范围 columns to the key list table**

Add two `<th>` (`用户组`, `缓存范围`) and two `<td>` per row:

```tsx
                    <td style={{ fontSize: 'var(--font-size-sm)' }}>{key.group_id ? (groups.find(g => g.group_id === key.group_id)?.name || key.group_id) : '-'}</td>
                    <td style={{ fontSize: 'var(--font-size-sm)' }}>{key.cache_scope || 'group'}</td>
```

Update the `colSpan` values (loading/empty rows) from 7 to 9.

- [ ] **Step 6: Build + manual verify**

Run: `cd control-panel && npm run build`
Expected: PASS. Then rebuild frontend container: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel`. Manually verify: Quotas page shows Tab; 用户组 Tab lists groups; create key form requires a group; key list shows group + scope columns.

- [ ] **Step 7: Commit**

```bash
git add control-panel/src/pages/Quotas.tsx
git commit -m "feat(panel): Quotas page Tab + group management + key group/scope fields"
```

---

### Task 16: Frontend — Cache.tsx MISS red + Costs.tsx pie-by-group + real trend

**Files:**
- Modify: `control-panel/src/pages/Cache.tsx`
- Modify: `control-panel/src/pages/Costs.tsx`

- [ ] **Step 1: Cache.tsx — import Cell + per-bar color**

In `Cache.tsx`, add `Cell` to the recharts import (line 2):

```tsx
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend, Cell } from 'recharts'
```

Replace the single `<Bar ...>` element (line ~270) with per-bar coloring:

```tsx
                  <Bar dataKey="hits" name="Count" radius={[4, 4, 0, 0]}>
                    {chartData.map((entry) => (
                      <Cell key={entry.tier} fill={entry.tier === 'MISS' ? 'var(--color-danger)' : 'var(--color-success)'} />
                    ))}
                  </Bar>
```

- [ ] **Step 2: Costs.tsx — pie by group + real 7-day trend**

Replace the fabricated 7-day block (lines ~36-47, the `const dailyCost = costTotal / 7 ...` block) with a real fetch. Add state + effect for the trend:

```tsx
import { getMetricsText, parseMetrics, metricsQuery } from '@/api/client'
```

Add state:

```tsx
  const [costByGroup, setCostByGroup] = useState<{ name: string; cost: number }[]>([])
```

In the `load()` function, replace the `costByModel` parsing with `costByGroup`:

```tsx
        const groupSamples = samples.filter(s => s.name === 'gateway_cost_by_group_total')
        ...
        setCostByGroup(groupSamples.map(m => ({ name: m.labels.group || 'ungrouped', cost: m.value })))
```

(Keep `costTotal` from `gateway_cost_total`, `totalRequests` from `gateway_http_requests_total`.)

Add a separate effect for the real 7-day trend (replaces the fabricated `costHistory`):

```tsx
  const [costHistory, setCostHistory] = useState<{ date: string; cost: number }[]>([])

  useEffect(() => {
    let cancelled = false
    async function loadTrend() {
      try {
        const end = Math.floor(Date.now() / 1000)
        const start = end - 7 * 86400
        const resp = await metricsQuery({ query: 'increase(gateway_cost_total[24h])', start: String(start), end: String(end), step: '86400' })
        if (cancelled || !resp?.data?.result?.length) { setCostHistory([]); return }
        const series = resp.data.result[0].values
        const history = series.map(([ts, val], i) => {
          const d = new Date(ts * 1000)
          return { date: `${d.getMonth() + 1}/${d.getDate()}`, cost: Math.round(parseFloat(val) * 100) / 100 }
        })
        if (!cancelled) setCostHistory(history)
      } catch {
        if (!cancelled) setCostHistory([])
      }
    }
    loadTrend()
    const interval = setInterval(loadTrend, 60000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])
```

Update the pie chart JSX (lines ~118-147) to use `costByGroup` instead of `costByModel`:

```tsx
        <Card title="成本分布 by 用户组">
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={60} outerRadius={100} paddingAngle={2} dataKey="cost">
                {pieData.map((_entry, index) => (
                  <Cell key={`cell-${index}`} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip contentStyle={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border)', borderRadius: 8 }} />
            </PieChart>
          </ResponsiveContainer>
          {/* legend: show all groups, not just first 4 */}
          <div className="grid grid-cols-2 gap-2 mt-4">
            {pieData.map((m, i) => (
              <div key={m.name} className="flex items-center gap-2">
                <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: CHART_COLORS[i % CHART_COLORS.length] }} />
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{m.name} - ${m.cost.toFixed(4)}</span>
              </div>
            ))}
          </div>
        </Card>
```

Where `pieData` is now:

```tsx
  const pieData = costByGroup.length > 0 ? costByGroup : [{ name: '暂无数据', cost: 0 }]
```

- [ ] **Step 3: Build + manual verify**

Run: `cd control-panel && npm run build`
Expected: PASS. Rebuild: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel`. Verify: Cache MISS bar is red; Costs pie shows by-group; 7-day trend shows real per-day bars (will be 0/empty until Prom has 24h of data — that's expected; confirm no NaN/`total/7` flat line).

- [ ] **Step 4: Commit**

```bash
git add control-panel/src/pages/Cache.tsx control-panel/src/pages/Costs.tsx
git commit -m "feat(panel): MISS bar red; cost dist by group; real 7-day trend via Prom"
```

---

### Task 17: Docs — DB_SCHEMA.md + CLAUDE.md

**Files:**
- Modify: `docs/DB_SCHEMA.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update DB_SCHEMA.md**

Add a new section for group keys:

```markdown
## §6 User Groups (aigateway:group:*)

| Key | Type | Purpose |
|---|---|---|
| `aigateway:group:{group_id}` | Hash | Group record + group-level used counters (daily_tokens_used, monthly_cost_used, rpm/tpm window) — isomorphic to `aigateway:key:{hash}` |
| `aigateway:group_lookup:{name}` | String | group name -> group_id reverse lookup (name unique) |
| `aigateway:group:{group_id}:members` | SET | member key_hash set |
| `aigateway:quota:{group_id}:{period}` | Hash | group-level historical usage (reuses key-quota schema) |
| `aigateway:groups:index` | SET | all group_ids |
| `aigateway:groups:sync` | Pub/Sub | group CRUD events |

`group_id` format: `grp-{slug}`. System default group: `grp-default` (receives all pre-existing groupless keys on startup migrate; cannot be deleted).

API key hash gains fields: `group_id`, `cache_scope` (private|group|public, default group).

Cache key v2 scope tiers (replaces tenant_id): public (no identity segment) / group (`g={group_id}`) / private (`u={user_id}`).
```

Also update the §3 cache-key v2 description to drop `tenant_id` and describe the three scope tiers.

- [ ] **Step 2: Update CLAUDE.md**

In the "Security & Quotas" section, add a line:

```markdown
`GroupStore` - Redis hash per group (`aigateway:group:{group_id}`), isomorphic to KeyStore. Group-level quotas (daily tokens/monthly cost/RPM/TPM) checked first, then key-level; both incremented per request. `group_id` persisted on each key; `cache_scope` (private/group/public) per key. System default group `grp-default` absorbs pre-existing groupless keys at startup.
```

In "Cache Key v2", replace the tenant_id line with the three scope tiers. Trim other sections if CLAUDE.md exceeds ~300 lines (run `wc -l CLAUDE.md` first).

- [ ] **Step 3: Commit**

```bash
git add docs/DB_SCHEMA.md CLAUDE.md
git commit -m "docs: user groups + cache scope tiers in DB_SCHEMA and CLAUDE.md"
```

---

## Final Verification

- [ ] **Full backend test suite:** `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py` — all pass.
- [ ] **Frontend build:** `cd control-panel && npm run build` — pass.
- [ ] **Docker rebuild + health:** `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway control-panel && curl -sf localhost:8000/health` — OK; `docker compose logs --tail=50 gateway | grep -i error` — clean.
- [ ] **End-to-end manual:**
  1. Quotas page → 用户组 Tab → create a group → create a key assigned to it (cache_scope=group).
  2. Send a few chat requests with that key; verify group + key `daily_tokens_used`/`monthly_cost_used` both increment (check via `/admin/groups` and `/admin/api-keys`).
  3. Exceed the personal (key) monthly cost limit → request rejected with `quota_exceeded_monthly_cost`; exceed group limit → `quota_exceeded_group_monthly_cost`.
  4. Cache page → MISS bar is red.
  5. Costs page → pie shows by-group slices; 7-day trend shows real per-day values (non-flat after Prom collects 24h).
- [ ] **Push:** after all green, push to `main` (per CLAUDE.md workflow rule 5).
