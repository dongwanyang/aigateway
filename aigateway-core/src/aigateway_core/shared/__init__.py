"""Cross-layer utilities."""

from . import config as _config
from .configured_config import ConfigManager
from .qdrant_client import QdrantClientManager

# Package initialization runs before ``shared.config`` is returned to callers,
# so every normal canonical import observes the environment-aware manager.
_config.ConfigManager = ConfigManager

__all__ = ["ConfigManager", "QdrantClientManager"]
