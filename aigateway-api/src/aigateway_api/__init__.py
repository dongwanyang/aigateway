"""
aigateway_api - AI Gateway API 服务层
====================================

FastAPI 应用，提供 OpenAI 兼容接口和管理接口。
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "1.0.0"


def _preload_cors_origins() -> None:
    """Expose ``server.cors_origins`` before the FastAPI app factory runs.

    ``main._create_app`` must register CORS middleware before lifespan creates a
    ``ConfigManager``. Reading only this small YAML value here keeps the source of
    truth in ``config.yaml`` and avoids falling through to localhost constants in
    normal deployments. An explicit environment value still has highest priority.
    """
    if os.environ.get("AI_GATEWAY_CORS_ORIGINS", "").strip():
        return

    config_path = Path(
        os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    ).expanduser()
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (ImportError, OSError, yaml.YAMLError):
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
        os.environ["AI_GATEWAY_CORS_ORIGINS"] = ",".join(normalized)


_preload_cors_origins()
