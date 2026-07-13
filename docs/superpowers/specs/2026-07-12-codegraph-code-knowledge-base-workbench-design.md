# CodeGraph-native Code Knowledge Base Workbench Design

Date: 2026-07-12  
Status: Proposed

## 1. Overview

This design upgrades the current Code RAG subsystem from an import-only v1 into a CodeGraph-native code knowledge base workbench. The current system supports asynchronous imports from `folder`, `server_path`, `git`, and `zip`, stores code embeddings in `rag_code_*` Qdrant collections, and builds a CodeGraph SQLite database per import. However, it still behaves like a one-shot importer: repeated imports create duplicate repositories, there is no stable repository identity, no sync/reindex lifecycle, and the Control Panel exposes only import/task/list/delete flows.

The redesigned system will use the existing CodeGraph TypeScript API as the primary graph/index backend, rather than expanding the current Python-only wrapper or proxying CLI commands. A new Node sidecar service (`codegraph-service`) will expose CodeGraph-backed repository lifecycle and graph query APIs to the existing Python backend. The Python backend will remain the orchestration layer for source management, repository catalog, operation tracking, embeddings/Qdrant integration, and Control Panel APIs.

The MVP will deliver a full workbench experience: repository management, sync/reindex, file browsing, symbol search, callers/callees/impact analysis, operation reporting, semantic code search, and managed repository watcher controls.

## 2. Goals

### 2.1 Product goals

一期交付一个可用的代码知识库工作台，而不是单纯补几个接口。 Users should be able to:

- import and manage code repositories
- maintain stable repository identity across repeated operations
- sync or reindex repositories depending on source type
- browse files and inspect graph-backed repository status
- search symbols and inspect callers/callees/impact
- run semantic code search against the existing Qdrant-backed code index
- monitor operations and errors from the Control Panel
- enable and disable graph watchers for managed repositories

### 2.2 Technical goals

- Reuse CodeGraph’s existing API capabilities instead of rebuilding graph/query features
- Keep the current Python/FastAPI architecture as the admin/orchestration layer
- Preserve compatibility with existing Code RAG retrieval in `rag_retriever_plugin.py`
- Add a stable, structured repository catalog and operation model
- Keep the MVP scope controlled by separating graph freshness from vector freshness

## 3. Non-goals

The MVP explicitly does not include:

- import dry-run / preview flows
- CLI-based CodeGraph integration as the primary runtime path
- custom graph query DSL or custom graph database features beyond CodeGraph
- advanced graph visualization beyond list/tree views
- automatic git remote polling/pull in watcher mode
- automatic embedding/Qdrant refresh triggered by every watcher event
- private git credential management beyond current public/accessible source assumptions
- deep retrieval-quality work such as hybrid rerank redesign, judge models, or major graph SQL rewrites
- CI / affected-tests workflow integration in the first implementation

## 4. Design principles

1. **CodeGraph-native** — Use CodeGraph’s published TypeScript API directly for indexing, syncing, search, graph traversal, and watcher control.
2. **No wheel reinvention** — Do not rebuild symbol search, callers/callees, impact, or status logic in Python if CodeGraph already provides it.
3. **Clear ownership split** — Python owns source ingestion, repo metadata, operations, Qdrant, and admin APIs; Node owns CodeGraph runtime interactions.
4. **Managed vs snapshot sources** — Only sources with stable upstream semantics get sync/watch capabilities.
5. **Controlled freshness semantics** — Watchers keep the CodeGraph graph fresh; sync/reindex keeps both graph and vector index fresh.
6. **Incremental adoption** — Existing retrieval paths remain valid during the migration.

## 5. Source model

### 5.1 Managed repositories

Managed repositories support long-lived lifecycle operations.

Supported source types:

- `git`
- `server_path`

Capabilities:

- import
- sync
- reindex
- status
- files browsing
- symbol search
- callers/callees/impact
- watch/unwatch
- delete

### 5.2 Snapshot repositories

Snapshot repositories represent imported snapshots, not continuously managed upstreams.

Supported source types:

- `folder`
- `zip`

Capabilities:

- import
- reindex
- status
- files browsing
- symbol search
- callers/callees/impact
- delete

Not supported:

- sync
- watch/unwatch

This split keeps the MVP bounded while still supporting all four current import modes.

## 6. Architecture

### 6.1 Top-level architecture

The system will be split into three cooperating layers:

1. **Control Panel workbench**  
   React UI for repository management, file browsing, graph analysis, semantic search, and operations.

2. **Python admin/orchestration layer**  
   Existing FastAPI + core services continue to own source validation, upload handling, repository catalog, operation tracking, chunking/embedding/Qdrant integration, and public admin APIs.

3. **Node `codegraph-service` sidecar**  
   A new local service that wraps the CodeGraph TypeScript API and exposes repository graph lifecycle and query endpoints to the Python backend.

### 6.2 Why a sidecar is required

CodeGraph’s published runtime API is TypeScript/Node-native. The existing gateway backend is Python/FastAPI. Embedding CodeGraph indirectly through ad-hoc subprocess-based Node invocations would complicate error handling, session management, and watcher lifecycle. A dedicated sidecar keeps the integration explicit and maintainable:

- the Node runtime boundary is isolated
- watcher process/session state lives in one place
- Python interacts through a small stable client
- future API additions such as `buildContext()` or `watch()` do not require redesigning the orchestration layer

## 7. Repository model

### 7.1 Repository entity

Each logical code repository will be represented by a structured repository record.

Required fields:

- `repo_id`
- `display_name`
- `source_type`
- `source_ref`
- `source_fingerprint`
- `branch` (git only)
- `snapshot_only`
- `status`
- `embedding_model`
- `qdrant_collection`
- `workspace_path`
- `graph_db_path`
- `last_commit` (git if available)
- `file_count`
- `function_count`
- `class_count`
- `chunk_count`
- `language_summary`
- `created_at`
- `updated_at`
- `last_successful_sync_at`
- `last_error`
- `watch_enabled`
- `watch_status`
- `watch_started_at`
- `last_watch_event_at`
- `last_watch_success_at`
- `last_watch_error`

### 7.2 Fingerprint rules

Repository identity will be based on normalized source fingerprints.

- git: `git::<normalized_url>::<branch>`
- server_path: `server::<realpath>`
- folder: `folder::<upload_root>::<session_or_hash>`
- zip: `zip::<filename>::<hash>`

`git` and `server_path` fingerprints support dedupe and managed lifecycle operations. `folder` and `zip` fingerprints distinguish snapshots but do not imply long-lived upstream equivalence.

## 8. Operation model

Every lifecycle action will create an operation record.

Fields:

- `operation_id`
- `repo_id`
- `type` = `import | sync | reindex | delete | watch_start | watch_stop`
- `status`
- `started_at`
- `finished_at`
- `duration_ms`
- `current_step`
- `current_file`
- `summary`
- `error_message`
- `stats`
- `artifacts`

Operations unify asynchronous task progress, reporting, and future auditability.

## 9. Storage model

The MVP will continue to use Redis, but repository state will move from an append-only list into structured keys.

### 9.1 Repository catalog

- `aigateway:rag:code:repo:{repo_id}`
- `aigateway:rag:code:repos`
- `aigateway:rag:code:fingerprint:{fingerprint}` -> `repo_id`

### 9.2 Operations

- `aigateway:rag:code:op:{operation_id}`
- `aigateway:rag:code:repo_ops:{repo_id}`

### 9.3 Workspace layout

A stable workspace root will be introduced:

- `/data/code_repos/{repo_id}/source`
- `/data/code_repos/{repo_id}/graph`
- `/data/code_repos/{repo_id}/artifacts`

This replaces the current one-shot temp-only mindset and gives sync/reindex/watch a stable filesystem home.

## 10. CodeGraph sidecar design

### 10.1 Runtime

New service:

- directory: `codegraph-service/`
- runtime: Node 22.5+
- language: TypeScript
- dependency: `@colbymchenry/codegraph`

### 10.2 Responsibilities

The sidecar will be the only component that directly calls the CodeGraph TypeScript API.

It will expose operations backed by:

- `init(path)`
- `open(path)`
- `close()`
- `sync()`
- `indexAll(opts)`
- `searchNodes(query)`
- `getCallers(id)`
- `getCallees(id)`
- `getImpactRadius(id, depth)`
- `watch()`
- `unwatch()`
- `buildContext(task, opts)` (reserved for future integration)

### 10.3 Sidecar API surface

Initial endpoints:

- `POST /repos/init`
- `POST /repos/sync`
- `POST /repos/reindex`
- `GET /repos/status`
- `GET /repos/files`
- `GET /repos/search`
- `GET /repos/callers`
- `GET /repos/callees`
- `GET /repos/impact`
- `POST /repos/watch`
- `POST /repos/unwatch`
- `GET /repos/watch-status`

Reserved for later but designed in:

- `POST /repos/build-context`

### 10.4 Sidecar state

The sidecar will maintain a watcher/session registry keyed by repository path or `repo_id`, containing:

- active watch state
- start time
- last event time
- last success time
- last error

Service shutdown must close open graph sessions and stop watchers cleanly.

## 11. Python backend changes

### 11.1 New core services

Add the following Python services:

- `repository_service.py` — repository CRUD, fingerprint logic, capability checks, workspace allocation
- `operation_service.py` — operation lifecycle, progress updates, reports, cancellation hooks
- `repository_orchestrator.py` — import/sync/reindex/delete/watch orchestration
- `codegraph_client.py` — HTTP client for `codegraph-service`

### 11.2 API layer

The current `code_rag_routes.py` will be expanded or split, but route responsibilities should separate into:

- repository lifecycle routes
- CodeGraph-backed workbench routes
- operation/report routes
- semantic code query routes

## 12. Admin API design

### 12.1 Repository lifecycle APIs

- `POST /admin/rag/code/import`
- `POST /admin/rag/code/repositories/{repo_id}/sync`
- `POST /admin/rag/code/repositories/{repo_id}/reindex`
- `DELETE /admin/rag/code/repositories/{repo_id}`
- `GET /admin/rag/code/repositories`
- `GET /admin/rag/code/repositories/{repo_id}`

### 12.2 CodeGraph-backed workbench APIs

- `GET /admin/rag/code/repositories/{repo_id}/status`
- `GET /admin/rag/code/repositories/{repo_id}/files`
- `GET /admin/rag/code/repositories/{repo_id}/symbols?q=...`
- `GET /admin/rag/code/repositories/{repo_id}/callers?node_id=...`
- `GET /admin/rag/code/repositories/{repo_id}/callees?node_id=...`
- `GET /admin/rag/code/repositories/{repo_id}/impact?node_id=...&depth=...`

### 12.3 Watcher APIs

- `POST /admin/rag/code/repositories/{repo_id}/watch/start`
- `POST /admin/rag/code/repositories/{repo_id}/watch/stop`
- `GET /admin/rag/code/repositories/{repo_id}/watch`

### 12.4 Semantic code query API

Retain and evolve a unified semantic code search endpoint:

- `POST /admin/rag/code/query`

This endpoint remains Qdrant/code-retrieval-backed and serves a different purpose than CodeGraph symbol/node search. It is used for natural-language semantic code lookup and returns:

- semantic hits
- graph-expanded hits
- diagnostics

### 12.5 Operation APIs

- `GET /admin/rag/code/operations`
- `GET /admin/rag/code/operations/{operation_id}`
- `GET /admin/rag/code/operations/{operation_id}/report`
- `POST /admin/rag/code/operations/{operation_id}/cancel`

## 13. Repository lifecycle flows

### 13.1 Import flow

1. Validate source input and capability constraints.
2. Compute source fingerprint.
3. Reject duplicate managed repository imports that collide with an existing managed repo unless a future explicit overwrite mode is added.
4. Create repository record and operation record.
5. Materialize source into stable workspace.
6. Call `codegraph-service` `init` for the workspace.
7. Run code splitting / embedding / Qdrant upsert.
8. Update repository stats and graph metadata.
9. Persist operation report.

### 13.2 Sync flow

Applicable only to `git` and `server_path`.

1. Load repo and validate managed capabilities.
2. Refresh source:
   - git: fetch/pull into local workspace
   - server_path: rescan existing path
3. Call `codegraph-service` `sync`.
4. Refresh changed chunks/vectors using the existing Python pipeline.
5. Update repository metadata and operation report.

### 13.3 Reindex flow

Applicable to all source types.

1. Load repo.
2. Call `codegraph-service` `reindex`.
3. Re-run full code splitting / embedding / Qdrant rebuild for the repo.
4. Update repository stats.
5. Persist operation report.

### 13.4 Delete flow

1. Stop watcher if running.
2. Delete repository metadata.
3. Remove workspace and graph artifacts.
4. Delete Qdrant points for the repo.
5. Mark operation outcome.

## 14. Watcher design

### 14.1 Scope

Watcher support is part of the MVP, but only for managed repositories:

- supported: `git`, `server_path`
- unsupported: `folder`, `zip`

### 14.2 Semantics

Watcher support keeps the **CodeGraph graph fresh**, not the full vector index fresh.

This distinction is deliberate:

- `watch()` updates CodeGraph’s internal graph/index state for local filesystem changes
- `sync` and `reindex` remain the operations that bring Python-side chunk/vector/Qdrant state back into alignment

### 14.3 Source-type behavior

For `server_path`, watcher directly tracks local directory changes.

For `git`, watcher tracks the local checked-out workspace only. It does **not** pull remote changes. Users must still run sync to fetch/pull upstream updates.

### 14.4 Watcher data model

Repository metadata tracks:

- whether watching is enabled
- current watch status: `running | stopped | failed`
- last watch activity timestamps
- last watch error

### 14.5 Operational behavior

Watcher start/stop operations are tracked like other repository operations. Repo deletion must automatically stop watching. Sidecar shutdown must stop all watchers cleanly.

## 15. Frontend workbench design

The existing `KnowledgeCodeTab.tsx` will be split into focused panels/components.

### 15.1 Repositories panel

Displays:

- repo name
- source type
- branch
- status
- watcher status
- last sync
- stats summary
- actions: sync, reindex, start watch, stop watch, view status, browse files, search, delete

### 15.2 Semantic search panel

Backed by `POST /admin/rag/code/query`.

Used for natural-language semantic lookup over the code knowledge base. Includes filters such as repo selection and graph expansion controls if supported by the existing retrieval API.

### 15.3 Graph tools panel

Backed by CodeGraph APIs. Supports:

- symbol search
- callers
- callees
- impact

Initial presentation is list/tree oriented rather than advanced graph visualization.

### 15.4 Files panel

Allows browsing repository file structure and drilling into graph-backed file information.

### 15.5 Operations panel

Shows:

- running operations
- historical operations
- errors
- summary stats

### 15.6 Repo detail drawer/page

Displays repository metadata, graph status, watcher state, recent operations, and quick actions.

### 15.7 Watcher UX requirements

The frontend must expose watcher controls and make the semantics explicit.

For git repositories, the UI should explain that watch keeps the **local** CodeGraph graph fresh but does not pull remote updates. Sync remains necessary.

For server_path repositories, the UI should explain that watch tracks local directory changes directly.

## 16. Existing retrieval compatibility

### 16.1 Preserve current retrieval path

The current Qdrant + graph-expansion flow in `rag_retriever_plugin.py` remains in place for the MVP.

### 16.2 Required compatibility work

- align graph/workspace path resolution with the new stable repository layout
- evolve metadata assumptions from one-shot document imports toward stable repository records
- preserve tolerant graph lookup behavior during migration

### 16.3 Future evolution path

The sidecar API leaves room for future use of CodeGraph-native APIs such as `buildContext()` and richer node search, but these are not required to ship the MVP.

## 17. Configuration changes

### 17.1 New CodeGraph service config

Add a new config block, for example:

```yaml
codegraph_service:
  enabled: true
  base_url: http://codegraph-service:PORT
  timeout_seconds: 30
  watch_enabled: true
```

### 17.2 Workspace root config

Extend Code RAG config with a stable workspace root:

```yaml
code_rag:
  workspace_root: /data/code_repos
```

The config template must be updated alongside runtime code.

## 18. Error handling

### 18.1 Sidecar failures

If the sidecar returns transport or runtime errors, Python APIs must surface actionable operation errors and preserve last-known repo state rather than silently corrupting metadata.

### 18.2 Watcher failures

Watcher failures must update `watch_status` and `last_watch_error`, and must be visible in the Control Panel.

### 18.3 Partial lifecycle failures

If graph init/sync succeeds but embedding/Qdrant refresh fails, repository status must reflect that the repo is not fully healthy. Error reporting must distinguish graph freshness from vector freshness.

## 19. Testing strategy

### 19.1 Backend tests

- repository fingerprint and capability tests
- import/sync/reindex route tests
- codegraph client error handling tests
- operation/report lifecycle tests
- watcher state transition tests

### 19.2 Sidecar tests

- route contract tests
- CodeGraph API wrapper tests against fixture repos
- watcher registry tests
- shutdown cleanup tests

### 19.3 Frontend tests

- repository list/actions
- watcher controls
- graph tools display
- operation panel error/success flows

### 19.4 End-to-end tests

Add at least one end-to-end flow that proves:

- import managed repo
- view status/files/symbol search
- start watch
- stop watch
- sync and see updated state

## 20. Implementation phases

### Phase 1 — foundations

- introduce repository catalog and operation model
- add stable workspace layout
- scaffold `codegraph-service`
- implement Python `codegraph_client`

### Phase 2 — lifecycle integration

- wire import/sync/reindex/delete flows to sidecar
- migrate graph path handling
- persist repo/watch state

### Phase 3 — workbench APIs and UI

- repository list/detail
- files/status/symbol/callers/callees/impact APIs
- Control Panel workbench panels
- watcher controls in UI

### Phase 4 — hardening

- improve reports/errors
- add tests and docs
- cleanly retire one-shot assumptions where safe

## 21. Success criteria

The MVP is successful when:

1. `git` and `server_path` repositories can be imported and synced with stable identity.
2. `folder` and `zip` repositories can be imported and reindexed as snapshots.
3. The Control Panel exposes repository status, files, symbol search, callers, callees, and impact.
4. Managed repositories support start/stop watch from the UI and backend.
5. Semantic code search continues to work through the existing Qdrant-backed path.
6. Every operation has visible status and usable error reporting.
7. Existing chat retrieval behavior does not regress.

## 22. Risks and mitigations

### Risk: sidecar/API contract drift
Mitigation: define a narrow HTTP contract, add route contract tests, and keep Python-side normalization minimal.

### Risk: watcher/resource leaks
Mitigation: maintain explicit watcher registry, stop watchers on repo delete and service shutdown, and expose watch state visibly.

### Risk: graph and vector freshness diverge
Mitigation: make the distinction explicit in status reporting and UI messaging; keep sync/reindex as the vector freshness boundary.

### Risk: scope creep into retrieval redesign
Mitigation: keep semantic retrieval changes out of MVP unless required for compatibility.
