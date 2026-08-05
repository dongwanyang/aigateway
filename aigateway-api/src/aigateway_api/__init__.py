"""
aigateway_api - AI Gateway API 服务层
=====================================

FastAPI 应用，提供 OpenAI 兼容接口和管理接口。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

__version__ = "1.0.0"
_CORS_YAML_BOOTSTRAP_MARKER = (
    "AI_GATEWAY_CORS_ORIGINS_BOOTSTRAPPED_FROM_YAML"
)


def _ensure_core_src() -> None:
    """Support repository-source execution before importing core modules."""
    core_src = Path(__file__).resolve().parents[3] / "aigateway-core" / "src"
    if core_src.is_dir() and str(core_src) not in sys.path:
        sys.path.insert(0, str(core_src))


def _reconcile_gpu_topology() -> None:
    """Refresh stale GPU UUID state before CUDA-aware modules are imported."""
    if os.environ.get("AI_GATEWAY_ACCELERATOR", "").strip().lower() != "cuda":
        return
    # ``-1`` is an explicit operator request to keep Gateway off CUDA. The host
    # topology controller can still reconcile local ComfyUI workers independently.
    if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "-1":
        return
    from .gpu_topology_bootstrap import bootstrap_gpu_topology

    bootstrap_gpu_topology()


def _allow_config_precondition_header() -> None:
    """Extend Starlette's CORS allow-list before middleware construction."""
    try:
        from starlette.middleware import cors
    except ImportError:
        return
    headers = set(getattr(cors, "SAFELISTED_HEADERS", set()))
    headers.add("If-Match")
    cors.SAFELISTED_HEADERS = headers


def _dotenv_bootstrap_values() -> dict[str, Any]:
    """Read bootstrap-only values from .env without mutating ``os.environ``."""
    try:
        from dotenv import dotenv_values, find_dotenv
    except ImportError:
        return {}

    dotenv_path = find_dotenv(usecwd=True)
    if not dotenv_path:
        return {}
    try:
        return dict(dotenv_values(dotenv_path))
    except OSError:
        return {}


def _preload_cors_origins() -> None:
    """Expose CORS origins before the FastAPI app factory adds middleware.

    A process or .env value is a real environment override. A YAML value is only
    copied temporarily because middleware is constructed before the lifespan
    ConfigManager exists; ConfigManager consumes the marker and removes the
    synthetic environment value before loading runtime configuration.
    """
    if os.environ.get("AI_GATEWAY_CORS_ORIGINS", "").strip():
        return

    dotenv_values = _dotenv_bootstrap_values()
    dotenv_cors = str(
        dotenv_values.get("AI_GATEWAY_CORS_ORIGINS") or ""
    ).strip()
    if dotenv_cors:
        os.environ["AI_GATEWAY_CORS_ORIGINS"] = dotenv_cors
        return

    try:
        import yaml
    except ImportError:
        return

    config_path_value = (
        os.environ.get("AI_GATEWAY_CONFIG_PATH", "").strip()
        or str(dotenv_values.get("AI_GATEWAY_CONFIG_PATH") or "").strip()
        or "./config.yaml"
    )
    config_path = Path(config_path_value).expanduser()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return

    server = raw.get("server", {}) if isinstance(raw, dict) else {}
    origins = server.get("cors_origins") if isinstance(server, dict) else None
    if not isinstance(origins, list):
        return

    normalized = [
        value.strip()
        for value in origins
        if isinstance(value, str) and value.strip()
    ]
    if normalized:
        bootstrap_origins = ",".join(normalized)
        os.environ["AI_GATEWAY_CORS_ORIGINS"] = bootstrap_origins
        # Store the synthetic value itself. ConfigManager can then distinguish
        # it from an operator/test override written after package import.
        os.environ[_CORS_YAML_BOOTSTRAP_MARKER] = bootstrap_origins


def _install_admin_security_guards() -> None:
    """Install sensitive-route, transactional-config and ownership replacements."""
    from . import admin_routes, security_routes
    from .config_management_routes import install_config_management_routes
    from .draft_security import assert_draft_owner
    from .model_reference_cleanup import (
        configured_model_names,
        prune_removed_model_references,
    )

    # Secure route handlers resolve these globals at request time. Replacing the
    # narrow legacy helpers here preserves their public test/import surface while
    # installing the complete scalar-reference validation contract.
    security_routes._configured_model_names = configured_model_names
    security_routes._prune_removed_model_references = (
        prune_removed_model_references
    )
    security_routes.install_security_routes(admin_routes.router)
    install_config_management_routes(admin_routes.router)
    admin_routes._assert_draft_owner = assert_draft_owner


def _install_gpu_routes() -> None:
    """Install authenticated GPU diagnostics and memory-release endpoints."""
    from . import admin_routes
    from .gpu_routes import install_gpu_routes

    install_gpu_routes(admin_routes.router)


def _install_config_schema_parser() -> None:
    """Install YAML-aware schema parsing and remove the legacy write route."""
    from . import routes
    from .config_schema import parse_template_schema

    routes._parse_template_schema = parse_template_schema
    routes.router.routes[:] = [
        route
        for route in routes.router.routes
        if not (
            getattr(route, "path", None) == "/admin/config/table"
            and "PUT" in set(getattr(route, "methods", set()) or set())
        )
    ]


_ensure_core_src()
_reconcile_gpu_topology()
_allow_config_precondition_header()
_preload_cors_origins()
_install_admin_security_guards()
_install_gpu_routes()
_install_config_schema_parser()
