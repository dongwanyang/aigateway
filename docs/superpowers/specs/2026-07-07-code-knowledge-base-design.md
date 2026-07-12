# Code Knowledge Base Design

Date: 2026-07-07
Status: Draft approved in brainstorming

## Summary

Add a dedicated **Code** tab to the Control Panel Knowledge page so users can import codebases and build a code-aware knowledge base. The implementation must use the packages and chain referenced in `AST.txt` instead of re-implementing parsing logic: LangChain's `GenericLoader`, `LanguageParser`, and `RecursiveCharacterTextSplitter.from_language` for AST-aware chunking, plus CodeGraph for call-graph indexing/query. The exact import path/package split must match the versions actually installable in this repo.

The feature is intentionally separated from the existing text/web-document RAG path. Text knowledge (`/admin/rag/documents`, `rag_documents`) stays unchanged. Code knowledge gets its own API routes, async import task flow, per-embedding-model Qdrant collections, and graph-enhanced retrieval.

## Goals

1. Add a **Code** tab in `control-panel/src/pages/Knowledge.tsx`.
2. Support four code import sources:
   - drag-and-drop folder
   - server-side repository path
   - Git repository URL
   - ZIP upload
3. Build a code knowledge base using the approach described in `AST.txt`:
   - AST-aware chunking via `LanguageParser`
   - line-aware metadata (`start_line`, `end_line`)
   - call graph extraction via CodeGraph
   - graph-enhanced retrieval using callers/callees hops
4. Allow the user to specify the embedding model at import time.
5. Store code vectors in **separate Qdrant collections per embedding model** to avoid vector-dimension conflicts.
6. Keep the existing text knowledge base behavior intact.

## Non-goals

1. Do not merge code and text import flows into one implementation path.
2. Do not silently degrade to plain-text splitting when code graph generation fails.
3. Do not support private Git auth, SSH cloning, or submodules in phase 1.
4. Do not redesign the existing text knowledge table or text import flow.

## User-confirmed decisions

1. Use **Approach A**: build a dedicated code RAG subsystem instead of extending the current text RAG endpoint in place.
2. First functional scope must include:
   - AST-aware chunking
   - basic metadata
   - call graph / callers / callees / imports
   - graph-enhanced retrieval at query time
3. Use packages explicitly referenced by `AST.txt`:
   - LangChain `GenericLoader` + `LanguageParser` + `RecursiveCharacterTextSplitter.from_language`
   - CodeGraph
   - do not hand-roll tree-sitter chunking / graph logic
4. Embedding model is user-provided at import time.
5. Qdrant storage must be **per embedding model collection** instead of forcing all models into one 1024-d collection.
6. Import flow must be **async with progress polling**.
7. Code knowledge must be separated from text knowledge in the UI via a dedicated tab.
8. Payload must include `start_line` / `end_line`.
9. `server_path` must be constrained by an allowlist.
10. CodeGraph failure is a **hard failure** for the import task.

## Existing system context

### Current text knowledge base

The current system already has:

- `GET /admin/rag/documents`
- `POST /admin/rag/documents`
- `DELETE /admin/rag/documents/{doc_id}`
- Qdrant collection `rag_documents`
- local sentence-transformers embedding using `Qwen/Qwen3-Embedding-0.6B`
- `control-panel/src/pages/Knowledge.tsx` with URL import and file upload UI

Current text import uses character/paragraph splitting in `aigateway-api/src/aigateway_api/admin_routes.py::_split_text` and has no code-structure awareness.

### Current retrieval path

`aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py` currently uses a single configured collection name (`rag_documents`) and injects hits into the request as a system prefix. It must be extended to query code collections in parallel and enrich code hits through graph hops.

## Architecture

### High-level layout

```text
Control Panel Knowledge page
  ├─ Text tab (existing, unchanged)
  └─ Code tab (new)
       ├─ import source selector
       ├─ embedding model input
       ├─ async import progress
       └─ code repository list

API layer
  ├─ admin_routes.py                existing text knowledge routes
  └─ code_rag_routes.py             new code knowledge routes

Core layer
  ├─ plugins/rag_retriever_plugin.py   existing retrieval plugin, extended
  └─ code_rag/
       ├─ splitter.py
       ├─ graph_builder.py
       ├─ graph_query.py
       └─ embedding_router.py
```

### Design boundary

The code knowledge base is a separate subsystem:

- separate routes
- separate Redis keys for tasks/documents
- separate Qdrant collections (`rag_code_<model_slug>`)
- separate graph files (`/data/code_graphs/<document_id>.db`)
- retrieval integration only at the final retrieval stage

This keeps text RAG stable while allowing code-specific ingestion and graph semantics.

## Frontend design

### Knowledge page structure

Add a `Code` tab beside the existing `Text` tab.

The `Code` tab contains:

1. **Import source selector**
   - drag-and-drop folder
   - server path
   - Git URL
   - ZIP upload
2. **Shared config section**
   - embedding model input (free text)
   - quick model presets dropdown
3. **Task progress section**
   - status
   - current phase
   - current file
   - completed/total files
   - error details if failed
4. **Repository list**
   - list is per imported repository/codebase, not per file

### Repository list model

The list item is an imported codebase, not a single file.

```ts
interface CodeRepositoryImport {
  document_id: string
  source_type: 'folder' | 'server_path' | 'git' | 'zip'
  source_label: string
  file_count: number
  language_summary: string[]
  function_count: number
  class_count: number
  chunk_count: number
  embedding_model: string
  import_time: string
}
```

A later detail view may expand repository → files → symbols, but that is not required for this first implementation.

## API design

Create `aigateway-api/src/aigateway_api/code_rag_routes.py` with four endpoints.

### 1. POST `/admin/rag/code/import`

Creates an async import task and immediately returns `task_id`.

Supported request modes:

- `source_type=folder` via multipart upload
- `source_type=zip` via multipart upload
- `source_type=server_path` via JSON
- `source_type=git` via JSON

Example JSON body:

```json
{
  "source_type": "git",
  "git_url": "https://github.com/org/repo",
  "git_branch": "main",
  "embedding_model": "Qwen/Qwen3-Embedding-0.6B"
}
```

Example response:

```json
{
  "task_id": "uuid",
  "status": "pending"
}
```

### 2. GET `/admin/rag/code/tasks/{task_id}`

Returns current task progress.

```json
{
  "task_id": "uuid",
  "status": "pending | scanning | splitting | building_graph | embedding | completed | failed",
  "current_file": "control-panel/src/pages/Knowledge.tsx",
  "done": 12,
  "total": 86,
  "error": null
}
```

### 3. GET `/admin/rag/code/repositories`

Lists imported code repositories.

### 4. DELETE `/admin/rag/code/repositories/{document_id}`

Deletes:
- all Qdrant points matching `document_id` across all code collections
- repository metadata in Redis
- CodeGraph DB file `/data/code_graphs/<document_id>.db`

## Import data flow

### Async task flow

```text
POST /admin/rag/code/import
  → create task_id
  → store Redis task state
  → schedule background task
  → return task_id immediately

Background task:
  1. materialize source into a local temp directory
  2. scan files and apply ignore patterns / limits
  3. use LanguageParser + RecursiveCharacterTextSplitter.from_language
  4. build CodeGraph SQLite DB
  5. query graph metadata for chunks
  6. route embedding model → collection name + dimension
  7. batch-encode chunks in executor
  8. upsert into Qdrant
  9. write repository metadata
 10. mark task complete
```

### Source materialization rules

- **folder**: upload multiple files while preserving relative paths in a temp directory (from drag-and-drop folder or folder-picker selection)
- **zip**: unzip into temp directory after zip-slip / size checks
- **server_path**: validate allowlisted realpath, then read directly
- **git**: shallow clone (`--depth=1`) into temp directory

## Chunking and metadata

### Chunking strategy

Use the AST-aware chain described in `AST.txt`:

1. `GenericLoader.from_filesystem(...)`
2. `LanguageParser(...)`
3. `RecursiveCharacterTextSplitter.from_language(...)`

This is the required implementation path. Do not replace it with hand-written tree-sitter splitting.

### Per-chunk payload

Each Qdrant point payload includes:

```json
{
  "document_id": "uuid",
  "filename": "auth.py",
  "file_path": "core/auth.py",
  "language": "python",
  "chunk_index": 3,
  "chunk_text": "...",
  "chunk_type": "function | class | module",
  "function_name": "login",
  "class_name": "AuthService",
  "start_line": 42,
  "end_line": 67,
  "callers": ["register", "validate"],
  "callees": ["hash_password", "jwt_encode"],
  "imports": ["jwt", "bcrypt"],
  "embedding_model": "Qwen/Qwen3-Embedding-0.6B"
}
```

### Line number handling

`start_line` / `end_line` are required.

Resolution strategy:
1. prefer metadata emitted by the parsing / splitting chain
2. if unavailable, reconstruct line span from the chunk text against the source file

## Graph indexing design

### CodeGraph storage

Store one graph DB per imported code repository:

```text
/data/code_graphs/<document_id>.db
```

This makes deletion simple and avoids coupling unrelated repositories into one giant graph store.

### Graph metadata usage

During import:
- query CodeGraph for per-symbol callers / callees / imports
- merge results into Qdrant payload

During retrieval:
- retrieve primary vector hits from code collections
- for symbol hits, query CodeGraph for adjacent symbols
- fetch related chunks through payload filters / secondary lookup
- deduplicate and merge them into retrieval context

Graph-enhanced retrieval is part of phase 1 and is not optional.

## Embedding model and collection routing

### Why per-model collections are required

Qdrant vector dimension is fixed per collection. Since users may import code using different embedding models with different output dimensions, code vectors cannot share a single collection.

### Collection naming

```text
rag_code_<model_slug>
```

Examples:
- `rag_code_qwen3_0_6b`
- `rag_code_text_embedding_3_large`
- `rag_code_bge_small_en_v1_5`

### Dimension probing

Before creating a collection, encode a probe text once and measure the vector length. Cache this dimension per model.

Then create or validate the collection using that measured dimension.

## Retrieval design

Extend `rag_retriever_plugin.py` with code-RAG support.

### Retrieval sequence

1. query the existing text collection (`rag_documents`) as before
2. if `code_rag_enabled=true`, enumerate all `rag_code_*` collections
3. query those code collections in parallel
4. collect primary code hits
5. run graph hop expansion using callers/callees up to configured depth
6. deduplicate text hits + code hits
7. rank and inject into the system message

### New retrieval config

Add fields under `rag_retriever.config`:

```yaml
code_rag_enabled: true
code_rag_graph_hops: 2
code_rag_top_k: 5
```

### Failure behavior

Retrieval must be tolerant:
- one broken code collection must not break the request
- one missing graph DB must not break retrieval
- text retrieval remains usable even if code retrieval is partially degraded

## Redis design

```text
aigateway:rag:code:tasks:<task_id>   hash
aigateway:rag:code:documents         list of JSON metadata
```

### Task hash fields

- `status`
- `done`
- `total`
- `current_file`
- `error`
- `created_at`
- `updated_at`

Tasks may expire after 24h.

## Security constraints

### server_path allowlist

Add config:

```yaml
code_rag:
  enabled: true
  allowed_server_paths:
    - /home/ubuntu
    - /workspace
```

Behavior:
- canonicalize with `realpath`
- reject paths outside allowlist
- reject symlink escapes
- reject unreadable / nonexistent paths

### Git restrictions

Phase 1 supports only:
- public `https://` Git URLs
- shallow clone
- optional branch

Phase 1 does not support:
- SSH cloning
- private repo auth
- submodules

### Upload limits

Defaults:
- single file ≤ 5MB
- total upload ≤ 200MB
- max file count ≤ 5000

Ignore patterns:
- `.git`
- `node_modules`
- `__pycache__`
- `dist`
- `build`

ZIP handling must protect against zip-slip and decompression bombs.

## Failure policy

### Import-time behavior

- file parse failure: log warning, skip that file, continue task
- embedding model load failure: fail task
- dimension probe failure: fail task
- CodeGraph build failure: fail task
- Qdrant write failure: fail task and roll back points written for this `document_id`

### Retrieval-time behavior

- code collection query failure: warn and continue
- graph DB missing: warn and continue without hop expansion
- text retrieval path remains intact

This follows the confirmed rule: **strict on import, tolerant on retrieval**.

## Config changes

### New `code_rag` config section

```yaml
code_rag:
  enabled: true
  allowed_server_paths:
    - /home/ubuntu
    - /workspace
  max_file_size_mb: 5
  max_total_size_mb: 200
  max_file_count: 5000
  ignore_patterns:
    - node_modules
    - .git
    - __pycache__
    - dist
    - build
  graph_db_dir: /data/code_graphs
```

### Existing `rag_retriever` additions

```yaml
- name: rag_retriever
  enabled: true
  config:
    top_k: 5
    similarity_threshold: 0.7
    collection_name: rag_documents
    embedding_backend: local
    embedding_model: Qwen/Qwen3-Embedding-0.6B
    code_rag_enabled: true
    code_rag_graph_hops: 2
    code_rag_top_k: 5
```

`config.yaml.template` must be updated to include both schemas.

## Required file changes

### New files

- `aigateway-api/src/aigateway_api/code_rag_routes.py`
- `aigateway-core/src/aigateway_core/code_rag/__init__.py`
- `aigateway-core/src/aigateway_core/code_rag/splitter.py`
- `aigateway-core/src/aigateway_core/code_rag/graph_builder.py`
- `aigateway-core/src/aigateway_core/code_rag/graph_query.py`
- `aigateway-core/src/aigateway_core/code_rag/embedding_router.py`

### Existing files to modify

- `aigateway-api/requirements.txt`
- `aigateway-api/src/aigateway_api/main.py`
- `aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py`
- `config.yaml`
- `config.yaml.template`
- `docker-compose.yml`
- `control-panel/src/pages/Knowledge.tsx`
- `control-panel/src/api/client.ts`

## Testing strategy

### Backend

1. route tests for all four source types
2. allowlist validation tests for `server_path`
3. Qdrant collection creation tests with different dimensions
4. rollback test on partial Qdrant write failure
5. retrieval test: vector hit + graph hop expansion
6. line number metadata test (`start_line`, `end_line` present)

### Frontend

1. tab render test
2. import source switching behavior
3. progress polling behavior
4. error-state rendering
5. repository list rendering

### Manual validation

1. import current repo via `server_path=/home/ubuntu/aigateway`
2. verify graph DB is created
3. verify Qdrant `rag_code_*` collection exists with expected dimension
4. ask a code question that should require call-chain context
5. verify retrieved context includes adjacent callers/callees

## Open implementation notes

1. The exact PyPI package/API name for **CodeGraph** must be verified before implementation. `AST.txt` references the tool conceptually, but the concrete install/import path must be confirmed during planning.
2. The exact metadata emitted by `LanguageParser` for source locations must be verified. The spec already defines the fallback if line spans are not emitted directly.
3. For folder drag-and-drop in the browser, the final implementation may use `webkitdirectory` or drag APIs depending on the existing frontend pattern and browser support.

## Acceptance criteria

1. Control Panel Knowledge page shows a new **Code** tab.
2. User can import code using folder, server path, Git URL, or ZIP.
3. Import runs asynchronously and shows progress.
4. Code is chunked using the `AST.txt` parsing path, not plain text splitting.
5. Stored chunks include `start_line`, `end_line`, and graph metadata.
6. Different embedding models create/use separate code collections.
7. Query-time retrieval can expand around callers/callees.
8. Existing text knowledge base behavior remains unchanged.
9. `server_path` imports are restricted to configured allowlisted directories.
10. CodeGraph failure causes import failure instead of silent degradation.
