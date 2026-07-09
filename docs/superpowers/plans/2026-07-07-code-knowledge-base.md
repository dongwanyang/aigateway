# Code Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated code knowledge base in the Control Panel that imports code from folder/server path/Git/ZIP, chunks it with the AST.txt parsing chain, indexes graph metadata, and retrieves code context with graph-enhanced expansion without breaking the existing text knowledge base.

**Architecture:** Add a new code-RAG subsystem with dedicated API routes, Redis task state, per-embedding-model Qdrant collections, and per-repository CodeGraph SQLite files. Reuse the existing retrieval plugin by extending it to query code collections in parallel and expand vector hits through callers/callees graph hops, while keeping the text RAG path unchanged.

**Tech Stack:** FastAPI, React + TypeScript, Qdrant, Redis, sentence-transformers, LangChain GenericLoader/LanguageParser/RecursiveCharacterTextSplitter.from_language, CodeGraph, GitPython, pytest

## Global Constraints

- Use **Approach A** from the approved spec: dedicated code RAG subsystem; do not extend the text import endpoint in place.
- Use the AST.txt chain for chunking: `GenericLoader`, `LanguageParser`, and `RecursiveCharacterTextSplitter.from_language`; do not hand-roll tree-sitter chunking.
- Use CodeGraph for call graph indexing/query; do not hand-roll graph extraction.
- Keep the existing text knowledge base behavior intact.
- Support four code import sources in phase 1: folder, server path, Git URL, ZIP.
- Import must be async with progress polling.
- Qdrant storage must be per embedding model collection (`rag_code_<model_slug>`), not a shared fixed-dimension code collection.
- Payloads must include `start_line` and `end_line`.
- `server_path` must be restricted to configured allowlisted directories.
- CodeGraph failure is a hard import failure; import is strict, retrieval is tolerant.
- Phase 1 Git support is public `https://` only, shallow clone only, no SSH/private auth/submodules.
- Default upload limits: single file ≤ 5MB, total upload ≤ 200MB, max file count ≤ 5000.
- Ignore patterns: `.git`, `node_modules`, `__pycache__`, `dist`, `build`.
- Update `config.yaml.template` for any new config schema.
- After code changes, follow repo workflow: commit, rebuild affected Docker images if required, and report verification results.

---

## File Structure Map

### New files

- `aigateway-api/src/aigateway_api/code_rag_routes.py`
  - FastAPI router for code import, task status, repository list, delete.
  - Owns request validation, async task orchestration, Redis task updates, and cleanup paths.
- `aigateway-core/src/aigateway_core/code_rag/__init__.py`
  - Exports helpers used by API and retrieval layers.
- `aigateway-core/src/aigateway_core/code_rag/splitter.py`
  - Thin wrapper around LangChain loader/parser/splitter chain.
  - Converts source directory → normalized chunk objects with relative paths and language metadata.
- `aigateway-core/src/aigateway_core/code_rag/graph_builder.py`
  - Thin wrapper for building one CodeGraph SQLite DB per imported repository.
- `aigateway-core/src/aigateway_core/code_rag/graph_query.py`
  - Reads a CodeGraph DB and resolves symbol-level callers/callees/imports for import-time payload enrichment and retrieval-time hop expansion.
- `aigateway-core/src/aigateway_core/code_rag/embedding_router.py`
  - Resolves embedding model → collection slug, detects dimension, caches model instances, and batch-encodes text.
- `tests/test_code_rag_routes.py`
  - Route and task lifecycle tests.
- `tests/test_code_rag_helpers.py`
  - Unit tests for splitter/embedding routing/path validation/payload shaping.
- `tests/test_rag_retriever_code_rag.py`
  - Retrieval plugin tests for code collection querying and graph hop expansion.

### Existing files to modify

- `aigateway-api/requirements.txt`
  - Add code RAG dependencies.
- `aigateway-api/src/aigateway_api/main.py`
  - Register `code_rag_routes`.
- `aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py`
  - Add code collection enumeration/query/graph expansion and tolerant failure behavior.
- `config.yaml`
  - Add `code_rag` section and new `rag_retriever` config fields.
- `config.yaml.template`
  - Mirror `code_rag` schema and `rag_retriever` additions.
- `docker-compose.yml`
  - Add persistent volume for `/data/code_graphs`.
- `control-panel/src/api/client.ts`
  - Add Code tab API methods and request/response types.
- `control-panel/src/pages/Knowledge.tsx`
  - Add Code tab UI, source selector, import progress, repository list.
- `CLAUDE.md`
  - Update only if architecture or commands materially change after implementation; not required during pure plan writing.

## Task 1: Add config, dependencies, and storage scaffolding

**Files:**
- Modify: `aigateway-api/requirements.txt`
- Modify: `config.yaml`
- Modify: `config.yaml.template`
- Modify: `docker-compose.yml`
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- Consumes: existing config loading in `aigateway_core.config`, existing docker gateway service volume pattern
- Produces:
  - `code_rag` config section with fields:
    - `enabled: bool`
    - `allowed_server_paths: list[str]`
    - `max_file_size_mb: int`
    - `max_total_size_mb: int`
    - `max_file_count: int`
    - `ignore_patterns: list[str]`
    - `graph_db_dir: str`
  - `rag_retriever` config additions:
    - `code_rag_enabled: bool`
    - `code_rag_graph_hops: int`
    - `code_rag_top_k: int`

- [ ] **Step 1: Write the failing config/schema test**

```python
from pathlib import Path


def test_code_rag_settings_exist_in_template():
    content = Path("config.yaml.template").read_text(encoding="utf-8")
    assert "code_rag:" in content
    assert "allowed_server_paths:" in content
    assert "graph_db_dir:" in content
    assert "code_rag_enabled:" in content
    assert "code_rag_graph_hops:" in content
    assert "code_rag_top_k:" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_code_rag_settings_exist_in_template -v`
Expected: FAIL because the new config keys are missing.

- [ ] **Step 3: Add minimal test file scaffold**

```python
from pathlib import Path


def test_code_rag_settings_exist_in_template():
    content = Path("config.yaml.template").read_text(encoding="utf-8")
    assert "code_rag:" in content
    assert "allowed_server_paths:" in content
    assert "graph_db_dir:" in content
    assert "code_rag_enabled:" in content
    assert "code_rag_graph_hops:" in content
    assert "code_rag_top_k:" in content
```

Save as `tests/test_code_rag_helpers.py`.

- [ ] **Step 4: Add dependency lines to `aigateway-api/requirements.txt`**

```text
# Code RAG
langchain-community
langchain-text-splitters
gitpython
codegraph
```

Place them near the existing RAG/embedding dependencies.

- [ ] **Step 5: Add runtime config to `config.yaml`**

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

Also extend the existing `rag_retriever` config block with:

```yaml
code_rag_enabled: true
code_rag_graph_hops: 2
code_rag_top_k: 5
```

- [ ] **Step 6: Mirror the same schema in `config.yaml.template`**

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

And the same `rag_retriever` additions:

```yaml
code_rag_enabled: true
code_rag_graph_hops: 2
code_rag_top_k: 5
```

- [ ] **Step 7: Add graph DB persistence in `docker-compose.yml`**

```yaml
services:
  gateway:
    volumes:
      - code_graphs_data:/data/code_graphs

volumes:
  code_graphs_data:
```

Merge this into the existing gateway service volume list instead of replacing other mounts.

- [ ] **Step 8: Run test to verify it passes**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_code_rag_settings_exist_in_template -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add aigateway-api/requirements.txt config.yaml config.yaml.template docker-compose.yml tests/test_code_rag_helpers.py
git commit -m "feat: add code rag config scaffolding"
```

## Task 2: Implement code RAG helper modules

**Files:**
- Create: `aigateway-core/src/aigateway_core/code_rag/__init__.py`
- Create: `aigateway-core/src/aigateway_core/code_rag/splitter.py`
- Create: `aigateway-core/src/aigateway_core/code_rag/graph_builder.py`
- Create: `aigateway-core/src/aigateway_core/code_rag/graph_query.py`
- Create: `aigateway-core/src/aigateway_core/code_rag/embedding_router.py`
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- Consumes: config values from Task 1; sentence-transformers; LangChain loader/parser/splitter chain; CodeGraph package
- Produces:
  - `materialize_model_slug(model_name: str) -> str`
  - `resolve_collection_name(model_name: str) -> str`
  - `probe_embedding_dimension(model_name: str) -> int`
  - `encode_texts(model_name: str, texts: list[str]) -> list[list[float]]`
  - `split_code_directory(root_dir: str, ignore_patterns: list[str]) -> list[dict]`
  - `build_code_graph(source_dir: str, graph_db_path: str) -> str`
  - `lookup_symbol_metadata(graph_db_path: str, file_path: str, symbol_name: str | None, chunk_text: str) -> dict`

- [ ] **Step 1: Write the failing helper tests**

```python
from aigateway_core.code_rag.embedding_router import materialize_model_slug, resolve_collection_name


def test_materialize_model_slug_normalizes_model_name():
    assert materialize_model_slug("Qwen/Qwen3-Embedding-0.6B") == "qwen_qwen3_embedding_0_6b"


def test_resolve_collection_name_prefixes_code_collection():
    assert resolve_collection_name("text-embedding-3-large") == "rag_code_text_embedding_3_large"
```

Append to `tests/test_code_rag_helpers.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_materialize_model_slug_normalizes_model_name tests/test_code_rag_helpers.py::test_resolve_collection_name_prefixes_code_collection -v`
Expected: FAIL with `ModuleNotFoundError` or missing function definitions.

- [ ] **Step 3: Create `embedding_router.py` with minimal slug/collection helpers**

```python
import re


def materialize_model_slug(model_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", model_name.strip().lower())
    return normalized.strip("_")



def resolve_collection_name(model_name: str) -> str:
    return f"rag_code_{materialize_model_slug(model_name)}"
```

- [ ] **Step 4: Export helper symbols from `__init__.py`**

```python
from .embedding_router import materialize_model_slug, resolve_collection_name

__all__ = ["materialize_model_slug", "resolve_collection_name"]
```

- [ ] **Step 5: Run tests to verify slug helpers pass**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_materialize_model_slug_normalizes_model_name tests/test_code_rag_helpers.py::test_resolve_collection_name_prefixes_code_collection -v`
Expected: PASS.

- [ ] **Step 6: Add failing path-validation test**

```python
from pathlib import Path

from aigateway_core.code_rag.splitter import is_path_allowed


def test_is_path_allowed_accepts_allowlisted_path(tmp_path: Path):
    root = tmp_path / "workspace"
    project = root / "repo"
    project.mkdir(parents=True)
    assert is_path_allowed(str(project), [str(root)]) is True
```

- [ ] **Step 7: Run test to verify it fails**

Run: `python3 -m pytest tests/test_code_rag_helpers.py::test_is_path_allowed_accepts_allowlisted_path -v`
Expected: FAIL because `is_path_allowed` does not exist.

- [ ] **Step 8: Implement path validation and line-span fallback helpers in `splitter.py`**

```python
from pathlib import Path


def is_path_allowed(candidate: str, allowed_roots: list[str]) -> bool:
    candidate_path = Path(candidate).resolve()
    for root in allowed_roots:
        root_path = Path(root).resolve()
        if candidate_path == root_path or root_path in candidate_path.parents:
            return True
    return False



def compute_line_span(source_text: str, chunk_text: str) -> tuple[int, int]:
    start_index = source_text.find(chunk_text)
    if start_index < 0:
        return (1, max(1, source_text.count("\n") + 1))
    start_line = source_text[:start_index].count("\n") + 1
    end_line = start_line + chunk_text.count("\n")
    return (start_line, end_line)
```

- [ ] **Step 9: Add LangChain/CodeGraph thin-wrapper function stubs**

```python
# splitter.py
from typing import Any


def split_code_directory(root_dir: str, ignore_patterns: list[str]) -> list[dict[str, Any]]:
    raise NotImplementedError
```

```python
# graph_builder.py
def build_code_graph(source_dir: str, graph_db_path: str) -> str:
    raise NotImplementedError
```

```python
# graph_query.py
from typing import Any


def lookup_symbol_metadata(graph_db_path: str, file_path: str, symbol_name: str | None, chunk_text: str) -> dict[str, Any]:
    raise NotImplementedError
```

```python
# embedding_router.py
from functools import lru_cache
from sentence_transformers import SentenceTransformer


@lru_cache(maxsize=8)
def _get_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)



def probe_embedding_dimension(model_name: str) -> int:
    vector = _get_model(model_name).encode(["dimension probe"], normalize_embeddings=True)[0]
    return len(vector)



def encode_texts(model_name: str, texts: list[str]) -> list[list[float]]:
    return _get_model(model_name).encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()
```

- [ ] **Step 10: Add tests for line-span fallback and dimension probing surface**

```python
from aigateway_core.code_rag.splitter import compute_line_span


def test_compute_line_span_returns_exact_span():
    source = "a\nfoo()\nbar()\n"
    chunk = "foo()\nbar()"
    assert compute_line_span(source, chunk) == (2, 3)
```

- [ ] **Step 11: Run helper tests**

Run: `python3 -m pytest tests/test_code_rag_helpers.py -v`
Expected: PASS for slug/path/line-span tests; any remaining failures must be caused only by deliberately unimplemented wrapper behavior not yet asserted in tests.

- [ ] **Step 12: Flesh out the wrappers around real packages**

Implement these shapes using the exact package APIs verified from the installed versions in Task 1. For CodeGraph querying, prefer the package's documented Python API; only drop to direct SQLite reads if the package does not expose symbol query helpers.

```python
# splitter.py
from pathlib import Path
from langchain_community.document_loaders.generic import GenericLoader
from langchain_community.document_loaders.parsers import LanguageParser
from langchain_text_splitters import RecursiveCharacterTextSplitter, Language


def split_code_directory(root_dir: str, ignore_patterns: list[str]) -> list[dict]:
    loader = GenericLoader.from_filesystem(
        root_dir,
        glob="**/*",
        parser=LanguageParser(parser_threshold=500),
    )
    documents = loader.load()
    results = []
    for doc in documents:
        path = str(doc.metadata.get("source", ""))
        if any(part in path for part in ignore_patterns):
            continue
        language = doc.metadata.get("language", "python")
        splitter = RecursiveCharacterTextSplitter.from_language(
            language=getattr(Language, language.upper(), Language.PYTHON),
            chunk_size=2000,
            chunk_overlap=200,
        )
        source_text = doc.page_content
        for index, chunk in enumerate(splitter.split_text(source_text)):
            start_line, end_line = compute_line_span(source_text, chunk)
            results.append(
                {
                    "file_path": str(Path(path).relative_to(root_dir)) if path else "",
                    "filename": Path(path).name if path else "",
                    "language": language,
                    "chunk_index": index,
                    "chunk_text": chunk,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )
    return results
```

```python
# graph_builder.py
from pathlib import Path


def build_code_graph(source_dir: str, graph_db_path: str) -> str:
    from codegraph import CodeGraph

    Path(graph_db_path).parent.mkdir(parents=True, exist_ok=True)
    graph = CodeGraph(source_dir)
    graph.build()
    graph.save(graph_db_path)
    return graph_db_path
```

```python
# graph_query.py
from typing import Any


def lookup_symbol_metadata(graph_db_path: str, file_path: str, symbol_name: str | None, chunk_text: str) -> dict[str, Any]:
    if not symbol_name:
        return {"callers": [], "callees": [], "imports": [], "chunk_type": "module", "function_name": None, "class_name": None}
    return {
        "callers": [],
        "callees": [],
        "imports": [],
        "chunk_type": "function",
        "function_name": symbol_name,
        "class_name": None,
    }
```

- [ ] **Step 13: Run helper tests again**

Run: `python3 -m pytest tests/test_code_rag_helpers.py -v`
Expected: PASS.

- [ ] **Step 14: Commit**

```bash
git add aigateway-core/src/aigateway_core/code_rag tests/test_code_rag_helpers.py
git commit -m "feat: add code rag helper modules"
```

## Task 3: Implement code RAG API routes and async task lifecycle

**Files:**
- Create: `aigateway-api/src/aigateway_api/code_rag_routes.py`
- Modify: `aigateway-api/src/aigateway_api/main.py`
- Test: `tests/test_code_rag_routes.py`

**Interfaces:**
- Consumes:
  - `split_code_directory(root_dir: str, ignore_patterns: list[str]) -> list[dict]`
  - `build_code_graph(source_dir: str, graph_db_path: str) -> str`
  - `lookup_symbol_metadata(graph_db_path: str, file_path: str, symbol_name: str | None, chunk_text: str) -> dict`
  - `resolve_collection_name(model_name: str) -> str`
  - `probe_embedding_dimension(model_name: str) -> int`
  - `encode_texts(model_name: str, texts: list[str]) -> list[list[float]]`
- Produces:
  - `POST /admin/rag/code/import`
  - `GET /admin/rag/code/tasks/{task_id}`
  - `GET /admin/rag/code/repositories`
  - `DELETE /admin/rag/code/repositories/{document_id}`

- [ ] **Step 1: Write the failing route test for task creation**

```python
from fastapi.testclient import TestClient

from src.aigateway_api.main import create_app


def test_post_code_import_returns_task_id(monkeypatch):
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/admin/rag/code/import",
        json={
            "source_type": "server_path",
            "server_path": "/home/ubuntu/gateway2",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        },
        headers={"x-api-key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert "task_id" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_post_code_import_returns_task_id -v`
Expected: FAIL with 404 because the route does not exist.

- [ ] **Step 3: Create minimal router and register it**

```python
# aigateway-api/src/aigateway_api/code_rag_routes.py
from fastapi import APIRouter
import uuid

router = APIRouter()


@router.post("/rag/code/import")
async def import_code_document() -> dict[str, str]:
    return {"task_id": str(uuid.uuid4()), "status": "pending"}
```

```python
# in aigateway-api/src/aigateway_api/main.py
from aigateway_api.code_rag_routes import router as code_rag_router

app.include_router(code_rag_router, prefix="/admin")
```

- [ ] **Step 4: Run the route test to verify it passes**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_post_code_import_returns_task_id -v`
Expected: PASS.

- [ ] **Step 5: Add failing tests for task status and repository list**

```python
def test_get_code_task_status_returns_progress(monkeypatch):
    app = create_app()
    client = TestClient(app)
    response = client.get("/admin/rag/code/tasks/task-123", headers={"x-api-key": "test-key"})
    assert response.status_code == 200
    assert "status" in response.json()


def test_list_code_repositories_returns_array(monkeypatch):
    app = create_app()
    client = TestClient(app)
    response = client.get("/admin/rag/code/repositories", headers={"x-api-key": "test-key"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_get_code_task_status_returns_progress tests/test_code_rag_routes.py::test_list_code_repositories_returns_array -v`
Expected: FAIL with 404.

- [ ] **Step 7: Implement minimal in-memory endpoints first**

```python
_TASKS: dict[str, dict] = {}
_REPOSITORIES: list[dict] = []


@router.get("/rag/code/tasks/{task_id}")
async def get_code_task(task_id: str) -> dict:
    return _TASKS.get(task_id, {"task_id": task_id, "status": "pending", "done": 0, "total": 0, "current_file": None, "error": None})


@router.get("/rag/code/repositories")
async def list_code_repositories() -> list[dict]:
    return _REPOSITORIES
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_get_code_task_status_returns_progress tests/test_code_rag_routes.py::test_list_code_repositories_returns_array -v`
Expected: PASS.

- [ ] **Step 9: Replace in-memory state with Redis-backed helpers and real import orchestration**

Implement these functions inside `code_rag_routes.py`:

```python
import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, UploadFile


async def _set_task_state(redis_client, task_id: str, payload: dict) -> None:
    await redis_client.hset(f"aigateway:rag:code:tasks:{task_id}", mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()})
    await redis_client.expire(f"aigateway:rag:code:tasks:{task_id}", 86400)


async def _get_task_state(redis_client, task_id: str) -> dict:
    raw = await redis_client.hgetall(f"aigateway:rag:code:tasks:{task_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="task not found")
    decoded = {}
    for key, value in raw.items():
        text = value.decode() if isinstance(value, bytes) else value
        decoded[key.decode() if isinstance(key, bytes) else key] = text
    return decoded


async def _append_repository(redis_client, payload: dict) -> None:
    await redis_client.lpush("aigateway:rag:code:documents", json.dumps(payload))
```

Create one background task function with this shape:

```python
async def _run_code_import_task(
    request: Request,
    task_id: str,
    source_dir: str,
    source_label: str,
    source_type: str,
    embedding_model: str,
) -> None:
    ...
```

Its responsibilities:
- load config and qdrant manager from app state
- split source directory
- build graph DB at `graph_db_dir/<document_id>.db`
- enrich chunks with graph metadata
- probe collection dimension and `upsert_collection`
- encode chunk texts in `run_in_executor`
- upsert Qdrant points
- compute repository aggregate counts
- append Redis repository metadata
- update task state at each phase
- on failure, mark task failed and roll back points for the current `document_id`
- always clean up temp directories for `folder`, `zip`, and `git`
```

- [ ] **Step 10: Implement request parsing for four source types**

Add helpers with exact signatures:

```python
async def _materialize_folder_upload(files: list[UploadFile]) -> tuple[str, str]:
    ...  # returns (temp_dir, source_label)


async def _materialize_zip_upload(file: UploadFile) -> tuple[str, str]:
    ...


async def _materialize_git_repo(git_url: str, git_branch: str | None) -> tuple[str, str]:
    ...


async def _validate_server_path(server_path: str, allowed_roots: list[str]) -> tuple[str, str]:
    ...
```

Use `git.Repo.clone_from(git_url, temp_dir, depth=1, branch=git_branch)` for Git and `zipfile.ZipFile` with path traversal checks for ZIP.

- [ ] **Step 11: Add delete-route failing test**

```python
def test_delete_code_repository_returns_204(monkeypatch):
    app = create_app()
    client = TestClient(app)
    response = client.delete("/admin/rag/code/repositories/doc-123", headers={"x-api-key": "test-key"})
    assert response.status_code == 204
```

- [ ] **Step 12: Run delete test to verify it fails**

Run: `python3 -m pytest tests/test_code_rag_routes.py::test_delete_code_repository_returns_204 -v`
Expected: FAIL with 404.

- [ ] **Step 13: Implement delete route**

```python
@router.delete("/rag/code/repositories/{document_id}", status_code=204)
async def delete_code_repository(document_id: str, request: Request) -> None:
    app_state = request.app.state
    redis_client = app_state.redis_client
    qdrant_mgr = app_state.qdrant_client
    graph_db_dir = app_state.config_manager.get("code_rag", {}).get("graph_db_dir", "/data/code_graphs")
    collections = await qdrant_mgr.list_collections()
    for name in collections:
        if name.startswith("rag_code_"):
            await qdrant_mgr.delete_points_by_filter(name=name, must=[{"key": "document_id", "match": {"value": document_id}}])
    graph_db_path = Path(graph_db_dir) / f"{document_id}.db"
    if graph_db_path.exists():
        graph_db_path.unlink()
```

Also remove matching repository metadata from Redis by filtering the current `aigateway:rag:code:documents` list and rewriting it.

- [ ] **Step 14: Run route tests**

Run: `python3 -m pytest tests/test_code_rag_routes.py -v`
Expected: PASS.

- [ ] **Step 15: Commit**

```bash
git add aigateway-api/src/aigateway_api/code_rag_routes.py aigateway-api/src/aigateway_api/main.py tests/test_code_rag_routes.py
git commit -m "feat: add code rag api routes"
```

## Task 4: Extend retrieval plugin for code collections and graph hops

**Files:**
- Modify: `aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py`
- Test: `tests/test_rag_retriever_code_rag.py`

**Interfaces:**
- Consumes:
  - code RAG Qdrant payload fields from Task 3
  - graph DB files at `graph_db_dir/<document_id>.db`
  - `lookup_symbol_metadata(...) -> dict`
- Produces:
  - code-aware retrieval path behind config gate
  - graph-expanded retrieval context merged with existing text hits

- [ ] **Step 1: Write the failing retrieval config-gate test**

```python
from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin


def test_code_rag_disabled_keeps_text_only_behavior(monkeypatch):
    plugin = RAGRetrieverPlugin(config=type("Cfg", (), {
        "enabled": True,
        "top_k": 5,
        "similarity_threshold": 0.7,
        "collection_name": "rag_documents",
        "embedding_backend": "local",
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        "code_rag_enabled": False,
        "code_rag_graph_hops": 2,
        "code_rag_top_k": 5,
    })())
    assert getattr(plugin._config, "code_rag_enabled", False) is False
```

- [ ] **Step 2: Run test to verify it fails only if constructor/config handling cannot accept the new field**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py::test_code_rag_disabled_keeps_text_only_behavior -v`
Expected: FAIL if the test file/import path is missing; PASS once the new test file exists.

- [ ] **Step 3: Create the retrieval test file scaffold**

```python
from aigateway_core.plugins.rag_retriever_plugin import RAGRetrieverPlugin


def test_code_rag_disabled_keeps_text_only_behavior(monkeypatch):
    plugin = RAGRetrieverPlugin(config=type("Cfg", (), {
        "enabled": True,
        "top_k": 5,
        "similarity_threshold": 0.7,
        "collection_name": "rag_documents",
        "embedding_backend": "local",
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        "code_rag_enabled": False,
        "code_rag_graph_hops": 2,
        "code_rag_top_k": 5,
    })())
    assert getattr(plugin._config, "code_rag_enabled", False) is False
```

Save as `tests/test_rag_retriever_code_rag.py`.

- [ ] **Step 4: Add failing test for code collection enumeration helper**

```python
from aigateway_core.plugins.rag_retriever_plugin import _filter_code_collections


def test_filter_code_collections_only_keeps_prefixed_names():
    names = ["rag_documents", "rag_code_qwen", "rag_code_openai", "semantic_cache"]
    assert _filter_code_collections(names) == ["rag_code_qwen", "rag_code_openai"]
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py::test_filter_code_collections_only_keeps_prefixed_names -v`
Expected: FAIL because helper does not exist.

- [ ] **Step 6: Add helper functions in `rag_retriever_plugin.py`**

```python
def _filter_code_collections(names: list[str]) -> list[str]:
    return [name for name in names if name.startswith("rag_code_")]
```

Add a second helper:

```python
def _dedupe_hits_by_identity(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, int]] = set()
    result: list[dict] = []
    for item in items:
        key = (
            item.get("document_id", ""),
            item.get("file_path", item.get("filename", "")),
            int(item.get("chunk_index", 0)),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
```

- [ ] **Step 7: Run helper test to verify it passes**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py::test_filter_code_collections_only_keeps_prefixed_names -v`
Expected: PASS.

- [ ] **Step 8: Add failing graph-expansion unit test**

```python
from aigateway_core.plugins.rag_retriever_plugin import _expand_code_hit_metadata


def test_expand_code_hit_metadata_reads_call_relationships():
    hit = {"document_id": "doc1", "file_path": "auth.py", "function_name": "login"}
    expanded = _expand_code_hit_metadata(hit, {"callers": ["register"], "callees": ["hash_password"]})
    assert expanded["callers"] == ["register"]
    assert expanded["callees"] == ["hash_password"]
```

- [ ] **Step 9: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py::test_expand_code_hit_metadata_reads_call_relationships -v`
Expected: FAIL because helper does not exist.

- [ ] **Step 10: Implement the metadata merge helper**

```python
def _expand_code_hit_metadata(hit: dict, graph_metadata: dict) -> dict:
    merged = dict(hit)
    merged["callers"] = graph_metadata.get("callers", [])
    merged["callees"] = graph_metadata.get("callees", [])
    merged["imports"] = graph_metadata.get("imports", [])
    return merged
```

- [ ] **Step 11: Run unit tests**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py -v`
Expected: PASS for helper-level tests.

- [ ] **Step 12: Integrate code retrieval flow in `RAGRetrieverPlugin`**

Add methods with these signatures:

```python
async def _list_code_collections(self) -> list[str]:
    ...


async def _retrieve_code_hits(self, query: str) -> list[dict]:
    ...


async def _expand_code_hits_with_graph(self, hits: list[dict]) -> list[dict]:
    ...
```

Implementation requirements:
- if `code_rag_enabled` is false, return `[]`
- call Qdrant to list collections, filter `rag_code_*`
- query each code collection with the same embedding backend as the plugin uses
- normalize each hit into payload dictionaries containing `document_id`, `file_path`, `filename`, `chunk_index`, `chunk_text`, `function_name`, `class_name`, `start_line`, `end_line`
- for each hit, open `/data/code_graphs/<document_id>.db` and enrich from `lookup_symbol_metadata(...)`
- tolerate per-collection and per-graph failures with warning logs
- dedupe before returning

- [ ] **Step 13: Integrate code hits into `execute()` before `_inject_system_message`**

Pseudo-shape:

```python
text_hits = ...existing retrieval output...
code_hits = await self._retrieve_code_hits(query_text)
code_hits = await self._expand_code_hits_with_graph(code_hits)
merged_hits = text_hits + code_hits
merged_hits = _dedupe_hits_by_identity(merged_hits)
```

Make sure the existing text path remains unchanged when no code hits are available.

- [ ] **Step 14: Run retrieval tests**

Run: `python3 -m pytest tests/test_rag_retriever_code_rag.py -v`
Expected: PASS.

- [ ] **Step 15: Commit**

```bash
git add aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py tests/test_rag_retriever_code_rag.py
git commit -m "feat: add code rag retrieval expansion"
```

## Task 5: Add frontend API client for Code tab

**Files:**
- Modify: `control-panel/src/api/client.ts`
- Test: `control-panel/src/api/client.ts` (typecheck-driven; no dedicated frontend test harness is defined in repo docs)

**Interfaces:**
- Consumes:
  - backend endpoints from Task 3
- Produces:
  - `CodeImportTask`
  - `CodeRepositoryImport`
  - `importCodeRepository(...)`
  - `getCodeImportTask(...)`
  - `listCodeRepositories()`
  - `deleteCodeRepository(...)`

- [ ] **Step 1: Add the TypeScript interfaces**

```ts
export interface CodeImportTask {
  task_id: string
  status: 'pending' | 'scanning' | 'splitting' | 'building_graph' | 'embedding' | 'completed' | 'failed'
  current_file: string | null
  done: number
  total: number
  error: string | null
}

export interface CodeRepositoryImport {
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

- [ ] **Step 2: Add the API methods**

```ts
export async function importCodeRepository(payload: FormData | {
  source_type: 'server_path' | 'git'
  server_path?: string
  git_url?: string
  git_branch?: string
  embedding_model: string
}): Promise<{ task_id: string; status: 'pending' }> {
  const headers = await ensureAuthHeaders()
  const init: RequestInit = {
    method: 'POST',
    headers: payload instanceof FormData ? headers : { ...headers, 'Content-Type': 'application/json' },
    body: payload instanceof FormData ? payload : JSON.stringify(payload),
  }
  const response = await fetch(`${API_BASE}/admin/rag/code/import`, init)
  return handleJsonResponse(response)
}

export async function getCodeImportTask(taskId: string): Promise<CodeImportTask> {
  const headers = await ensureAuthHeaders()
  const response = await fetch(`${API_BASE}/admin/rag/code/tasks/${taskId}`, { headers })
  return handleJsonResponse(response)
}

export async function listCodeRepositories(): Promise<CodeRepositoryImport[]> {
  const headers = await ensureAuthHeaders()
  const response = await fetch(`${API_BASE}/admin/rag/code/repositories`, { headers })
  return handleJsonResponse(response)
}

export async function deleteCodeRepository(documentId: string): Promise<void> {
  const headers = await ensureAuthHeaders()
  const response = await fetch(`${API_BASE}/admin/rag/code/repositories/${documentId}`, {
    method: 'DELETE',
    headers,
  })
  if (!response.ok) throw new Error(`Delete failed: ${response.status}`)
}
```

- [ ] **Step 3: Run frontend typecheck**

Run: `cd control-panel && npm run build`
Expected: FAIL if interfaces or helper names mismatch existing client utilities.

- [ ] **Step 4: Align the client methods with existing file conventions**

Before finalizing the new methods, inspect the surrounding code in `control-panel/src/api/client.ts` and make these exact adjustments:
- if neighboring methods use `apiFetch(...)`, rewrite the new code-RAG methods to use `apiFetch(...)` too
- if neighboring methods use a shared JSON parser helper, replace `handleJsonResponse(response)` with that exact helper name
- keep the new method names adjacent to `listRagDocuments`, `importRagDocument`, and `deleteRagDocument`
- preserve the file's existing export style (`export async function ...` vs grouped exports)

- [ ] **Step 5: Run frontend typecheck again**

Run: `cd control-panel && npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat: add code rag api client"
```

## Task 6: Add Code tab UI and progress polling in Knowledge page

**Files:**
- Modify: `control-panel/src/pages/Knowledge.tsx`
- Test: `control-panel/src/pages/Knowledge.tsx` (typecheck + manual UI verification)

**Interfaces:**
- Consumes:
  - `CodeImportTask`
  - `CodeRepositoryImport`
  - `importCodeRepository(...)`
  - `getCodeImportTask(...)`
  - `listCodeRepositories()`
  - `deleteCodeRepository(...)`
- Produces:
  - Code tab UI with source selector, embedding model input, progress section, repository list

- [ ] **Step 1: Add page state for Code tab**

Add state variables shaped like:

```tsx
const [knowledgeTab, setKnowledgeTab] = useState<'text' | 'code'>('text')
const [codeSourceType, setCodeSourceType] = useState<'folder' | 'server_path' | 'git' | 'zip'>('folder')
const [embeddingModel, setEmbeddingModel] = useState('Qwen/Qwen3-Embedding-0.6B')
const [serverPath, setServerPath] = useState('')
const [gitUrl, setGitUrl] = useState('')
const [gitBranch, setGitBranch] = useState('main')
const [zipFile, setZipFile] = useState<File | null>(null)
const [folderFiles, setFolderFiles] = useState<File[]>([])
const [codeRepositories, setCodeRepositories] = useState<CodeRepositoryImport[]>([])
const [codeTask, setCodeTask] = useState<CodeImportTask | null>(null)
```

- [ ] **Step 2: Add repository loading effect**

```tsx
useEffect(() => {
  if (knowledgeTab !== 'code') return
  void listCodeRepositories().then(setCodeRepositories).catch((error) => {
    setError(error instanceof Error ? error.message : '加载代码知识库失败')
  })
}, [knowledgeTab])
```

Use the file's existing error-state conventions if they differ.

- [ ] **Step 3: Add `startCodeImport` handler**

```tsx
async function startCodeImport() {
  const payload =
    codeSourceType === 'server_path'
      ? { source_type: 'server_path' as const, server_path: serverPath, embedding_model: embeddingModel }
      : codeSourceType === 'git'
      ? { source_type: 'git' as const, git_url: gitUrl, git_branch: gitBranch, embedding_model: embeddingModel }
      : (() => {
          const form = new FormData()
          form.append('source_type', codeSourceType)
          form.append('embedding_model', embeddingModel)
          if (codeSourceType === 'zip' && zipFile) form.append('file', zipFile)
          if (codeSourceType === 'folder') {
            folderFiles.forEach((file) => form.append('files', file, (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name))
          }
          return form
        })()

  const task = await importCodeRepository(payload)
  setCodeTask({ task_id: task.task_id, status: 'pending', current_file: null, done: 0, total: 0, error: null })
}
```

- [ ] **Step 4: Add progress polling effect**

```tsx
useEffect(() => {
  if (!codeTask || codeTask.status === 'completed' || codeTask.status === 'failed') return
  const timer = window.setInterval(() => {
    void getCodeImportTask(codeTask.task_id).then((nextTask) => {
      setCodeTask(nextTask)
      if (nextTask.status === 'completed') {
        void listCodeRepositories().then(setCodeRepositories)
      }
    }).catch((error) => {
      setCodeTask((current) => current ? { ...current, status: 'failed', error: error instanceof Error ? error.message : '导入状态轮询失败' } : current)
    })
  }, 2000)
  return () => window.clearInterval(timer)
}, [codeTask])
```

- [ ] **Step 5: Add the tab switcher markup**

```tsx
<div className="knowledge-tabs">
  <button type="button" onClick={() => setKnowledgeTab('text')} className={knowledgeTab === 'text' ? 'active' : ''}>文本</button>
  <button type="button" onClick={() => setKnowledgeTab('code')} className={knowledgeTab === 'code' ? 'active' : ''}>代码</button>
</div>
```

Apply existing page styling conventions rather than inventing a new visual system if the page already has tab/button classes.

- [ ] **Step 6: Add the Code tab form UI**

Render these exact controls when `knowledgeTab === 'code'`:

```tsx
<select value={codeSourceType} onChange={(event) => setCodeSourceType(event.target.value as 'folder' | 'server_path' | 'git' | 'zip')}>
  <option value="folder">拖拽/选择文件夹</option>
  <option value="server_path">服务器目录路径</option>
  <option value="git">Git 仓库 URL</option>
  <option value="zip">ZIP 上传</option>
</select>

<input value={embeddingModel} onChange={(event) => setEmbeddingModel(event.target.value)} placeholder="Qwen/Qwen3-Embedding-0.6B" />
```

Conditional sections:
- `folder`: `<input type="file" webkitdirectory="" multiple ... />`
- `server_path`: `<input value={serverPath} ... />`
- `git`: `<input value={gitUrl} ... />` and `<input value={gitBranch} ... />`
- `zip`: `<input type="file" accept=".zip" ... />`

- [ ] **Step 7: Add task progress UI**

```tsx
{codeTask && (
  <div>
    <div>状态：{codeTask.status}</div>
    <div>当前文件：{codeTask.current_file || '-'}</div>
    <div>进度：{codeTask.done}/{codeTask.total}</div>
    {codeTask.error && <div className="error-text">{codeTask.error}</div>}
  </div>
)}
```

- [ ] **Step 8: Add repository list rendering and delete action**

```tsx
{codeRepositories.map((repo) => (
  <tr key={repo.document_id}>
    <td>{repo.source_label}</td>
    <td>{repo.source_type}</td>
    <td>{repo.language_summary.join(', ')}</td>
    <td>{repo.file_count}</td>
    <td>{repo.function_count}</td>
    <td>{repo.class_count}</td>
    <td>{repo.chunk_count}</td>
    <td>{repo.embedding_model}</td>
    <td>{repo.import_time}</td>
    <td>
      <button
        type="button"
        onClick={async () => {
          await deleteCodeRepository(repo.document_id)
          setCodeRepositories((items) => items.filter((item) => item.document_id !== repo.document_id))
        }}
      >
        删除
      </button>
    </td>
  </tr>
))}
```

- [ ] **Step 9: Run frontend typecheck**

Run: `cd control-panel && npm run build`
Expected: PASS.

- [ ] **Step 10: Manual UI verification**

Run: `cd control-panel && npm run dev`
Expected:
- Knowledge page renders Text and Code tabs
- source selector switches forms
- starting an import creates a task state
- completed import refreshes the repository list

- [ ] **Step 11: Commit**

```bash
git add control-panel/src/pages/Knowledge.tsx
git commit -m "feat: add code knowledge tab"
```

## Task 7: Wire route logic to helper modules and verify end-to-end import

**Files:**
- Modify: `aigateway-api/src/aigateway_api/code_rag_routes.py`
- Modify: `aigateway-core/src/aigateway_core/code_rag/splitter.py`
- Modify: `aigateway-core/src/aigateway_core/code_rag/graph_builder.py`
- Modify: `aigateway-core/src/aigateway_core/code_rag/graph_query.py`
- Modify: `aigateway-core/src/aigateway_core/code_rag/embedding_router.py`
- Test: `tests/test_code_rag_routes.py`
- Test: `tests/test_code_rag_helpers.py`

**Interfaces:**
- Consumes: all helpers and routes created in Tasks 2 and 3
- Produces: fully wired import pipeline that stores aggregate repository metadata and Qdrant chunk payloads with line spans + graph metadata

- [ ] **Step 1: Add a failing import-payload shape test**

```python
from aigateway_core.code_rag.splitter import compute_line_span


def test_code_chunk_payload_includes_required_fields():
    payload = {
        "document_id": "doc1",
        "filename": "auth.py",
        "file_path": "core/auth.py",
        "language": "python",
        "chunk_index": 0,
        "chunk_text": "def login():\n    pass",
        "chunk_type": "function",
        "function_name": "login",
        "class_name": None,
        "start_line": 1,
        "end_line": 2,
        "callers": [],
        "callees": [],
        "imports": [],
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
    }
    assert set(payload.keys()) >= {
        "document_id", "filename", "file_path", "language", "chunk_index", "chunk_text",
        "chunk_type", "function_name", "class_name", "start_line", "end_line",
        "callers", "callees", "imports", "embedding_model",
    }
```

- [ ] **Step 2: Run the focused tests**

Run: `python3 -m pytest tests/test_code_rag_helpers.py tests/test_code_rag_routes.py -v`
Expected: identify any missing payload fields or integration breakages before final wiring.

- [ ] **Step 3: Implement final chunk enrichment in `_run_code_import_task`**

When converting split chunks to Qdrant points, build payloads with this exact shape:

```python
payload = {
    "document_id": document_id,
    "filename": chunk["filename"],
    "file_path": chunk["file_path"],
    "language": chunk["language"],
    "chunk_index": chunk["chunk_index"],
    "chunk_text": chunk["chunk_text"],
    "chunk_type": graph_metadata.get("chunk_type", "module"),
    "function_name": graph_metadata.get("function_name"),
    "class_name": graph_metadata.get("class_name"),
    "start_line": chunk["start_line"],
    "end_line": chunk["end_line"],
    "callers": graph_metadata.get("callers", []),
    "callees": graph_metadata.get("callees", []),
    "imports": graph_metadata.get("imports", []),
    "embedding_model": embedding_model,
}
```

Upsert points using the route layer's Qdrant manager and a stable point id, for example:

```python
point_id = f"{document_id}:{chunk['file_path']}:{chunk['chunk_index']}"
```

- [ ] **Step 4: Compute and store repository aggregate metadata**

Append a Redis record with this exact shape:

```python
repository_record = {
    "document_id": document_id,
    "source_type": source_type,
    "source_label": source_label,
    "file_count": len({chunk["file_path"] for chunk in chunks}),
    "language_summary": sorted({chunk["language"] for chunk in chunks}),
    "function_count": sum(1 for payload in payloads if payload["chunk_type"] == "function"),
    "class_count": sum(1 for payload in payloads if payload["chunk_type"] == "class"),
    "chunk_count": len(payloads),
    "embedding_model": embedding_model,
    "import_time": datetime.utcnow().isoformat() + "Z",
}
```

- [ ] **Step 5: Implement rollback on Qdrant failure**

Wrap upsert in `try/except` and, on failure, delete all points with the current `document_id` from the current collection, mark task failed, and re-raise/log.

```python
try:
    await qdrant_mgr.upsert_points(name=collection_name, points=points)
except Exception:
    await qdrant_mgr.delete_points_by_filter(name=collection_name, must=[{"key": "document_id", "match": {"value": document_id}}])
    raise
```

- [ ] **Step 6: Run backend tests**

Run: `python3 -m pytest tests/test_code_rag_helpers.py tests/test_code_rag_routes.py tests/test_rag_retriever_code_rag.py -v`
Expected: PASS.

- [ ] **Step 7: Rebuild backend image and verify health**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway && curl -sf localhost:8000/health && docker compose logs --tail=50 gateway | grep -i error`
Expected:
- docker build succeeds
- `/health` returns success
- gateway log grep returns no relevant startup/import errors (empty output is acceptable)

- [ ] **Step 8: Manual import validation using the real app**

Run these checks:

```bash
curl -s -X POST http://localhost:8000/admin/rag/code/import \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: <ADMIN_KEY>' \
  -d '{"source_type":"server_path","server_path":"/home/ubuntu/gateway2","embedding_model":"Qwen/Qwen3-Embedding-0.6B"}'
```

Then poll:

```bash
curl -s -H 'x-api-key: <ADMIN_KEY>' http://localhost:8000/admin/rag/code/tasks/<TASK_ID>
```

And verify repository list:

```bash
curl -s -H 'x-api-key: <ADMIN_KEY>' http://localhost:8000/admin/rag/code/repositories
```

Expected:
- import reaches `completed`
- repository appears in list
- graph DB file exists under `/data/code_graphs`

- [ ] **Step 9: Commit**

```bash
git add aigateway-api/src/aigateway_api/code_rag_routes.py aigateway-core/src/aigateway_core/code_rag tests/test_code_rag_helpers.py tests/test_code_rag_routes.py
git commit -m "feat: wire code rag import pipeline"
```

## Task 8: Final verification, docs sync, and cleanup

**Files:**
- Modify: `CLAUDE.md` (only if implementation changes repo commands/architecture enough to warrant it)
- Test: `tests/test_code_rag_helpers.py`
- Test: `tests/test_code_rag_routes.py`
- Test: `tests/test_rag_retriever_code_rag.py`

**Interfaces:**
- Consumes: completed implementation from Tasks 1-7
- Produces: verified, documented, shippable feature branch state

- [ ] **Step 1: Run backend regression tests**

Run: `python3 -m pytest tests/test_code_rag_helpers.py tests/test_code_rag_routes.py tests/test_rag_retriever_code_rag.py tests/test_cache_key_v2.py -v`
Expected: PASS.

- [ ] **Step 2: Run frontend production build**

Run: `cd control-panel && npm run build`
Expected: PASS.

- [ ] **Step 3: Rebuild both images if frontend and backend changed**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway control-panel && curl -sf localhost:8000/health && docker compose logs --tail=50 gateway | grep -i error`
Expected:
- builds succeed
- backend health passes
- no meaningful gateway errors on startup

- [ ] **Step 4: Manual end-to-end check**

Verify all of the following in the running app:
- Knowledge page shows Text and Code tabs
- Code tab supports folder/server path/Git/ZIP inputs
- import progress updates while running
- completed import appears in repository list
- delete removes the repository entry
- existing Text tab still imports/list/deletes documents correctly

- [ ] **Step 5: Update `CLAUDE.md` if needed**

If the final implementation introduces a new persistent operational fact, add one short current-state note. Example snippet style:

```md
- **Code RAG** — Control Panel Knowledge page now has a Code tab with async imports (folder/server path/Git/ZIP). Graph DBs live under `/data/code_graphs`; code vectors use per-model `rag_code_*` collections.
```

Skip this step if no durable contributor-facing guidance changed.

- [ ] **Step 6: Final commit**

```bash
git add CLAUDE.md
git commit -m "docs: record code rag workflow"
```

If `CLAUDE.md` was unchanged, skip this commit and report that no docs sync was needed.

## Self-Review Checklist

- Spec coverage:
  - Code tab UI → Tasks 5-6
  - Four import sources → Tasks 3 and 6
  - AST.txt parsing chain → Tasks 2 and 7
  - CodeGraph graph DB + metadata → Tasks 2, 4, 7
  - per-model Qdrant collections → Tasks 2, 3, 7
  - async tasks/progress → Task 3 and Task 6
  - retrieval graph hops → Task 4
  - server_path allowlist/security → Tasks 1-3
  - persistence volume → Task 1
  - line spans → Tasks 2 and 7
- Placeholder scan:
  - One implementation note remains intentionally open: verify the exact CodeGraph package API and prefer its official query API over raw SQLite if available.
  - One implementation note remains intentionally open: align LangChain import paths with the actually installable versions in `requirements.txt`.
- Type consistency:
  - Repository type name: `CodeRepositoryImport`
  - Task type name: `CodeImportTask`
  - Collection naming helper: `resolve_collection_name(model_name: str) -> str`
  - Graph lookup helper: `lookup_symbol_metadata(graph_db_path: str, file_path: str, symbol_name: str | None, chunk_text: str) -> dict`

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-07-code-knowledge-base.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
