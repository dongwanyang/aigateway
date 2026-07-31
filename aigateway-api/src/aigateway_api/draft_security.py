"""Fail-closed ownership policy for persisted draft artifacts."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def assert_draft_owner(
    draft: Any,
    auth: dict[str, Any],
    *,
    action: str,
) -> None:
    """Require persisted owner metadata before any interactive draft action."""
    draft_user_id = str(getattr(draft, "user_id", None) or "")
    draft_group_id = str(getattr(draft, "group_id", None) or "")
    if not draft_user_id and not draft_group_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": (
                        "Draft ownership metadata is missing; legacy drafts "
                        "must expire or be migrated before access"
                    ),
                }
            },
        )
    if not isinstance(auth, dict):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": f"Only the draft owner can {action}",
                }
            },
        )

    auth_user_id = str(auth.get("user_id") or "")
    auth_group_id = str(auth.get("group_id") or "")
    if (
        (draft_user_id and draft_user_id != auth_user_id)
        or (draft_group_id and draft_group_id != auth_group_id)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": f"Only the draft owner can {action}",
                }
            },
        )


__all__ = ["assert_draft_owner"]
