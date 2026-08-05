# Wan2.2 main integration record — 2026-08-05

## Scope

This integration brings the completed Wan2.2 PR1–PR5 aggregate branch into
`main`. Draft PR #40 (`agent/fix-wan22-design-compliance`) is explicitly
excluded.

The superseded staging branches were not merged independently because their
intended changes are already represented by the authoritative aggregate branch:

- `agent/pr3-rebase-staging`
- `agent/pr3-rebase-staging-2`
- `agent/pr5-request-fixes-e2e-rebuild`

## Conflict analysis

A real Git three-way merge against current `main` produced ten text conflicts
and no add/add or delete/modify conflicts.

The final rebuild resolved only the conflict marker blocks and retained all
non-conflicting hunks produced by Git:

- final PR1–PR5 behavior was selected in Wan2.2 API, planning, draft and chat UI
  conflict blocks;
- current `main` behavior was selected in the abort-aware polling conflict block;
- current `main` behavior was selected in GPU deployment conflict blocks to
  preserve PR #39 UUID migration, strict inventory validation, selector
  remapping and bind-mounted config inode locking.

The first coarse whole-file resolution was discarded after CI exposed that it
had removed valid auto-merged frontend changes. The integration branch was then
rebuilt from `main` with conflict-block-granular resolution.

## Validation gate

The resolved head must pass the repository Test Coverage, Security and Benchmark
Regression workflows before squash merge into `main`.
