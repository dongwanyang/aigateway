# Gateway Runtime Base Image Design

Date: 2026-07-07
Status: Draft approved for planning

## Summary

Split the current all-in-one gateway image into two layers without changing runtime behavior:

1. `gateway-runtime-full` — a prebuilt runtime base image that contains all heavy, low-churn dependencies required by the current full gateway deployment.
2. `gateway-app-full` — a thin application image built on top of `gateway-runtime-full` that copies only the frequently changing Python source code and keeps the same Uvicorn startup behavior.

This first phase targets Docker build and image distribution cost only. It does not introduce a new service, does not change embedding execution mode, and does not alter API behavior.

## Problem

The current gateway image mixes low-churn heavy dependencies with fast-changing application code in a single build path. The heavy parts include:

- system packages for code graphing, OCR, and media processing
- `torch`
- Python requirements installation
- global `@colbymchenry/codegraph` install
- predownload of `Qwen/Qwen3-Embedding-0.6B`

As a result, rebuilding the gateway image for normal application changes is slower and more expensive than necessary, especially for the current full deployment path.

## Goals

- Speed up day-to-day Docker rebuilds after Python code changes.
- Preserve the current full deployment behavior.
- Avoid architecture changes in phase 1.
- Avoid changes to local non-Docker Python workflows.
- Make it explicit when a dependency-layer rebuild is required.

## Non-goals

- No embedding service extraction.
- No remote model serving.
- No multi-target capability matrix in phase 1.
- No changes to RAG, Code RAG, admin ingestion, or media-processing code paths.
- No API contract changes.

## Current State

Today the gateway Dockerfile installs all heavy dependencies and copies app code into the same final image:

- system packages and Node/npm tooling in `aigateway-api/Dockerfile`
- `torch` installed in a dedicated layer
- Python requirements installed before source copy
- CodeGraph CLI installed globally via npm
- Qwen embedding model downloaded during image build
- app source copied at the end and launched via Uvicorn

This layering already shows a natural split between low-churn runtime content and high-churn source code, but the repository currently publishes only one effective image shape.

## Proposed Design

### Image 1: `gateway-runtime-full`

A prebuilt base image that contains everything needed for the current full gateway runtime except the mutable application source tree.

Contents:

- base Python image
- system packages currently required by the full image:
  - `curl`
  - `git`
  - `nodejs`
  - `npm`
  - `tesseract-ocr`
  - `tesseract-ocr-chi-sim`
  - `tesseract-ocr-eng`
  - `libgl1`
  - `libglib2.0-0`
  - `ffmpeg`
- `torch`
- Python dependencies from `aigateway-api/requirements.txt`
- global `@colbymchenry/codegraph`
- predownloaded `Qwen/Qwen3-Embedding-0.6B`
- working directory and any stable runtime filesystem setup

Excluded from this image:

- `aigateway_core` source code
- `aigateway_api` source code
- environment-specific mounted config files

### Image 2: `gateway-app-full`

A thin application image that starts from `gateway-runtime-full` and adds only the code that changes frequently.

Contents:

- `FROM gateway-runtime-full`
- copy `aigateway-core/src/aigateway_core`
- copy `aigateway-api/src/aigateway_api`
- preserve the existing exposed port and Uvicorn startup command

This image should behave the same as the current full image from the perspective of runtime features and API behavior.

## Build Model

### Rebuild `gateway-runtime-full` only when:

- `aigateway-api/Dockerfile` dependency/setup layers change
- `aigateway-api/requirements.txt` changes
- the embedding model version changes
- system package requirements change
- CodeGraph installation details change

### Rebuild `gateway-app-full` when:

- `aigateway-core/src/**` changes
- `aigateway-api/src/**` changes
- startup command or app-only image metadata changes

## Compose / Deployment Shape

Phase 1 keeps the service topology unchanged.

- `gateway` remains a single container at runtime.
- Redis, Qdrant, Prometheus, Grafana, and the control panel remain unchanged.
- The main operational change is how the gateway image is built and referenced.

Recommended deployment behavior:

- build and publish `gateway-runtime-full` separately on dependency changes
- build `gateway-app-full` frequently on application changes
- point Compose or deployment automation at the app image for normal rollouts

## Local Development Impact

### Unchanged

- direct local Python workflows remain valid
- embedding stays local/in-process
- no extra service is required to run the gateway logic

### Changed

- Docker-based workflows gain an optional dependency/runtime prebuild step
- contributors need clear guidance on when to rebuild runtime vs app image

## Operational Guidance

The documentation should make these rules explicit:

1. If only Python source changed, rebuild the app image.
2. If requirements, heavy tooling, or model version changed, rebuild the runtime image first, then rebuild the app image.
3. The runtime image tag must be versioned in a way that makes stale-base mistakes obvious.

Recommended tag discipline:

- runtime image tags should reflect dependency state, such as date or git-derived dependency version
- app image tags should reflect the application revision
- app builds should pin an explicit runtime tag instead of relying on an ambiguous `latest`

The exact tag naming convention can be finalized in implementation planning.

## Error Handling and Failure Modes

This design does not add runtime network hops, so it avoids the new operational failure modes that a separate embedding service would introduce.

Main risks are build/deployment mistakes:

- app image built from an outdated runtime base
- runtime image not rebuilt after dependency changes
- deployment automation still using the legacy monolithic build path

Mitigations:

- document rebuild rules clearly
- use explicit tags for runtime images
- verify image ancestry during CI or release steps if practical

## Verification Plan

After implementation, verify:

1. `gateway-app-full` builds successfully using the prebuilt runtime image.
2. `gateway` starts successfully via Docker.
3. `GET /health` succeeds.
4. embedding-dependent paths still work with the baked-in local model.
5. CodeGraph-dependent flows still work.
6. OCR/media-related dependencies are still available where expected.
7. Rebuilding after a source-only change is measurably faster than the current all-in-one rebuild path.

## Rollout Plan

1. Introduce runtime/app image split without changing service topology.
2. Update Docker build instructions and Compose usage.
3. Validate runtime parity with the current full image.
4. Use the split build path for normal development and deployment.
5. Reassess whether additional slimming or service extraction is needed after measuring results.

## Alternatives Considered

### 1. Extract embedding into a separate service now

Rejected for phase 1 because it changes runtime architecture, adds network hops and new failure modes, and does not address all major weight sources in the current image.

### 2. Add a full multi-target capability matrix now

Rejected for phase 1 because it improves long-term flexibility but adds deployment complexity before solving the immediate build-speed problem.

### 3. Leave the image as-is

Rejected because it preserves the current slow rebuild experience and continues mixing low-churn heavy dependencies with fast-changing code.

## Open Questions Resolved

- Local deployment compatibility: preserved by keeping the current in-process execution model.
- Full-feature runtime support: preserved by keeping a `full` runtime base image.
- Service extraction timing: deferred until after build-speed gains are measured.

## Acceptance Criteria

The design is successful when all of the following are true:

- the gateway still runs with the same full feature set in Docker
- developers can rebuild normal code changes without reinstalling heavy dependencies or redownloading the embedding model
- local non-Docker workflows still function as before
- the repository documentation clearly states when runtime rebuilds are required
