# Runtime Structure Design

Date: 2026-07-07
Status: Draft approved in conversation
Scope: Design only; no implementation changes

## Summary

Reorganize the core runtime structure so it reflects the actual 总分总 execution model instead of historical package growth or mixed technical categories.

The target runtime shape is:

```text
shared prefix → dispatch → understanding | generation → route
```

This structure should be expressed primarily inside `aigateway-core/src/aigateway_core/`, while API/CLI/Control Panel remain surface layers at the repo/package level.

## Goals

1. Make the directory structure describe request lifecycle directly.
2. Separate shared pre-routing work from pipeline-specific work.
3. Make understanding and generation first-class sibling pipelines.
4. Make routing and unified response handling a first-class runtime layer.
5. Keep external surfaces (`aigateway-api`, `aigateway-cli`, `control-panel`) clearly separate from runtime internals.

## Non-goals

1. Do not redesign external product surfaces.
2. Do not change runtime behavior in this design phase.
3. Do not force a full repo-root reorganization around pipelines.
4. Do not collapse all shared logic into generic utility buckets.

## Design Principles

### 1. Directory structure should reflect execution order

The core runtime should tell the story of a request:

1. Shared preprocessing runs first.
2. Dispatch decides the pipeline.
3. The request goes through either the understanding or generation pipeline.
4. A unified route layer resolves the final model/provider path and assembles the outgoing response.

### 2. Shared prefix is not part of the understanding pipeline

`PII`, `cache`, and `media` are shared pre-routing stages. They should not live under `understanding/` if they execute before dispatch. Otherwise the directory structure would misrepresent the actual architecture.

### 3. Dispatch and route are skeleton-level responsibilities

These are not just helper modules:

- `dispatch/` is the first “总”: it coordinates and splits work.
- `route/` is the final “总”: it resolves outbound routing and performs unified response closure.

### 4. Surface layers stay separate from runtime internals

`aigateway-api`, `aigateway-cli`, and `control-panel` should remain external surfaces. The runtime skeleton belongs in core, not spread across entry surfaces.

## Target Core Layout

Recommended conceptual structure inside `aigateway-core/src/aigateway_core/`:

```text
aigateway_core/
  prefix/
    pii/
    cache/
    media/

  dispatch/
    dispatcher.py
    classifier.py
    context.py

  pipelines/
    understanding/
      rag/
      conversation/
      compression/

    generation/
      director/
      intent/
      token/
      draft/
      cost/

  route/
    model_resolution/
    bridge/
    streaming/
    quota/
    metrics/
    response/

  shared/
    config.py
    tracing.py
    exceptions.py
    plugin_registry.py
    logger.py
```

This is a conceptual target shape, not a mandatory one-shot move list.

## Layer Semantics

### `prefix/`

Shared preprocessing that runs before pipeline dispatch.

Expected responsibilities:
- PII detection / sanitize / reject behavior
- cache key generation, cache lookup, cache backfill orchestration
- media preprocessing such as OCR, transcription, and document extraction

This layer represents the system-wide pre-routing prefix, not understanding-specific logic.

### `dispatch/`

The main orchestration and split layer.

Expected responsibilities:
- request classification
- pipeline selection
- context creation and propagation
- orchestration of prefix → selected pipeline → route

This is the first “总” in the 总分总 model.

### `pipelines/understanding/`

Understanding-specific runtime chain.

Expected responsibilities:
- RAG retrieval
- code/document graph retrieval where applicable
- conversation compression/summarization
- prompt compression for understanding requests

Its job is to optimize inputs for understanding-oriented model calls.

### `pipelines/generation/`

Generation-specific runtime chain.

Expected responsibilities:
- AI Director prompt shaping
- intent/complexity evaluation
- token/feature compression
- draft generation workflow
- cost tracking
- generation-side route signals as needed

Its job is to optimize generation requests for cost, success rate, and model fit.

### `route/`

Unified routing and response closure layer.

This should include not only outbound model/provider resolution, but also the common end-of-flow responsibilities that happen after either pipeline.

Expected responsibilities:
- auto model resolution
- provider selection and fallback/cooldown handling
- LiteLLM bridge integration
- streaming assembly
- final response assembly
- quota accounting
- metrics accounting

This is the final “总” in the 总分总 model.

### `shared/`

True cross-layer shared modules only.

Expected responsibilities:
- config loading
- tracing
- exceptions
- low-level logging
- registry/base abstractions that do not belong to a specific runtime phase

This bucket should remain disciplined. If a module clearly belongs to `prefix`, `dispatch`, `pipelines`, or `route`, it should not live in `shared/` just because multiple files import it.

## Surface vs Runtime Boundary

### Keep as surfaces

#### `aigateway-api`
Should remain the API surface:
- FastAPI app setup
- HTTP/OpenAI-compatible request handling
- admin endpoints
- protocol adaptation
- handing requests into the runtime skeleton

It should gradually stop acting as a long-term home for core runtime orchestration.

#### `aigateway-cli`
Should remain the CLI surface:
- command entrypoints
- terminal interaction
- request submission into gateway/runtime

#### `control-panel`
Should remain the human-facing surface:
- pages and components
- browser-side API client
- admin and operator workflows
- future human chat surface

### Move into runtime skeleton conceptually

The following responsibilities belong to the runtime structure inside core:

- prefix-level preprocessing
- dispatch/orchestration/classification
- understanding pipeline internals
- generation pipeline internals
- unified route/bridge/response closure

## Current-to-Target Classification Guide

This design recommends classifying existing modules before moving files.

### Prefix-class modules
- PII detector / security behavior that runs before split
- cache manager / cache-key / tier orchestration
- media optimization preprocessing

### Dispatch-class modules
- dispatcher orchestration
- request classification
- runtime context construction

### Understanding-class modules
- RAG retrieval
- code RAG helpers and graph retrieval
- conversation compression
- prompt compression

### Generation-class modules
- AI Director
- intent evaluator
- token compressor
- draft generator
- generation cost tracking

### Route-class modules
- model resolution
- LiteLLM bridge
- fallback/cooldown
- streaming assembly
- response assembly
- quota and metrics closure

### Shared-class modules
- config
- tracing
- common exceptions
- low-level logging/registry primitives

## Recommended Migration Strategy

### Phase 1: Classify responsibilities

Before moving files, classify every runtime module into one of:
- surface
- prefix
- dispatch
- understanding
- generation
- route
- shared

This is the most important step. Without it, file moves will just rename confusion.

### Phase 2: Introduce target directories and adapters

Create the target directory structure and move modules in small batches. Add compatibility imports/adapters where needed so imports do not all have to flip at once.

### Phase 3: Move orchestration out of surface-heavy files

Gradually reduce orchestration responsibility in API-surface files such as `openai_compat.py`, keeping them focused on protocol adaptation.

### Phase 4: Align runtime execution with runtime structure

Only after the structure and boundaries are clear should runtime execution be fully aligned to the same prefix → dispatch → pipelines → route flow.

## Why This Design Is Preferred

Compared with organizing primarily by product surface, this design better reflects the runtime architecture the user wants to preserve and make visible.

Compared with a repo-root reorganization around `understanding/` and `generation/`, this design preserves a clean distinction between:
- external surfaces
- internal runtime skeleton
- ops/docs/tests assets

Compared with putting PII/cache/media under `understanding/`, this design keeps directory semantics truthful to execution order.

## Risks and Mitigations

### Risk: Overusing `shared/`
Mitigation: force a phase-based classification first; only truly phase-neutral modules belong in `shared/`.

### Risk: Moving files before clarifying execution ownership
Mitigation: classify modules by runtime role before any move.

### Risk: API surface keeps accumulating orchestration again
Mitigation: explicitly treat `aigateway-api` as protocol surface, not runtime skeleton.

### Risk: One-shot big-bang refactor
Mitigation: migrate in phases with compatibility shims and import bridges.

## Acceptance Criteria

This design is successful when:

1. A new contributor can infer request flow from the core directory tree.
2. Shared prefix responsibilities are not mislabeled as understanding-only.
3. Understanding and generation are obvious sibling pipelines.
4. Unified routing/response closure is explicit rather than scattered.
5. API/CLI/Panel remain recognizable surfaces, not mixed runtime skeletons.

## Open Decisions Resolved in This Design

- Shared prefix exists and includes `PII / cache / media`.
- Runtime structure should live primarily in core, not by fully pipeline-izing the repo root.
- The unified final layer should be named `route/`, not `exit/`.
- `route/` should include final response assembly, streaming, quota, and metrics closure, not only model invocation.

## Next Step

Write an implementation plan that maps current files and packages to the target structure, then sequence the refactor in low-risk stages.