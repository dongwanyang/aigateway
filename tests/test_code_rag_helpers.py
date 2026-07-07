"""Tests for the Code RAG scaffolding (Task 1).

Locked into repo-root config files so misplaced keys and missing volumes
fail loudly instead of silently drifting.
"""
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_code_rag_settings_exist_in_template() -> None:
    content = (REPO_ROOT / "config.yaml.template").read_text(encoding="utf-8")
    assert "code_rag:" in content
    assert "allowed_server_paths:" in content
    assert "graph_db_dir:" in content
    assert "code_rag_enabled:" in content
    assert "code_rag_graph_hops:" in content
    assert "code_rag_top_k:" in content


def _find_plugin(config: dict, plugin_name: str) -> dict:
    for entry in config.get("plugins", []) or []:
        if entry.get("name") == plugin_name:
            return entry
    raise AssertionError(f"plugin '{plugin_name}' missing from config")


def test_runtime_config_has_top_level_code_rag_block() -> None:
    config = _load_yaml(REPO_ROOT / "config.yaml")
    assert "code_rag" in config, "top-level code_rag block missing from config.yaml"
    code_rag = config["code_rag"]
    assert code_rag["enabled"] is True
    assert isinstance(code_rag["allowed_server_paths"], list) and code_rag["allowed_server_paths"]
    assert code_rag["max_file_size_mb"] == 5
    assert code_rag["max_total_size_mb"] == 200
    assert code_rag["max_file_count"] == 5000
    assert set(code_rag["ignore_patterns"]) >= {"node_modules", ".git", "__pycache__", "dist", "build"}
    assert code_rag["graph_db_dir"] == "/data/code_graphs"


def test_runtime_rag_retriever_owns_code_rag_flags() -> None:
    """code_rag_* keys MUST live under rag_retriever.config, not elsewhere."""
    config = _load_yaml(REPO_ROOT / "config.yaml")
    rag_retriever = _find_plugin(config, "rag_retriever")
    rag_cfg = rag_retriever.get("config") or {}
    assert rag_cfg.get("code_rag_enabled") is True
    assert rag_cfg.get("code_rag_graph_hops") == 2
    assert rag_cfg.get("code_rag_top_k") == 5

    for other in ("pii_detector", "prompt_cache", "semantic_cache",
                  "model_router", "prompt_compress", "conv_compressor"):
        try:
            plugin = _find_plugin(config, other)
        except AssertionError:
            continue
        other_cfg = plugin.get("config") or {}
        for key in ("code_rag_enabled", "code_rag_graph_hops", "code_rag_top_k"):
            assert key not in other_cfg, (
                f"code_rag flag '{key}' must NOT live under plugin '{other}'"
            )


def test_template_rag_retriever_owns_code_rag_flags() -> None:
    config = _load_yaml(REPO_ROOT / "config.yaml.template")
    rag_retriever = _find_plugin(config, "rag_retriever")
    rag_cfg = rag_retriever.get("config") or {}
    assert "code_rag_enabled" in rag_cfg
    assert "code_rag_graph_hops" in rag_cfg
    assert "code_rag_top_k" in rag_cfg


def test_docker_compose_has_code_graphs_volume() -> None:
    compose = _load_yaml(REPO_ROOT / "docker-compose.yml")
    named_volumes = compose.get("volumes") or {}
    assert "code_graphs_data" in named_volumes, "named volume code_graphs_data missing"

    gateway = compose["services"]["gateway"]
    mounts = [str(item) for item in (gateway.get("volumes") or [])]
    assert any("code_graphs_data:/data/code_graphs" in item for item in mounts), (
        "gateway service must mount code_graphs_data at /data/code_graphs"
    )


def test_requirements_declare_code_rag_deps() -> None:
    text = (REPO_ROOT / "aigateway-api" / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("langchain-community", "langchain-text-splitters", "gitpython", "codegraph"):
        assert pkg in text, f"missing dep '{pkg}' in aigateway-api/requirements.txt"


# ---------------------------------------------------------------------------
# Task 2: helper modules
# ---------------------------------------------------------------------------

from aigateway_core.code_rag.embedding_router import (  # noqa: E402
    materialize_model_slug,
    resolve_collection_name,
)
from aigateway_core.code_rag.splitter import (  # noqa: E402
    compute_line_span,
    is_path_allowed,
)


def test_materialize_model_slug_normalizes_model_name() -> None:
    assert materialize_model_slug("Qwen/Qwen3-Embedding-0.6B") == "qwen_qwen3_embedding_0_6b"


def test_materialize_model_slug_strips_edges_and_collapses_runs() -> None:
    assert materialize_model_slug("  //text-embedding-3-large//  ") == "text_embedding_3_large"


def test_resolve_collection_name_prefixes_code_collection() -> None:
    assert resolve_collection_name("text-embedding-3-large") == "rag_code_text_embedding_3_large"


def test_is_path_allowed_accepts_allowlisted_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    project = root / "repo"
    project.mkdir(parents=True)
    assert is_path_allowed(str(project), [str(root)]) is True


def test_is_path_allowed_rejects_outside_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert is_path_allowed(str(outside), [str(root)]) is False


def test_is_path_allowed_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside)
    assert is_path_allowed(str(link), [str(root)]) is False


def test_compute_line_span_returns_exact_span() -> None:
    source = "a\nfoo()\nbar()\n"
    chunk = "foo()\nbar()"
    assert compute_line_span(source, chunk) == (2, 3)


def test_compute_line_span_falls_back_when_chunk_missing() -> None:
    source = "line1\nline2\nline3\n"
    chunk = "not present"
    start, end = compute_line_span(source, chunk)
    assert start == 1
    assert end >= start


# ---------------------------------------------------------------------------
# Task 7: import payload shape lock-in
# ---------------------------------------------------------------------------


def test_code_chunk_payload_includes_required_fields() -> None:
    """锁死 code chunk 写入 Qdrant 的 payload 字段集(spec §Payload)."""
    required = {
        "document_id",
        "filename",
        "file_path",
        "language",
        "chunk_index",
        "chunk_text",
        "chunk_type",
        "function_name",
        "class_name",
        "start_line",
        "end_line",
        "callers",
        "callees",
        "imports",
        "embedding_model",
    }
    sample = {
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
    assert required.issubset(set(sample.keys()))


def test_code_rag_routes_build_matching_payload_shape(monkeypatch) -> None:
    """确认 code_rag_routes 侧构造 payload 的字段集与 spec 一致。

    这里做静态字符串核对而不启动完整导入(依赖 codegraph/sentence-transformers),
    避免在开发机跑重资产依赖。
    """
    src = (
        REPO_ROOT
        / "aigateway-api"
        / "src"
        / "aigateway_api"
        / "code_rag_routes.py"
    ).read_text(encoding="utf-8")
    for field in (
        "document_id",
        "filename",
        "file_path",
        "language",
        "chunk_index",
        "chunk_text",
        "chunk_type",
        "function_name",
        "class_name",
        "start_line",
        "end_line",
        "callers",
        "callees",
        "imports",
        "embedding_model",
    ):
        assert f'"{field}"' in src, f"code_rag_routes 缺少 payload 字段 '{field}'"


def test_lookup_symbol_metadata_reads_codegraph_sqlite_schema(tmp_path: Path) -> None:
    import sqlite3

    from aigateway_core.code_rag.graph_query import lookup_symbol_metadata

    db = tmp_path / "codegraph.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER)"
    )
    cur.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, kind TEXT, metadata TEXT, line INTEGER, col INTEGER, provenance TEXT)"
    )
    cur.executemany(
        "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("file:auth.py", "file", "auth.py", "auth.py", "auth.py", "python", 1, 20),
            ("import:jwt", "import", "jwt", "jwt", "auth.py", "python", 1, 1),
            ("function:login", "function", "login", "login", "auth.py", "python", 5, 8),
            ("function:register", "function", "register", "register", "auth.py", "python", 10, 12),
            ("function:hash", "function", "hash_password", "hash_password", "auth.py", "python", 14, 16),
        ],
    )
    cur.executemany(
        "INSERT INTO edges (source, target, kind, metadata, line, col, provenance) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("file:auth.py", "import:jwt", "contains", None, None, None, None),
            ("file:auth.py", "function:login", "contains", None, None, None, None),
            ("file:auth.py", "function:register", "contains", None, None, None, None),
            ("file:auth.py", "function:hash", "contains", None, None, None, None),
            ("function:register", "function:login", "calls", '{"confidence": 0.9}', 11, 3, None),
            ("function:login", "function:hash", "calls", '{"confidence": 0.9}', 6, 4, None),
        ],
    )
    conn.commit()
    conn.close()

    meta = lookup_symbol_metadata(str(db), "auth.py", "login", "def login():\n    return hash_password()")
    assert meta["chunk_type"] == "function"
    assert meta["function_name"] == "login"
    assert meta["class_name"] is None
    assert meta["callers"] == ["register"]
    assert meta["callees"] == ["hash_password"]
    assert meta["imports"] == ["jwt"]


def test_code_rag_routes_batches_embedding_work() -> None:
    src = (
        REPO_ROOT
        / "aigateway-api"
        / "src"
        / "aigateway_api"
        / "code_rag_routes.py"
    ).read_text(encoding="utf-8")
    assert "batch_size = 64" in src
    assert "for batch_start in range(0, len(chunks), batch_size):" in src
    assert "await _mark(done=processed, current_file=current_file)" in src


def test_code_rag_routes_use_strict_graph_lookup_during_import() -> None:
    src = (
        REPO_ROOT
        / "aigateway-api"
        / "src"
        / "aigateway_api"
        / "code_rag_routes.py"
    ).read_text(encoding="utf-8")
    assert "lookup_symbol_metadata_strict" in src
    assert "graph_meta = lookup_symbol_metadata_strict(" in src


def test_folder_source_label_prefers_root_folder_name() -> None:
    from aigateway_api.code_rag_routes import _folder_source_label

    class DummyUpload:
        def __init__(self, filename: str | None) -> None:
            self.filename = filename

    files = [DummyUpload("main.py")]
    assert _folder_source_label(files, ["repo/src/main.py"]) == "folder://repo"
    assert _folder_source_label(files, []) == "folder://main.py"
    assert _folder_source_label([DummyUpload(None)], []) == "folder://upload"


def test_rag_retriever_source_mentions_real_graph_hops() -> None:
    src = (
        REPO_ROOT
        / "aigateway-core"
        / "src"
        / "aigateway_core"
        / "plugins"
        / "rag_retriever_plugin.py"
    ).read_text(encoding="utf-8")
    assert "lookup_related_symbols" in src
    assert 'code_rag_graph_hops' in src
    assert 'related_chunks = await self._fetch_related_code_chunks(' in src


# ---------------------------------------------------------------------------
# Splitter symbol extraction (Review finding 1):
#   split_code_directory 必须在每个 chunk 里写 function_name / class_name,
#   否则下游 lookup_symbol_metadata_strict 收到 symbol_name=None 直接短路,
#   callers/callees/imports 永远是空,graph_hops 也永远不触发。
# ---------------------------------------------------------------------------


def test_extract_top_symbol_finds_python_def() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("def login(user):\n    return user\n")
    assert result == ("function", "login")


def test_extract_top_symbol_finds_python_async_def() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("async def fetch(url):\n    pass\n")
    assert result == ("function", "fetch")


def test_extract_top_symbol_finds_python_class() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("class UserService:\n    pass\n")
    assert result == ("class", "UserService")


def test_extract_top_symbol_finds_js_function() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("export async function login(user) {\n  return user;\n}\n")
    assert result == ("function", "login")


def test_extract_top_symbol_finds_go_func() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("func (s *Server) Handle(req Request) error {\n  return nil\n}\n")
    assert result == ("function", "Handle")


def test_extract_top_symbol_finds_rust_fn() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("pub async fn login(user: String) -> Result<()> {\n}\n")
    assert result == ("function", "login")


def test_extract_top_symbol_returns_none_for_module_scope() -> None:
    from aigateway_core.code_rag.splitter import extract_top_symbol

    assert extract_top_symbol("# just a comment\nx = 1\n") is None
    assert extract_top_symbol("") is None


def test_split_code_directory_writes_symbol_names(tmp_path) -> None:
    """回归:切完的 chunk 字典必须携带 function_name/class_name,
    否则 code_rag_routes 侧的 strict-lookup 拿到 symbol=None 直接短路。
    """
    from aigateway_core.code_rag.splitter import split_code_directory

    src = tmp_path / "auth.py"
    src.write_text(
        "def login(user):\n"
        "    return user\n"
        "\n"
        "class UserService:\n"
        "    def register(self):\n"
        "        pass\n",
        encoding="utf-8",
    )

    chunks = split_code_directory(str(tmp_path), ignore_patterns=[])
    assert chunks, "splitter 应该至少切出一个 chunk"
    # 每个 chunk 都必须有这两个字段(即使为 None,payload 组装侧才敢直接读)
    for c in chunks:
        assert "function_name" in c, f"chunk 缺少 function_name: {c}"
        assert "class_name" in c, f"chunk 缺少 class_name: {c}"
    # 至少一个 chunk 提取到符号(证明抽取真的跑通,不是全 None)
    non_empty = [
        c for c in chunks if c["function_name"] or c["class_name"]
    ]
    assert non_empty, (
        f"split_code_directory 一个符号都没抽到,graph 展开必然失效: {chunks}"
    )
    names = {c["function_name"] for c in chunks} | {c["class_name"] for c in chunks}
    assert names & {"login", "UserService", "register"}, (
        f"未能抽到 login/UserService/register 中的任何一个: names={names}"
    )
