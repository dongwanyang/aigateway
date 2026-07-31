"""Cross-layer utilities."""

from . import config as _config
from .configured_config import ConfigManager

# Python imports the package before resolving ``shared.config``. Publishing the
# environment-aware class here therefore keeps the canonical import path
# consistent for API, CLI, tests and external consumers.
_config.ConfigManager = ConfigManager

from .qdrant_client import QdrantClientManager

__all__ = ["ConfigManager", "QdrantClientManager"]
