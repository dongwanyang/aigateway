"""Progressive video metrics must be emitted from real call sites.

Implementation plan section 9.3 asks for metrics that explain "the subject
changed / the length is wrong". The two that answer the reference-image failure
mode are ``video_reference_source_total`` (a spike in ``generated_keyframe`` for
image-to-video means reference images are being dropped) and
``video_keyframe_integrity_mismatch_total``.
"""
from __future__ import annotations

import hashlib
import time

import pytest
from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError
from aigateway_core.pipelines.generation._common.metrics import (
    reset_prometheus_registry,
)
from aigateway_core.pipelines.generation._common.models import (
    DRAFT_STATUS_PENDING,
    DraftResult,
)
from aigateway_core.pipelines.generation.draft.draft_generator import (
    DraftGeneratorStrategy,
)
from prometheus_client import generate_latest


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_prometheus_registry()
    yield
    reset_prometheus_registry()


def _exported() -> str:
    from aigateway_core.pipelines.generation._common.metrics import (
        get_prometheus_registry,
    )

    return generate_latest(
        get_prometheus_registry()._collector_registry
    ).decode()


def _video_draft(preview: bytes, **params):
    return DraftResult(
        draft_id="draft-video",
        previews=[preview],
        generation_params={"trace_id": "t", **params},
        created_at=time.time(),
        expires_at=time.time() + 3600,
        attempt_number=1,
        max_attempts=5,
        status=DRAFT_STATUS_PENDING,
        media_type="video",
    )


@pytest.mark.parametrize(
    ("params", "expected_kind"),
    [
        ({}, "generated_keyframe"),
        ({"has_reference_image": True}, "uploaded"),
        ({"source_draft_id": "img-1"}, "source_draft"),
    ],
)
def test_freezing_a_keyframe_attributes_its_origin(params, expected_kind):
    draft = _video_draft(b"keyframe-bytes", **params)

    DraftGeneratorStrategy._freeze_video_keyframe(draft)

    assert draft.generation_params["source_kind"] == expected_kind
    exported = _exported()
    assert (
        f'gen_opt_video_reference_source_total{{source_kind="{expected_kind}"}} 1.0'
        in exported
    )
    assert 'gen_opt_video_keyframe_total{outcome="success"} 1.0' in exported


def test_integrity_mismatch_is_counted_and_still_fails_closed():
    preview = b"approved-keyframe"
    draft = _video_draft(preview)
    DraftGeneratorStrategy._freeze_video_keyframe(draft)
    # Someone swapped the frozen bytes after the user approved them.
    draft.previews = [b"tampered-keyframe"]

    with pytest.raises(DraftWorkflowError, match="video_keyframe_integrity_mismatch"):
        DraftGeneratorStrategy._validate_frozen_video_keyframe(draft)

    assert "gen_opt_video_keyframe_integrity_mismatch_total 1.0" in _exported()


def test_unmodified_keyframe_passes_validation_without_counting_a_mismatch():
    preview = b"approved-keyframe"
    draft = _video_draft(preview)
    DraftGeneratorStrategy._freeze_video_keyframe(draft)

    assert draft.generation_params["source_image_sha256"] == (
        hashlib.sha256(preview).hexdigest()
    )
    DraftGeneratorStrategy._validate_frozen_video_keyframe(draft)

    assert "gen_opt_video_keyframe_integrity_mismatch_total 0.0" in _exported()
