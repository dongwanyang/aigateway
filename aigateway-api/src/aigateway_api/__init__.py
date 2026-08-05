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
    headers.update({"If-Match", "X-Request-ID"})
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
    """Expose CORS origins before the FastAPI app factory adds middleware."""
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
        os.environ[_CORS_YAML_BOOTSTRAP_MARKER] = bootstrap_origins


def _install_unified_source_contract() -> None:
    """Extend the chat request model with the documented source draft field."""
    from .unified_source_contract import install_unified_source_contract

    install_unified_source_contract()


def _install_admin_security_guards() -> None:
    """Install sensitive-route, transactional-config and ownership replacements."""
    from . import admin_routes, security_routes
    from .config_management_routes import install_config_management_routes
    from .draft_security import assert_draft_owner
    from .model_reference_cleanup import (
        configured_model_names,
        prune_removed_model_references,
    )

    security_routes._configured_model_names = configured_model_names
    security_routes._prune_removed_model_references = (
        prune_removed_model_references
    )
    security_routes.install_security_routes(admin_routes.router)
    install_config_management_routes(admin_routes.router)
    admin_routes._assert_draft_owner = assert_draft_owner


def _install_draft_confirm_routes() -> None:
    """Replace legacy confirmation with stable errors and fail-closed ownership."""
    from . import admin_routes
    from .draft_confirm_routes import install_draft_confirm_routes

    install_draft_confirm_routes(admin_routes.router)


def _install_draft_request_routes() -> None:
    """Install request recovery and cancellation routes."""
    from . import admin_routes
    from .draft_request_routes import install_draft_request_routes

    install_draft_request_routes(admin_routes.router)


def _install_verified_draft_cancellation() -> None:
    """Require ComfyUI prompt release before persisting cancelled."""
    from .verified_draft_cancellation import (
        install_verified_draft_cancellation,
    )

    install_verified_draft_cancellation()


def _install_draft_rejection_lifecycle() -> None:
    """Move request recovery atomically to regenerated drafts."""
    from .draft_rejection_lifecycle import install_draft_rejection_lifecycle

    install_draft_rejection_lifecycle()


def _install_gpu_queue_handoff() -> None:
    """Prevent idle reservation from blocking FIFO generation handoff."""
    from .gpu_queue_handoff import install_gpu_queue_handoff

    install_gpu_queue_handoff()


def _install_gpu_routes() -> None:
    """Install authenticated GPU diagnostics and memory-release endpoints."""
    from . import admin_routes
    from .gpu_routes import install_gpu_routes

    install_gpu_routes(admin_routes.router)


def _install_source_draft_video_routes() -> None:
    """Install the authenticated existing-image-to-video endpoint exactly once."""
    from . import admin_routes
    from .source_draft_video_routes import router as source_draft_video_router

    route_path = "/draft/{source_draft_id}/video"
    if any(
        getattr(route, "path", None) == route_path
        for route in admin_routes.router.routes
    ):
        return

    source_routes = [
        route
        for route in source_draft_video_router.routes
        if getattr(route, "path", None) == route_path
    ]
    if not source_routes:
        available = sorted(
            str(getattr(route, "path", ""))
            for route in source_draft_video_router.routes
        )
        raise RuntimeError(
            f"source_draft_video_route_definition_missing:{available}"
        )

    admin_routes.router.routes.extend(source_routes)

    if not any(
        getattr(route, "path", None) == route_path
        for route in admin_routes.router.routes
    ):
        raise RuntimeError("source_draft_video_route_install_failed")


def _install_video_request_guards() -> None:
    """Install request-bound progressive video semantic validation."""
    from .video_request_guard import install_video_request_guard

    install_video_request_guard()


def _install_video_generation_observability() -> None:
    """Install privacy-preserving Wan workflow submission logging."""
    from .video_generation_observability import (
        install_video_generation_observability,
    )

    install_video_generation_observability()


def _install_runtime_identity() -> None:
    """Add commit/image identity to the health response."""
    from . import routes
    from .runtime_identity import install_runtime_identity

    install_runtime_identity(routes.router)


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
_install_unified_source_contract()
_install_video_request_guards()
_install_video_generation_observability()
_install_verified_draft_cancellation()
_install_draft_rejection_lifecycle()
_install_admin_security_guards()
_install_draft_confirm_routes()
_install_draft_request_routes()
_install_gpu_queue_handoff()
_install_gpu_routes()
_install_runtime_identity()
_install_config_schema_parser()
# Install this route last because package bootstrap imports can mutate routers.
_install_source_draft_video_routes()
