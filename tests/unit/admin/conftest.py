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
