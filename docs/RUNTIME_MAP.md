# Runtime Layer Map

Cross-reference between legacy module paths and the 总分总 runtime layers
introduced by `docs/superpowers/specs/2026-07-07-runtime-structure-design.md`.

Migration complete (2026-07-09): legacy root paths are REMOVED, not shims. The new paths below are canonical.

## `shared/` -- cross-layer utilities

| Legacy path                             | New path                                          |
|-----------------------------------------|---------------------------------------------------|
| `aigateway_core.config`                 | `aigateway_core.shared.config`                    |
| `aigateway_core.tracing`                | `aigateway_core.shared.tracing`                   |
| `aigateway_core.trace_event`            | `aigateway_core.shared.trace_event`               |
| `aigateway_core.exceptions`             | `aigateway_core.shared.exceptions`                |
| `aigateway_core.plugin_registry`        | `aigateway_core.shared.plugin_registry`           |
| `aigateway_core.logger`                 | `aigateway_core.shared.logger`                    |
| `aigateway_core.metrics`                | `aigateway_core.shared.metrics`                   |
| `aigateway_core.debug_config`           | `aigateway_core.shared.debug_config`              |
| `aigateway_core.redis_client`           | `aigateway_core.shared.redis_client`              |
| `aigateway_core.qdrant_client`          | `aigateway_core.shared.qdrant_client`             |
| `aigateway_core.integration_configs`    | `aigateway_core.shared.integration_configs`      |

## `prefix/` -- shared pre-routing layer (总 1 前半段)

| Legacy path                                | New path                             |
|--------------------------------------------|--------------------------------------|
| `aigateway_core.security` (KeyStore, PII)  | `aigateway_core.prefix.pii`          |
| `aigateway_core.pipeline.PIIDetectorPlugin`| `aigateway_core.prefix.pii`          |
| `aigateway_core.caching`                   | `aigateway_core.prefix.cache`        |
| `aigateway_core.pipeline.PromptCachePlugin`| `aigateway_core.prefix.cache`        |
| `aigateway_core.pipeline.SemanticCachePlugin` | `aigateway_core.prefix.cache`     |
| `aigateway_core.media` + subpackages       | `aigateway_core.prefix.media`        |

## `dispatch/` -- total orchestration (总 1 后半段)

| Legacy path                    | New path                              |
|--------------------------------|---------------------------------------|
| `aigateway_core.context`       | `aigateway_core.dispatch.context`     |

`RequestDispatcher` and `classify_request` now live in `aigateway_core.dispatch`.
`aigateway_api.dispatcher` is a thin adapter.

## `pipelines/understanding/` -- understanding pipeline (分)

| Legacy path                                       | New path                                              |
|---------------------------------------------------|-------------------------------------------------------|
| `aigateway_core.plugins.rag_retriever_plugin`     | `aigateway_core.pipelines.understanding.rag`          |
| `aigateway_core.plugins.conv_compressor_plugin`   | `aigateway_core.pipelines.understanding.conversation` |
| `aigateway_core.pipeline.PromptCompressPlugin`    | `aigateway_core.pipelines.understanding.compression`  |

## `pipelines/generation/` -- generation pipeline (分)

| Legacy path                                                                  | New path                                          |
|------------------------------------------------------------------------------|---------------------------------------------------|
| `aigateway_core.generation_optimization.strategies.ai_director`              | `aigateway_core.pipelines.generation.director`    |
| `aigateway_core.generation_optimization.plugins.ai_director_plugin`          | `aigateway_core.pipelines.generation.director`    |
| `aigateway_core.generation_optimization.strategies.intent_evaluator`         | `aigateway_core.pipelines.generation.intent`      |
| `aigateway_core.generation_optimization.plugins.intent_evaluator_plugin`     | `aigateway_core.pipelines.generation.intent`      |
| `aigateway_core.generation_optimization.strategies.token_compressor`         | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.feature_cache`            | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.prompt_confirmation`      | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.prompt_template_manager`  | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.video_preview`            | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.plugins.token_compressor_plugin`     | `aigateway_core.pipelines.generation.token`       |
| `aigateway_core.generation_optimization.strategies.draft_generator`          | `aigateway_core.pipelines.generation.draft`       |
| `aigateway_core.generation_optimization.plugins.draft_generator_plugin`      | `aigateway_core.pipelines.generation.draft`       |
| `aigateway_core.generation_optimization.plugins.cost_tracker_plugin`         | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.metrics`                             | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.models`                              | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.api_key_groups`                      | `aigateway_core.pipelines.generation.cost`        |
| `aigateway_core.generation_optimization.plugins.gen_model_router_plugin`     | `aigateway_core.pipelines.generation.routing_signals` |

## `route/` -- unified routing + response closure (总 2)

| Legacy path                                                          | New path                              |
|----------------------------------------------------------------------|---------------------------------------|
| `aigateway_core.generation_optimization.strategies.model_router`     | `aigateway_core.route.model_resolution` |
| `aigateway_core.litellm_bridge`                                      | `aigateway_core.route.bridge`         |

Streaming (`route/streaming/`) and costing (`route/metrics/`) have moved into `route/`. Quota accounting and final response assembly still live in the API surface.

## Other locations

`PipelineEngine` now lives in `aigateway_core.dispatch.pipeline_engine`. `code_rag` moved to `aigateway_core.pipelines.understanding.code_rag`. The surface packages (`aigateway_api`, `aigateway_cli`, `control-panel`) remain separate.
