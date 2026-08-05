# PR #40 document-guided integration

Date: 2026-08-05

This integration resolves PR #40 against `main` using the 2026-08-04 Wan2.2 progressive-video implementation plan as the acceptance authority.

## Preserved implementation goals

- The keyframe confirmation step remains a hard gate: Wan2.2 is not submitted before confirmation.
- `keyframe_prompt` and `motion_prompt` remain separate, and language handling is model-capability-driven.
- Uploaded images and `source_draft_id` results are copied and frozen as the video keyframe.
- Frozen keyframe bytes are verified with SHA-256 at confirmation.
- Duration, FPS, frame count, source identity and workflow version remain part of the draft snapshot.
- Missing image references fail with an actionable error rather than silently generating an unrelated keyframe.
- Current user messages and reference images are sent once.
- Confirmation is idempotent and request recovery/cancellation remains owner- and session-scoped.
- Runtime commit identity and privacy-preserving workflow observability remain available.

## Conflict policy

Conflict blocks implementing the plan were retained from PR #40. Current `main` was retained for unrelated GPU-topology, configuration-locking and deployment-renderer safety changes. Non-conflicting hunks from both histories were preserved.

## Validation boundary

Repository CI is required on the resulting head. Physical single-GPU ComfyUI/Wan2.2 history, output-duration and visible-motion acceptance is not claimed by this repository-only integration record.
