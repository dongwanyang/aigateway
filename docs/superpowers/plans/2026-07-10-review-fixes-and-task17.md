# Review Fixes + Task 17 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the confirmed code-review findings on the user-groups and cost-charts work, add regression coverage, verify backend/frontend, then execute Task 17 (docs) from the original plan.

**Architecture:** Minimum-correctness fixes only. Repair backend correctness bugs (`pipe_batch` usage, group usage migration, migration atomicity), complete the `/admin/api-keys` response contract, fix the frontend Prometheus query path and interval-cost semantics, add targeted regression tests, then update `docs/DB_SCHEMA.md` and `CLAUDE.md` to match the final implementation.

**Tech Stack:** Python 3 / FastAPI / redis-py (async) / pytest (backend); React + TypeScript + Vite + recharts (frontend). Backend tests run with `python3 -no-alias` (`python3 -m pytest`). Frontend verified with `npm run build` (`tsc -b && vite build`).

## Global Constraints

- Backend tests use `python3` (no `python` alias). No `conftest.py`/`pytest.ini`. Run targeted files: `python3 -m pytest tests/<file> -v`.
- `cache_scope` values are exactly `private` | `group` | `public`. Default is `group`.
- `group_id` format: `grp-{slug}`. System default group: `grp-default` (immutable, cannot be deleted).
- Conventional commit prefixes: `feat:` / `fix:` / `refactor:` / `docs:` / `test:`.
- No new frontend test framework is introduced; frontend acceptance is `npm run build`.
- Atomic Redis writes use `redis_mgr.pipe_batch(lambda p: [...])` — commands are built inside the lambda, never referencing `p` outside it.
- `API_BASE` (from `import.meta.env.VITE_API_BASE ?? ''`) is the single source of truth for frontend request paths.

---

## File Structure

**Modified backend:**
- `aigateway-core/src/aigateway_core/shared/auth/key_store.py` — fix `create()`/`seed_from_config()` `pipe_batch` usage; make `migrate_groups()` write key record + membership atomically.
- `aigateway-core/src/aigateway_core/shared/auth/group_store.py` — fix `assign_key_to_group()` to move only the selected key's usage, not the whole source group's aggregate.
- `aigateway-api/src/aigateway_api/admin_routes.py` — extend `_format_quota_item()` to include `group_id`, `group_name`, `cache_scope`; resolve `group_name` on the backend.

**Modified frontend:**
- `control-panel/src/api/client.ts` — `metricsQuery()` uses `${API_BASE}/admin/metrics/query_range`.
- `control-panel/src/pages/Costs.tsx` — trend query uses `increase(gateway_cost_total[1h])`; aggregate hourly into per-day cost.

**Modified tests:**
- `tests/test_group_store.py` — extend with multi-member migration test.
- `tests/test_group_quota.py` — extend with create/seed pipe_batch regression tests.

**Modified docs (Task 17):**
- `docs/DB_SCHEMA.md` — group keys section + cache-key v2 scope tiers.
- `CLAUDE.md` — GroupStore note + cache scope tiers.

---

## Task Dependency Order

Tasks 1→2 (backend correctness). 3 (admin contract, depends on 1). 4→5 (frontend, independent of 1-3 but same PR). 6 (broader backend verification, depends on 1-3). 7 (docs, last).

---

### Task 1: Fix `pipe_batch` usage in KeyStore.create / seed_from_config + migration atomicity

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/key_store.py` (lines ~280-312 for `create`, ~402-412 for `seed_from_config`, ~791-822 for `migrate_groups`)
- Test: `tests/test_group_quota.py`

**Interfaces:**
- Consumes: `redis_mgr.pipe_batch(fn)` (existing, accepts `lambda p: [p.hset(...), p.set(...), ...]`).
- Produces: `KeyStore.create(user_id, quotas, group_id="", cache_scope="group")` and `seed_from_config(keys_config)` both execute their multi-key writes atomically without referencing an undefined `p`. `migrate_groups(group_store)` writes the key record update and the destination group membership update together.

- [ ] **Step 1: Write the failing test for create() with group_id/cache_scope**

Add to `tests/test_group_quota.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_group_quota.py::test_create_with_group_and_cache_scope_persists -v`
Expected: FAIL with `NameError: name 'p' is not defined` (or similar from the broken `pipe_batch` call).

- [ ] **Step 3: Fix `create()` pipe_batch usage**

In `aigateway-core/src/aigateway_core/shared/auth/key_store.py`, replace the broken block (the one building `cmds` with an undefined `p` before the `await self.redis.pipe_batch(...)` call) with:

```python
        # Atomic batch: key hash + lookup + daily quota + monthly quota + member
        def _build(pipe):
            ops = [
                pipe.hset(f"aigateway:key:{key_hash}", mapping=key_data),
                pipe.set(f"aigateway:key_lookup:{key_prefix}", key_hash),
                pipe.hset(f"aigateway:quota:{key_hash}:daily:{today}", mapping=quota_base),
                pipe.hset(f"aigateway:quota:{key_hash}:monthly:{month}", mapping=quota_base),
            ]
            if group_id:
                ops.append(pipe.sadd(f"aigateway:group:{group_id}:members", key_hash))
            return ops

        await self.redis.pipe_batch(lambda pipe: _build(pipe))
```

- [ ] **Step 4: Fix `seed_from_config()` pipe_batch usage**

In the same file, replace the broken `cmds`/`pipe_batch` block inside the `else` branch (new-key creation) with:

```python
                # Atomic batch: key hash + lookup + daily quota + monthly quota + member
                def _build(pipe):
                    ops = [
                        pipe.hset(f"aigateway:key:{key_hash}", mapping=key_data),
                        pipe.set(f"aigateway:key_lookup:{key_prefix}", key_hash),
                        pipe.hset(f"aigateway:quota:{key_hash}:daily:{today}", mapping=quota_base),
                        pipe.hset(f"aigateway:quota:{key_hash}:monthly:{month}", mapping=quota_base),
                    ]
                    if cfg_group:
                        ops.append(pipe.sadd(f"aigateway:group:{cfg_group}:members", key_hash))
                    return ops

                await self.redis.pipe_batch(lambda pipe: _build(pipe))
```

- [ ] **Step 5: Write the failing test for migrate_groups atomicity**

Add to `tests/test_group_store.py`:

```python
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
```

(The existing `test_migrate_groupless_keys_to_default` already asserts the end state; this step confirms the test is green after the fix below. If it already passes, skip to Step 6.)

- [ ] **Step 6: Make `migrate_groups()` atomic**

In `aigateway-core/src/aigateway_core/shared/auth/key_store.py`, replace the per-key migration block inside the scan loop:

```python
                if not gid:
                    cs = data.get("cache_scope", "group")
                    await self.redis.set_api_key(kh, {"group_id": default_id, "cache_scope": cs})
                    await group_store.add_member(default_id, kh)
                    migrated += 1
                else:
                    # ensure membership tracked even for already-grouped keys
                    await group_store.add_member(gid, kh)
```

with:

```python
                if not gid:
                    cs = data.get("cache_scope", "group")
                    # Atomic: key record + destination membership together
                    def _build(pipe, _kh=kh, _gid=default_id, _cs=cs):
                        return [
                            pipe.hset(f"aigateway:key:{_kh}",
                                      mapping={"group_id": _gid, "cache_scope": _cs}),
                            pipe.sadd(f"aigateway:group:{_gid}:members", _kh),
                        ]
                    await self.redis.pipe_batch(lambda pipe: _build(pipe))
                    migrated += 1
                else:
                    # ensure membership tracked even for already-grouped keys
                    await group_store.add_member(gid, kh)
```

- [ ] **Step 7: Run the targeted tests**

Run: `python3 -m pytest tests/test_group_quota.py tests/test_group_store.py -v`
Expected: all PASS, including `test_create_with_group_and_cache_scope_persists` and `test_migrate_groupless_keys_to_default`.

- [ ] **Step 8: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/key_store.py tests/test_group_quota.py tests/test_group_store.py
git commit -m "fix(keystore): atomic pipe_batch in create/seed + migrate_groups key+membership

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

### Task 2: Fix `assign_key_to_group()` to move only the selected key's usage

**Files:**
- Modify: `aigateway-core/src/aigateway_core/shared/auth/group_store.py` (lines ~243-301)
- Test: `tests/test_group_quota.py`

**Interfaces:**
- Consumes: key record fields `daily_tokens_used`, `monthly_cost_used`; group record fields `daily_tokens_used`, `monthly_cost_used`.
- Produces: `assign_key_to_group(key_hash, new_group_id)` moves only the moved key's personal usage between groups; remaining members' usage stays in the source group.

- [ ] **Step 1: Write the failing test for multi-member migration**

Add to `tests/test_group_quota.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_group_quota.py::test_assign_key_preserves_other_members_usage -v`
Expected: FAIL — source group usage is zeroed to `"0"` / `0.0` and destination gets the full `300`/`15.0`.

- [ ] **Step 3: Fix the usage-transfer logic**

In `aigateway-core/src/aigateway_core/shared/auth/group_store.py`, replace the "# 3. Transfer usage from old → new" block (which reads `old_data`/`new_data` aggregates and zeroes `old_data`) with logic that moves only the key's personal usage:

```python
        # 3. Transfer only the moved key's personal usage from old → new.
        #    (Do NOT move the source group's aggregate — other members stay.)
        moved_daily = int(key_data.get("daily_tokens_used", "0"))
        moved_monthly = float(key_data.get("monthly_cost_used", "0.0"))

        if old_data:
            old_data["daily_tokens_used"] = str(
                max(0, int(old_data.get("daily_tokens_used", "0")) - moved_daily)
            )
            old_data["monthly_cost_used"] = str(round(
                max(0.0, float(old_data.get("monthly_cost_used", "0.0")) - moved_monthly), 4
            ))

        new_data["daily_tokens_used"] = str(
            int(new_data.get("daily_tokens_used", "0")) + moved_daily
        )
        new_data["monthly_cost_used"] = str(round(
            float(new_data.get("monthly_cost_used", "0.0")) + moved_monthly, 4
        ))
```

Leave the rest of `assign_key_to_group` (the atomic `pipe_batch` for group/member/key writes, and the publish) unchanged.

- [ ] **Step 4: Run the targeted tests**

Run: `python3 -m pytest tests/test_group_quota.py tests/test_group_store.py -v`
Expected: all PASS, including `test_assign_key_preserves_other_members_usage` and the existing `test_assign_key_to_group_migrates_usage`.

- [ ] **Step 5: Commit**

```bash
git add aigateway-core/src/aigateway_core/shared/auth/group_store.py tests/test_group_quota.py
git commit -m "fix(groups): move only selected key's usage in assign_key_to_group

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

### Task 3: Complete `/admin/api-keys` response contract (group_id / group_name / cache_scope)

**Files:**
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py` (`_format_quota_item` ~lines 292-327, and `list_api_keys` ~lines 335-392)
- Test: `tests/test_group_quota.py` (or a new focused test if a route-level harness exists; otherwise document the manual contract check)

**Interfaces:**
- Consumes: `key_data` dict (already has `group_id`, `cache_scope`); `GroupStore` on `app.state` for `group_name` lookup.
- Produces: each item in `GET /admin/api-keys` `data.items` includes `group_id: str`, `group_name: str | None`, `cache_scope: str`.

- [ ] **Step 1: Extend `_format_quota_item` to accept and emit group fields**

In `aigateway-api/src/aigateway_api/admin_routes.py`, change the signature and return of `_format_quota_item`:

```python
def _format_quota_item(key_data: Dict[str, Any], key_hash: str, group_name: Optional[str] = None) -> Dict[str, Any]:
    """格式化单个 API Key 的配额信息。"""
    defaults = _get_auth_defaults()
    daily_limit = int(key_data.get("daily_tokens_limit", defaults["daily_tokens"]))
    daily_used = int(key_data.get("daily_tokens_used", 0))
    monthly_limit = float(key_data.get("monthly_cost_limit", defaults["monthly_cost"]))
    monthly_used = float(key_data.get("monthly_cost_used", 0.00))
    rpm_limit = int(key_data.get("rate_limit_rpm", defaults["rate_limit_rpm"]))
    tpm_limit = int(key_data.get("rate_limit_tpm", defaults["rate_limit_tpm"]))

    # 获取当前 RPM/TPM 窗口计数
    rpm_current = int(key_data.get("rpm_window_count", 0))
    tpm_current = int(key_data.get("tpm_window_count", 0))

    return {
        "id": key_data.get("key_id", ""),
        "key_prefix": key_data.get("key_prefix", ""),
        "user_id": key_data.get("user_id", ""),
        "group_id": key_data.get("group_id", "") or "",
        "group_name": group_name,
        "cache_scope": key_data.get("cache_scope", "group") or "group",
        "created_at": key_data.get("created_at", ""),
        "last_used_at": key_data.get("last_used_at") or None,
        "status": key_data.get("status", "active"),
        "quotas": {
            "daily_tokens_used": daily_used,
            "daily_tokens_limit": daily_limit,
            "monthly_cost_used": round(monthly_used, 2),
            "monthly_cost_limit": monthly_limit,
            "rpm_current": rpm_current,
            "rpm_limit": rpm_limit,
            "tpm_current": tpm_current,
            "tpm_limit": tpm_limit,
        },
        "usage_percentage": {
            "daily_tokens": round(daily_used / daily_limit, 4) if daily_limit > 0 else 0.0,
            "monthly_cost": round(monthly_used / monthly_limit, 4) if monthly_limit > 0 else 0.0,
        },
    }
```

- [ ] **Step 2: Resolve `group_name` in `list_api_keys`**

In `list_api_keys`, after building `all_keys`, resolve group names in a single pass before formatting. Replace the `items = [_format_quota_item(...)]` line with:

```python
    # Resolve group_name for each key (one lookup per distinct group_id)
    from aigateway_api.main import app as _app
    group_store = getattr(_app.state, "group_store", None)
    group_name_cache: Dict[str, Optional[str]] = {}
    items: List[Dict[str, Any]] = []
    for k in paginated:
        gid = k.get("group_id", "") or ""
        if gid and gid not in group_name_cache:
            gname: Optional[str] = None
            if group_store is not None:
                try:
                    gdata = await group_store.get_group(gid)
                    if gdata:
                        gname = gdata.get("name")
                except Exception:
                    gname = None
            group_name_cache[gid] = gname
        items.append(_format_quota_item(k, k.get("_key_hash", ""), group_name_cache.get(gid)))
```

- [ ] **Step 3: Verify no other callers of `_format_quota_item` break**

Search for other call sites:

```bash
grep -n "_format_quota_item" aigateway-api/src/aigateway_api/admin_routes.py
```

Any other call site must pass the new `group_name` argument (or rely on the default `None`). Update each to resolve `group_name` from `app.state.group_store` the same way, or pass `None` if group name is not relevant there. Do not leave a call site that would silently drop group info where the frontend expects it.

- [ ] **Step 4: Run existing backend tests to confirm no regression**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: PASS (no test directly covers `_format_quota_item`, so this confirms imports/signatures are not broken elsewhere).

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/admin_routes.py
git commit -m "fix(admin): return group_id/group_name/cache_scope in /admin/api-keys

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

### Task 4: Fix `metricsQuery()` to use `API_BASE`

**Files:**
- Modify: `control-panel/src/api/client.ts` (lines ~869-883, `metricsQuery`)

**Interfaces:**
- Consumes: `API_BASE` (`import.meta.env.VITE_API_BASE ?? ''`), `ensureAuthHeaders()`.
- Produces: `metricsQuery({query, start, end, step})` requests `${API_BASE}/admin/metrics/query_range`.

- [ ] **Step 1: Replace the hard-coded path**

In `control-panel/src/api/client.ts`, replace the `fetch` URL in `metricsQuery`:

```typescript
  const resp = await fetch(`${API_BASE}/admin/metrics/query_range?${qs}`, {
    headers: await ensureAuthHeaders(),
  })
```

(The rest of the function — `qs` construction, error handling, `return resp.json()` — stays the same.)

- [ ] **Step 2: Verify the frontend typechecks/builds**

Run: `cd control-panel && npm run build`
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "fix(client): metricsQuery respects API_BASE

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

### Task 5: Fix `Costs.tsx` cost trend to use interval-cost semantics

**Files:**
- Modify: `control-panel/src/pages/Costs.tsx` (lines ~49-100, `loadTrend`)

**Interfaces:**
- Consumes: `metricsQuery({query, start, end, step})` returning a Prometheus `query_range` matrix.
- Produces: `costHistory` is per-day accumulated cost in USD (not cost-per-second).

- [ ] **Step 1: Replace `rate(...)` with `increase(...)` and aggregate per day**

In `control-panel/src/pages/Costs.tsx`, replace the `loadTrend` body's query and aggregation:

```typescript
    async function loadTrend() {
      try {
        const end = Math.floor(Date.now() / 1000)
        const start = end - 7 * 86400 // 7 days ago
        // increase() over a 1h window yields the cost accrued during that hour
        // (rate() would be cost/second, which understates spend by ~3600x).
        const resp = await metricsQuery({
          query: 'increase(gateway_cost_total[1h])',
          start: String(start),
          end: String(end),
          step: '3600',
        })

        const matrix = resp.data.result ?? []

        // Sum each hourly bucket's accrued cost into its calendar day
        const dayCosts: Record<string, number> = {}
        for (const item of matrix) {
          if (!item.values) continue
          for (const v of item.values) {
            const ts = parseInt(v.timestamp, 10)
            const val = parseFloat(v.value)
            if (!Number.isFinite(val)) continue
            const date = new Date(ts * 1000)
            const key = `${date.getMonth() + 1}/${date.getDate()}`
            dayCosts[key] = (dayCosts[key] || 0) + val
          }
        }

        if (!cancelled) {
          const today = new Date()
          const history: { date: string; cost: number }[] = []
          for (let i = 6; i >= 0; i--) {
            const d = new Date(today)
            d.setDate(d.getDate() - i)
            const key = `${d.getMonth() + 1}/${d.getDate()}`
            history.push({
              date: key,
              cost: Math.round((dayCosts[key] || 0) * 100) / 100,
            })
          }
          setCostHistory(history)
        }
      } catch {
        // Prometheus may not be available; fall back to empty
      }
    }
```

- [ ] **Step 2: Verify the frontend builds**

Run: `cd control-panel && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/pages/Costs.tsx
git commit -m "fix(costs): trend uses increase() per-hour cost, aggregated per day

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

### Task 6: Broader backend verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend suite**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`
Expected: all PASS.

- [ ] **Step 2: If any test fails, fix the regression**

Investigate the failing test. If it is a real regression from Tasks 1-3, fix the offending change and re-run. If it is a pre-existing failure unrelated to this work, note it and do not block on it.

- [ ] **Step 3: Confirm frontend build is green**

Run: `cd control-panel && npm run build`
Expected: build succeeds.

---

### Task 7: Docs — DB_SCHEMA.md + CLAUDE.md (original plan Task 17)

**Files:**
- Modify: `docs/DB_SCHEMA.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `docs/DB_SCHEMA.md`**

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

Also update the §3 cache-key v2 description to drop `tenant_id` and describe the three scope tiers (public / group `g={group_id}` / private `u={user_id}`), with default scope `group`.

- [ ] **Step 2: Update `CLAUDE.md`**

First check length:

```bash
wc -l CLAUDE.md
```

If over ~300 lines (or this addition would push it over), prune first per CLAUDE.md workflow rule 4 (collapse old Known-States entries, drop duplicates, prefer English). Then:

In the "Security & Quotas" section, add:

```markdown
`GroupStore` - Redis hash per group (`aigateway:group:{group_id}`), isomorphic to KeyStore. Group-level quotas (daily tokens/monthly cost/RPM/TPM) checked first, then key-level; both incremented per request. `group_id` persisted on each key; `cache_scope` (private/group/public) per key. System default group `grp-default` absorbs pre-existing groupless keys at startup. `assign_key_to_group` moves only the selected key's personal usage between groups.
```

In "Cache Key v2", replace the `tenant_id` line with the three scope tiers (public / group `g={group_id}` / private `u={user_id}`), default `group`.

- [ ] **Step 3: Commit**

```bash
git add docs/DB_SCHEMA.md CLAUDE.md
git commit -m "docs: user groups + cache scope tiers in DB_SCHEMA and CLAUDE.md

Co-Authored-By: CodeBuddy Opus 4.8 <noreply@Tencent.com>"
```

---

## Final Verification

- [ ] **Full backend test suite:** `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py` — all pass.
- [ ] **Frontend build:** `cd control-panel && npm run build` — pass.
- [ ] **Docker rebuild + health (only if a backend image-layer change was made):** `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway && curl -sf localhost:8000/health` — OK; `docker compose logs --tail=50 gateway | grep -i error` — clean.
- [ ] **Manual sanity (optional):** Quotas page 用户组 Tab creates a group; creating a key assigned to it lists `group_name` and `cache_scope`; assigning a key between two populated groups leaves the source group's other-member usage intact.
