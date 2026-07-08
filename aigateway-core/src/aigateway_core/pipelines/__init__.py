"""Two main pipelines (分).

See ``docs/superpowers/specs/2026-07-07-runtime-structure-design.md``.
The understanding and generation pipelines are sibling subpackages; each
groups the runtime plugins that already exist elsewhere in the tree.
"""
from aigateway_core.pipelines import understanding, generation  # noqa: F401

__all__ = ["understanding", "generation"]
