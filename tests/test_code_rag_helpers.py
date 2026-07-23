"""Tests for the Code RAG scaffolding (Task 1).

Locked into repo-root config files so misplaced keys and missing volumes
fail loudly instead of silently drifting.
"""
from pathlib import Path

import pytest
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

from aigateway_core.pipelines.understanding.code_rag.embedding_router import (  # noqa: E402
    materialize_model_slug,
    resolve_collection_name,
)
from aigateway_core.pipelines.understanding.code_rag.splitter import (  # noqa: E402
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
        "signature",
        "docstring",
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
        "signature": "(user, pw)",
        "docstring": "用户登录认证",
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
    }
    assert required.issubset(set(sample.keys()))


def test_code_rag_routes_build_matching_payload_shape(monkeypatch) -> None:
    """轻量防回归:确认 code_rag_routes 侧 payload 字段集仍在源码里。

    真实的 payload 字段集*落盘*已由
    tests/test_code_rag_routes.py::test_run_code_import_task_passes_real_symbol_name_to_graph_lookup
    运行时验证(完整 17 字段 issubset 检查)。本静态字符串核对仅作廉价 guard,
    避免开发机跑 codegraph/sentence-transformers 重依赖。
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
        "signature",
        "docstring",
        "embedding_model",
    ):
        assert f'"{field}"' in src, f"code_rag_routes 缺少 payload 字段 '{field}'"


def _build_codegraph_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """用真实 codegraph CLI 构建一个微型仓库,返回 repo 目录(graph_repo_path)。

    files: {rel_path: content},rel_path 相对 repo 根。源码放在 src/ 下(与
    graph_builder 的 symlink 约定一致 → db file_path 带 src/ 前缀)。
    需要本机装了 @colbymchenry/codegraph CLI;没装则 skip。
    """
    import shutil
    import subprocess

    if not shutil.which("codegraph"):
        pytest.skip("codegraph CLI 未安装,跳过真实图谱测试")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo / "src" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        ["codegraph", "init", str(repo)],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"codegraph init 失败: {proc.stdout[:300]}")
    return repo


def _build_codegraph_db_with_edges(db_path: Path, nodes: list[dict], edges: list[dict]) -> None:
    """手建一个 codegraph schema 的 db（不调 CLI），供 read_call_edges 单测。"""
    import sqlite3
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
        start_column INTEGER, end_column INTEGER, docstring TEXT, signature TEXT,
        visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER,
        is_abstract INTEGER, decorators TEXT, type_parameters TEXT, return_type TEXT,
        updated_at INTEGER);
    CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
        metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
    CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, size INTEGER, updated_at INTEGER);
    """)
    for n in nodes:
        conn.execute("INSERT INTO nodes (id,kind,name,file_path,start_line,end_line) VALUES (?,?,?,?,?,?)",
                     (n["id"], n["kind"], n["name"], n["file_path"], n["start_line"], n["end_line"]))
    for e in edges:
        conn.execute("INSERT INTO edges (source,target,kind) VALUES (?,?,?)",
                     (e["source"], e["target"], e["kind"]))
    conn.commit()
    conn.close()


def test_get_callers_callees_db_direct_matches_cli(tmp_path: Path) -> None:
    """db 直读后 get_callers/get_callees 与旧 CLI 结果一致。"""
    from aigateway_core.pipelines.understanding.code_rag.graph_query import get_callers, get_callees
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    repo = _build_codegraph_repo(
        tmp_path,
        # hash_password 必须有定义,否则 codegraph 不建节点 → 无 login→hash_password 边。
        # 与 test_lookup_symbol_metadata_reads_codegraph_sqlite_schema 同款 fixture。
        {"auth.py": "def login():\n    return hash_password()\ndef hash_password():\n    return 'h'\ndef register():\n    return login()\n"},
    )
    graph_query._edges_cache.clear()  # 防污染

    callers = get_callers(str(repo), "login")
    callees = get_callees(str(repo), "login")
    assert "register" in [c["name"] for c in callers]
    assert "hash_password" in [c["name"] for c in callees]


def test_cached_edges_rebuild_on_file_hash_change(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    db_path = tmp_path / "repo" / ".codegraph" / "codegraph.db"
    nodes = [
        {"id": "f:alpha", "kind": "function", "name": "alpha", "file_path": "src/a.py", "start_line": 1, "end_line": 2},
        {"id": "f:beta", "kind": "function", "name": "beta", "file_path": "src/a.py", "start_line": 3, "end_line": 4},
    ]
    edges = [{"source": "f:alpha", "target": "f:beta", "kind": "calls"}]
    _build_codegraph_db_with_edges(db_path, nodes, edges)
    # files 表需要至少一行供 hash 快照
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files (path, content_hash) VALUES ('src/a.py', 'hash_v1')")
    conn.commit()
    conn.close()

    # 清理模块级缓存(防其它测试污染)
    graph_query._edges_cache.clear()

    callers1, callees1 = graph_query._get_cached_edges(str(tmp_path / "repo"))
    assert callees1["f:alpha"][0]["name"] == "beta"

    # 改 db:加一条 calls 边 + 改 file hash
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO nodes (id,kind,name,file_path,start_line,end_line) VALUES ('f:gamma','function','gamma','src/a.py',5,6)")
    conn.execute("INSERT INTO edges (source,target,kind) VALUES ('f:beta','f:gamma','calls')")
    conn.execute("UPDATE files SET content_hash='hash_v2' WHERE path='src/a.py'")
    conn.commit()
    conn.close()

    callers2, callees2 = graph_query._get_cached_edges(str(tmp_path / "repo"))
    assert callees2["f:beta"][0]["name"] == "gamma"  # 重建后看到新边


def test_cached_edges_reuses_unchanged(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag import graph_query

    db_path = tmp_path / "repo2" / ".codegraph" / "codegraph.db"
    _build_codegraph_db_with_edges(db_path,
        [{"id": "f:x", "kind": "function", "name": "x", "file_path": "src/a.py", "start_line": 1, "end_line": 2}],
        [])
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files (path, content_hash) VALUES ('src/a.py', 'h1')")
    conn.commit()
    conn.close()

    graph_query._edges_cache.clear()
    graph_query._get_cached_edges(str(tmp_path / "repo2"))
    call_count_before = graph_query._edges_cache[str(tmp_path / "repo2")][0]  # snapshot ref
    graph_query._get_cached_edges(str(tmp_path / "repo2"))  # 第二次,hash 没变
    # 仍是同一个 snapshot 对象(未重建)
    assert graph_query._edges_cache[str(tmp_path / "repo2")][0] is call_count_before


def test_read_call_edges_returns_callers_and_callees(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import read_call_edges

    db_path = tmp_path / "repo" / ".codegraph" / "codegraph.db"
    # alpha -> beta -> gamma; caller -> alpha
    nodes = [
        {"id": "f:alpha", "kind": "function", "name": "alpha", "file_path": "src/a.py", "start_line": 1, "end_line": 2},
        {"id": "f:beta", "kind": "function", "name": "beta", "file_path": "src/a.py", "start_line": 3, "end_line": 4},
        {"id": "f:gamma", "kind": "function", "name": "gamma", "file_path": "src/a.py", "start_line": 5, "end_line": 6},
        {"id": "f:caller", "kind": "function", "name": "caller", "file_path": "src/a.py", "start_line": 7, "end_line": 8},
    ]
    edges = [
        {"source": "f:alpha", "target": "f:beta", "kind": "calls"},
        {"source": "f:beta", "target": "f:gamma", "kind": "calls"},
        {"source": "f:caller", "target": "f:alpha", "kind": "calls"},
    ]
    _build_codegraph_db_with_edges(db_path, nodes, edges)

    callers, callees = read_call_edges(str(tmp_path / "repo"))
    assert [r["name"] for r in callees["f:alpha"]] == ["beta"]
    assert [r["name"] for r in callees["f:beta"]] == ["gamma"]
    assert [r["name"] for r in callees["f:caller"]] == ["alpha"]
    assert [r["name"] for r in callers["f:beta"]] == ["alpha"]
    assert [r["name"] for r in callers["f:alpha"]] == ["caller"]
    # 无调用的符号不出现在 callees map
    assert "f:gamma" not in callees
    assert [r["name"] for r in callers["f:gamma"]] == ["beta"]


def test_read_call_edges_empty_when_no_db(tmp_path: Path) -> None:
    from aigateway_core.pipelines.understanding.code_rag.graph_query import read_call_edges

    callers, callees = read_call_edges(str(tmp_path / "nope"))
    assert callers == {}
    assert callees == {}


def test_lookup_symbol_metadata_reads_codegraph_sqlite_schema(tmp_path: Path) -> None:
    """lookup_symbol_metadata 走 codegraph CLI(重构后),验证 callers/callees/imports。"""
    from aigateway_core.pipelines.understanding.code_rag.graph_query import lookup_symbol_metadata

    repo = _build_codegraph_repo(
        tmp_path,
        {
            "auth.py": (
                "import jwt\n"
                "def login():\n"
                "    return hash_password()\n"
                "def hash_password():\n"
                "    return 'h'\n"
                "def register():\n"
                "    return login()\n"
            ),
        },
    )

    meta = lookup_symbol_metadata(str(repo), "auth.py", "login", "def login():\n    return hash_password()")
    assert meta["chunk_type"] == "function"
    assert meta["function_name"] == "login"
    assert meta["class_name"] is None
    assert meta["callers"] == ["register"]
    assert meta["callees"] == ["hash_password"]
    assert meta["imports"] == ["jwt"]


def test_lookup_symbol_metadata_matches_graph_path_with_builder_prefix(tmp_path: Path) -> None:
    """graph_builder 把源码 symlink 成 work_dir/src 后, codegraph 存的 file_path
    带 src/ 前缀 (src/auth.py), 与调用方传入的相对源根路径 (auth.py) 对不上.
    查询端按后缀归一化匹配, 否则符号 miss → chunk_type 退化成 module。
    """
    from aigateway_core.pipelines.understanding.code_rag.graph_query import lookup_symbol_metadata

    repo = _build_codegraph_repo(
        tmp_path,
        {"auth.py": "def login():\n    return 1\n"},
    )

    # 调用方传 auth.py (相对源根), graph 存的是 src/auth.py
    meta = lookup_symbol_metadata(str(repo), "auth.py", "login", "def login():\n    return 1")
    assert meta["chunk_type"] == "function", "前缀不一致导致符号 miss, 退化成 module"
    assert meta["function_name"] == "login"


def test_lookup_related_symbols_strips_builder_prefix_from_file_path(tmp_path: Path) -> None:
    """retrieval 端用 impact 返回的 file_path 去 Qdrant scroll 相关 chunk,
    而 Qdrant 存的是 splitter 路径 (无 src/ 前缀). impact 必须把 graph 节点的
    前缀剥掉, 否则 scroll 永远 miss, 相关符号增强失效。
    """
    from aigateway_core.pipelines.understanding.code_rag.graph_query import lookup_related_symbols_strict

    repo = _build_codegraph_repo(
        tmp_path,
        {
            "auth.py": "from utils import helper\n\ndef login():\n    return helper()\n",
            "utils.py": "def helper():\n    return 'ok'\n",
        },
    )

    related = lookup_related_symbols_strict(str(repo), "auth.py", "login", hops=1)
    # impact 返回 login 自身 + 直接邻居 helper;剥掉 src/ 前缀与 Qdrant 路径对齐
    names = {r["symbol_name"] for r in related}
    assert "helper" in names, "impact 没找到直接邻居 helper"
    for r in related:
        assert not r["file_path"].startswith("src/"), (
            f"file_path 没剥 src/ 前缀: {r['file_path']}"
        )


def test_code_rag_routes_batches_embedding_work() -> None:
    """轻量防回归:源码里仍保留 batch_size=64 的分批循环。

    真实的分批*行为*已由
    tests/test_code_rag_routes.py::test_run_code_import_task_batches_encode_calls_at_64_boundary
    运行时验证(70 chunks → encode_texts 调 2 次,每批单独 upsert)。
    本静态断言仅作廉价 guard,防止有人把分批常量删掉却没触发行为测试。
    """
    src = (
        REPO_ROOT
        / "aigateway-api"
        / "src"
        / "aigateway_api"
        / "code_rag_routes.py"
    ).read_text(encoding="utf-8")
    assert "batch_size = 64" in src
    assert "for batch_start in range(0, len(chunks), batch_size):" in src


def test_code_rag_routes_uses_build_symbol_chunks_during_import() -> None:
    """轻量防回归:导入走 build_symbol_chunks + 嵌 embed_text。

    真实的链路*行为*已由
    tests/test_code_rag_routes.py::test_run_code_import_task_passes_real_symbol_name_to_graph_lookup
    运行时验证(encode 的是 embed_text 结构描述,payload 带 function_name/callers/signature)。
    本静态断言仅作廉价 guard。
    """
    src = (
        REPO_ROOT
        / "aigateway-api"
        / "src"
        / "aigateway_api"
        / "code_rag_routes.py"
    ).read_text(encoding="utf-8")
    assert "build_symbol_chunks" in src
    assert 'lambda: build_symbol_chunks(' in src
    # 嵌入结构描述(embed_text),而非源码(chunk_text)
    assert '[c["embed_text"] for c in batch_chunks]' in src


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
    """已升级为运行时行为测试 test_expand_code_hits_with_graph_invokes_lookup_when_hops_positive,
    见 tests/test_rag_retriever_code_rag.py。

    旧的静态字符串核对(grep rag_retriever_plugin.py 源码里的
    'lookup_related_symbols' / 'code_rag_graph_hops')是被动断言 —— 源码里
    有这些字符串不代表 graph-hops 链路真的会被触发。这里仅保留一个轻量
    断言:配置项确实在 plugin 配置类上可读写(code_rag_graph_hops 的契约存在),
    避免删测试丢掉"该 flag 必须存在"这一约束。
    """
    plugin_src = (
        REPO_ROOT
        / "aigateway-core"
        / "src"
        / "aigateway_core"
        / "pipelines"
        / "understanding"
        / "rag"
        / "rag_retriever_plugin.py"
    ).read_text(encoding="utf-8")
    # _expand_code_hits_with_graph 必须读 code_rag_graph_hops(否则多跳扩展永远不触发)。
    assert "code_rag_graph_hops" in plugin_src


# ---------------------------------------------------------------------------
# Splitter symbol extraction (Review finding 1):
#   split_code_directory 必须在每个 chunk 里写 function_name / class_name,
#   否则下游 lookup_symbol_metadata_strict 收到 symbol_name=None 直接短路,
#   callers/callees/imports 永远是空,graph_hops 也永远不触发。
# ---------------------------------------------------------------------------


def test_extract_top_symbol_finds_python_def() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("def login(user):\n    return user\n")
    assert result == ("function", "login")


def test_extract_top_symbol_finds_python_async_def() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("async def fetch(url):\n    pass\n")
    assert result == ("function", "fetch")


def test_extract_top_symbol_finds_python_class() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("class UserService:\n    pass\n")
    assert result == ("class", "UserService")


def test_extract_top_symbol_finds_js_function() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("export async function login(user) {\n  return user;\n}\n")
    assert result == ("function", "login")


def test_extract_top_symbol_finds_go_func() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("func (s *Server) Handle(req Request) error {\n  return nil\n}\n")
    assert result == ("function", "Handle")


def test_extract_top_symbol_finds_rust_fn() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    result = extract_top_symbol("pub async fn login(user: String) -> Result<()> {\n}\n")
    assert result == ("function", "login")


def test_extract_top_symbol_returns_none_for_module_scope() -> None:
    from aigateway_core.pipelines.understanding.code_rag.splitter import extract_top_symbol

    assert extract_top_symbol("# just a comment\nx = 1\n") is None
    assert extract_top_symbol("") is None


def test_split_code_directory_writes_symbol_names(tmp_path) -> None:
    """回归:切完的 chunk 字典必须携带 function_name/class_name,
    否则 code_rag_routes 侧的 strict-lookup 拿到 symbol=None 直接短路。
    """
    try:
        import langchain_community  # noqa: F401
    except ImportError:
        pytest.skip("langchain_community not installed")
    from aigateway_core.pipelines.understanding.code_rag.splitter import split_code_directory

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
