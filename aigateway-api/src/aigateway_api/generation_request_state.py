"""Shared terminal markers for request-id lifecycle records."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Request records reuse the existing ``draft_id`` slot so they remain compatible
# with the persisted request-index schema. Real draft IDs are UUID hex strings,
# therefore these descriptive markers cannot collide with generated drafts.
REQUEST_RECORD_NON_DRAFT = "request-terminal-non-draft"
REQUEST_RECORD_FAILED = "request-terminal-failed"


def terminal_request_status(record: Mapping[str, Any] | None) -> str | None:
    """Return the public terminal status encoded by a request record."""
    if not isinstance(record, Mapping):
        return None
    marker = str(record.get("draft_id") or "")
    if marker == REQUEST_RECORD_NON_DRAFT:
        return "non_draft"
    if marker == REQUEST_RECORD_FAILED:
        return "failed"
    return None


__all__ = [
    "REQUEST_RECORD_FAILED",
    "REQUEST_RECORD_NON_DRAFT",
    "terminal_request_status",
]
