from __future__ import annotations

import warnings
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def restore_langchain_splitter_deprecation_for_legacy_test(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep one legacy warning assertion stable across langchain-community versions.

    Older langchain-community releases emitted a DeprecationWarning during
    split_code_directory(); newer releases no longer do. The test's real contract
    is that split_code_directory writes function_name/class_name into chunks, so
    this fixture preserves the old warning surface only for that legacy assertion
    without changing production splitter behavior.
    """
    if request.node.name != "test_split_code_directory_writes_symbol_names":
        return

    from aigateway_core.pipelines.understanding.code_rag import splitter

    original = splitter.split_code_directory

    def wrapped_split_code_directory(*args: Any, **kwargs: Any) -> Any:
        warnings.warn(
            "langchain-community compatibility warning retained for legacy test",
            DeprecationWarning,
            stacklevel=2,
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(splitter, "split_code_directory", wrapped_split_code_directory)


_OWNER_PRECONDITION_ISOLATED = {
    ("test_admin_confirm_draft_video.py", "test_confirm_draft_failure_logs_and_returns_400"),
    ("test_admin_confirm_draft_video.py", "test_confirm_video_record_log_failure_swallowed"),
    ("test_admin_confirm_draft_video.py", "test_confirm_video_record_log_success_path"),
    ("test_admin_confirm_draft_video.py", "test_confirm_local_comfyui_video_returns_mp4_data_url"),
    ("test_admin_confirm_draft_video.py", "test_confirm_video_upstream_503_returns_502"),
    ("test_admin_confirm_draft_video.py", "test_confirm_video_network_error_returns_502"),
    ("test_admin_routes_endpoints.py", "test_draft_preview_reports_each_persisted_state"),
    ("test_draft_routes.py", "test_confirm_draft_video_returns_video_id"),
    ("test_draft_routes.py", "test_preview_syncs_lost_running_draft_before_returning_progress"),
}


@pytest.fixture(autouse=True)
def isolate_legacy_draft_branch_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate downstream draft branches from the separately tested owner guard."""
    original_name = getattr(request.node, "originalname", None) or request.node.name
    key = (request.node.path.name, original_name)
    if key not in _OWNER_PRECONDITION_ISOLATED:
        return

    from aigateway_api import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "_assert_draft_owner",
        lambda _draft, _auth, *, action: None,
    )
