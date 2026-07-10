# Review fixes + Task 17 Design

## Goal
Bring the current user-groups and cost-charts work up to code-review quality by applying the minimum correct fixes for confirmed review findings, adding targeted regression coverage, verifying backend/frontend behavior, and only then executing Task 17 from `docs/superpowers/plans/2026-07-09-user-groups-and-cost-charts.md`.

## Scope
This design intentionally limits changes to the confirmed review findings:

1. Fix backend correctness bugs in `KeyStore` and `GroupStore`
2. Complete the `/admin/api-keys` response contract for new group/cache fields
3. Fix frontend API base-path handling for Prometheus queries
4. Fix the cost trend chart to use real interval cost semantics
5. Add targeted regression tests for the repaired paths
6. After all code and verification pass, execute Task 17 docs updates

Non-goals:
- broad refactoring
- unrelated cleanup
- introducing new frameworks or new testing infrastructure

## Recommended Approach
Use the minimum-correctness path:
- patch only the confirmed defects
- add regression tests that would have caught each defect class
- run targeted verification first, then broader verification
- defer docs changes until code is stable

This is preferred over opportunistic refactoring because the user’s priority is passing a high-standard review with the smallest safe diff.

## Design

### 1. Backend correctness fixes

#### `aigateway-core/src/aigateway_core/shared/auth/key_store.py`
- Repair `create()` and `seed_from_config()` so `pipe_batch` is invoked with commands built inside `lambda p: [...]` rather than referencing an undefined `p` outside the callback.
- Repair `migrate_groups()` so the key record update and destination group member-set update are written together atomically. This avoids half-migrated states where `group_id` is set on the key but membership bookkeeping is missing.

#### `aigateway-core/src/aigateway_core/shared/auth/group_store.py`
- Repair `assign_key_to_group()` so it migrates only the moved key’s personal usage (`daily_tokens_used`, `monthly_cost_used`) between groups.
- It must no longer zero the entire source group’s aggregate usage and move that whole aggregate to the destination.
- Preserve the current atomic batch structure for group records, member sets, and key record updates.

### 2. Admin API response contract fixes

#### `aigateway-api/src/aigateway_api/admin_routes.py`
- Extend `_format_quota_item()` to include:
  - `group_id`
  - `cache_scope`
  - `group_name`
- `group_name` should be resolved on the backend so the frontend receives a complete DTO instead of inferring names client-side.

### 3. Frontend correctness fixes

#### `control-panel/src/api/client.ts`
- Change `metricsQuery()` to use `${API_BASE}/admin/metrics/query_range` instead of a hard-coded `/aigateway/...` path.
- This keeps behavior consistent with the rest of the client and preserves deployment portability.

#### `control-panel/src/pages/Costs.tsx`
- Replace the current trend query that treats `rate(gateway_cost_total[1h])` as a bucket cost.
- Use interval-cost semantics instead, preferably `increase(gateway_cost_total[1h])`, then aggregate hourly results into per-day totals for the 7-day chart.
- The rendered chart must represent actual cost accumulated in each interval/day, not cost-per-second.

### 4. Test strategy

Add or extend regression coverage for the repaired paths:

#### Backend tests
- key creation with `group_id` and `cache_scope` succeeds through the new `pipe_batch` path
- config seeding succeeds through the new `pipe_batch` path
- moving one key out of a multi-member source group transfers only that key’s usage
- group migration updates key record and membership bookkeeping consistently
- if practical within existing test structure, `/admin/api-keys` list responses include `group_id`, `group_name`, and `cache_scope`

#### Frontend verification
- At minimum, require a successful `npm run build`
- If there is already suitable frontend test scaffolding, add the smallest useful coverage for the cost trend mapping or API-base-path behavior
- Do not introduce a new frontend test framework solely for this task

## Verification Plan

### Targeted verification
Run the most relevant backend tests first:
- `tests/test_group_store.py`
- `tests/test_group_quota.py`
- `tests/test_cache_key_v2.py`
- any new focused backend test file added for admin route contract coverage

### Broader verification
Then run the repo-standard backend suite:
- `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py`

### Frontend verification
- `cd control-panel && npm run build`

## Acceptance Criteria
The work is complete when all of the following are true:
- `KeyStore.create()` and `seed_from_config()` no longer crash from invalid `pipe_batch` usage
- `assign_key_to_group()` preserves source-group usage for remaining members and moves only the selected key’s usage
- `migrate_groups()` no longer leaves key/group membership in a half-updated state
- `/admin/api-keys` returns the group/cache fields already consumed by the frontend
- `metricsQuery()` no longer hardcodes the `/aigateway` path
- the Costs trend uses real interval cost semantics rather than raw rate values
- targeted regression tests pass
- the broader backend test suite passes
- the frontend build passes

## Task 17 sequencing
Only after the code fixes and verification are complete, execute Task 17 from `docs/superpowers/plans/2026-07-09-user-groups-and-cost-charts.md`:
- update `docs/DB_SCHEMA.md`
- update `CLAUDE.md`

The docs should reflect the final repaired implementation, not an intermediate state.
