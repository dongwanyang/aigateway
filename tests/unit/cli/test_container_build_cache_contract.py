"""Static contracts for reusable local container build caches."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_published_images_embed_inline_cache_for_compose_imports() -> None:
    workflow = (REPO_ROOT / ".github/workflows/docker-images.yml").read_text(
        encoding="utf-8"
    )

    # Gateway, ComfyUI and the control panel each publish an image consumed by
    # docker-compose.yml cache_from=type=registry. GHA-only caches are not
    # available to a fresh local BuildKit builder.
    assert workflow.count("type=inline") == 3
    assert workflow.count("type=gha,mode=max") == 3


def test_compose_registry_cache_is_configured_for_each_built_service() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )

    for service_name in ("gateway", "control-panel", "comfyui"):
        cache_from = compose["services"][service_name]["build"]["cache_from"]
        assert len(cache_from) == 1
        assert "type=registry" in cache_from[0]


def test_agent_guidance_uses_stateful_cached_rebuild_entrypoint() -> None:
    guidance = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")

    for document in (guidance, install):
        assert "--env-file .aigateway-install.env" in document or (
            "bash scripts/quickstart.sh" in document
            and "--distribution source" in document
            and "--build" in document
        )

    assert "Do not replace this with a bare `docker compose build`" in guidance
    assert "不要让自动化工具或编码代理默认执行以下命令" in install
