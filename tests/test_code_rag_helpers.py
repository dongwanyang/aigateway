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
