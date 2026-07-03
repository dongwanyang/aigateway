# Requirements Document

## Introduction

本文档定义 AI Gateway 平台的"Generation Optimization Layer（生成优化层）"功能需求。该功能层旨在通过四大核心策略——AI 导演 Prompt 优化、智能模型路由、渐进式生成工作流（Draft-to-HiRes）、输入端视觉 Token 压缩与资产复用——大幅降低生成式 AI 的调用成本，同时保证输出质量。

该功能层将集成到现有的 `aigateway-core/media` 模块中，作为生成请求进入昂贵模型之前的前置优化管线。

## Glossary

- **Generation_Optimization_Layer**: 生成优化层，AI Gateway 平台中位于用户请求和生成模型之间的优化中间层
- **AI_Director**: AI 导演模块，负责将用户模糊的提示词扩写为结构化、带专业约束的优化提示词
- **Intent_Evaluator**: 意图评估器，分析生成请求的复杂度并决定路由目标模型
- **Model_Router**: 模型路由器，根据意图评估结果将请求分发到不同等级的生成模型
- **Draft_Generator**: 草图生成器，负责生成低分辨率草图或关键帧供用户预览确认
- **Upscaler**: 超分辨率放大器，将用户确认的低分辨率结果放大为高清输出
- **Token_Compressor**: 视觉 Token 压缩器，对输入参考图进行语义级压缩以减少 Token 消耗
- **Feature_Cache**: 特征缓存，存储已提取的角色特征向量（Embedding）供后续复用
- **Prompt_Template**: 提示词模板，用户预先配置的结构化提示词模板，可按名称保存和引用
- **Routing_Hint**: 路由提示，用户在生成请求中附带的模型偏好提示（如"best quality"、"cheapest"等），路由器应尊重该偏好
- **API_Key_Group**: API Key 分组，用于对 API Key 进行分类管理（如按团队、项目或用途分组），不影响资源隔离逻辑
- **Model_Modality**: 模型模态分类，将模型分为三大类：
  - `llm`（纯文本语言模型）：输入文本，输出文本。包括 Chat、代码生成、翻译、RAG、Agent 等
  - `mllm`（多模态理解模型）：输入文本 + 图片/音频/视频，输出文本。包括视觉理解(VLM)、OCR、图表分析、文档理解等
  - `generative`（生成模型）：输入文本/图片/音频等，输出图片、视频、音频、3D 等。包括文生图、文生视频、图生视频、TTS、音乐生成等
- **Generation_Request**: 生成请求，包含用户提示词、参考图、目标模型等信息的请求对象
- **Complexity_Score**: 复杂度评分，意图评估器为生成请求打出的 0-100 分值
- **Feature_Vector**: 特征向量，从参考图中提取的角色/主体语义级嵌入表示

## Requirements

### Requirement 1: AI 导演 Prompt 优化

**User Story:** 作为平台用户，我希望系统自动优化我编写的生成提示词，或者使用我预先保存的提示词模板，以便提高一次性出片率并减少因反复重试造成的成本浪费。

#### Acceptance Criteria

1. WHEN a Generation_Request with a user prompt is received and no Prompt_Template is specified, THE AI_Director SHALL rewrite the prompt into a structured format containing subject description, action, environment, and camera parameters within a configurable timeout (default: 10 seconds)
2. WHEN the AI_Director rewrites a prompt, THE AI_Director SHALL use a configurable low-cost text model (default: GPT-4o-mini) to perform the rewrite, and the optimized prompt output SHALL NOT exceed a configurable maximum length (default: 2000 characters)
3. WHEN a Generation_Request specifies a Prompt_Template by name, THE AI_Director SHALL apply the referenced template to the user prompt instead of calling the rewrite model
4. WHEN the AI_Director completes prompt optimization and the prompt confirmation feature is enabled (default: enabled), THE Generation_Optimization_Layer SHALL return the optimized prompt to the user for confirmation or re-editing before proceeding to the next pipeline stage; WHEN the user confirms the optimized prompt or submits an edited version, THE Generation_Optimization_Layer SHALL attach the confirmed prompt to the Generation_Request and continue processing; WHERE the prompt confirmation feature is disabled via configuration, THE Generation_Optimization_Layer SHALL attach the optimized prompt directly to the Generation_Request metadata and proceed without waiting for user confirmation
5. WHEN a Generation_Request contains a prompt shorter than a configurable minimum length (default: 10 characters) and no Prompt_Template is specified, THE AI_Director SHALL expand the prompt with contextual details inferred from any accompanying reference images or generation parameters
6. IF the AI_Director model call fails or times out, THEN THE Generation_Optimization_Layer SHALL fall back to using the original user prompt, log the failure with the error reason, and continue processing the request
7. WHERE the AI_Director feature is disabled via configuration, THE Generation_Optimization_Layer SHALL pass the user prompt through without modification
8. THE Generation_Optimization_Layer SHALL propagate the existing trace_id from PipelineContext through all optimization stages, and each stage (AI_Director, Intent_Evaluator, Token_Compressor, Draft_Generator, Model_Router, Cost_Tracker) SHALL create a child span under the request trace for full pipeline observability

### Requirement 2: 智能模型路由

**User Story:** 作为平台运营者，我希望系统根据任务复杂度将生成请求路由到合适的模型，以便在保证输出质量的前提下降低整体 API 成本。

#### Acceptance Criteria

1. WHEN a Generation_Request is received, THE Intent_Evaluator SHALL analyze the request and assign a Complexity_Score between 0 and 100 within 2 seconds
2. WHEN the Intent_Evaluator produces a Complexity_Score, THE Model_Router SHALL select the most cost-effective model from the configured provider model list that meets the minimum capability threshold for the given score; the capability-to-price mapping SHALL be defined in the YAML configuration for each model
3. THE Intent_Evaluator SHALL evaluate complexity based on the following factors: number of subjects (1 = low, 2+ = higher), physical interaction between subjects (none/contact/dynamic), camera movement type (static/pan/tracking), and target resolution (≤512px = low, ≤1024px = mid, >1024px = high)
4. WHEN a Generation_Request specifies an explicit model override, THE Model_Router SHALL bypass routing and use the specified model
5. IF a Generation_Request specifies a model override that is not present in the configured provider model list, THEN THE Model_Router SHALL reject the request with an error indicating the specified model is not available
6. WHEN a Generation_Request contains a routing hint (such as "use best quality", "use cheapest", or a specific model name), THE Model_Router SHALL respect the user's hint preference: "best quality" selects the highest-capability model, "cheapest" selects the lowest-price model, and a specific name selects that model directly
7. WHEN the Model_Router selects a model, THE Generation_Optimization_Layer SHALL record the routing decision, selected model identifier, selected provider, and Complexity_Score in the request metadata
8. IF the Intent_Evaluator fails to compute a Complexity_Score (due to timeout or internal error), THEN THE Model_Router SHALL route the request to the model configured as the default in the generation_optimization config and log the failure
9. IF the selected target model is unavailable, THE Model_Router SHALL attempt to route the request to the next available model from the same provider's fallback_models list, and if no fallback is available, try models from other providers with similar capability, recording the fallback in the request metadata
10. WHEN routing a Generation_Request, THE Model_Router SHALL first filter available models by their configured Model_Modality to ensure only models of the `generative` category are considered for image/video/audio generation tasks; models of the `mllm` category SHALL be used for AI_Director prompt rewriting when the request includes reference images; models of the `llm` category SHALL be used for AI_Director prompt rewriting when no reference images are present

### Requirement 3: 渐进式生成工作流（Draft-to-HiRes）

**User Story:** 作为平台用户，我希望系统先生成低成本的预览草图，待我确认满意后再执行高清渲染，以便避免在不满意的结果上浪费高清渲染费用。

#### Acceptance Criteria

1. WHEN an image Generation_Request enters the Draft-to-HiRes workflow, THE Draft_Generator SHALL generate a low-resolution preview at a configurable resolution (default: 512x512 pixels) and return a unique draft identifier to the user within 30 seconds
2. WHEN a video Generation_Request enters the Draft-to-HiRes workflow, THE Draft_Generator SHALL generate keyframe images at a configurable interval (default: every 5 seconds, minimum 2 frames for first and last) at the same low-resolution preview size and return them with a unique draft identifier within 60 seconds; the user MAY explicitly specify the number of keyframes in the Generation_Request, overriding the interval-based calculation
3. WHEN the user confirms a draft result via the draft identifier, THE Upscaler SHALL upscale the confirmed image to the user-specified target resolution (default: 1920x1080 pixels, maximum: 4096x4096 pixels) using a configurable super-resolution algorithm
4. WHEN the user confirms video keyframes via the draft identifier, THE Generation_Optimization_Layer SHALL generate a preview video at a configurable duration (default: 30 seconds) and a configurable low frame rate (default: 8 fps), and then apply frame interpolation to reach the target frame rate (default: 60 fps, range: 24-120 fps)
5. WHEN the user rejects a draft result via the draft identifier, THE Draft_Generator SHALL regenerate a new low-resolution draft without incurring high-resolution generation costs, up to a configurable maximum number of regeneration attempts per request (default: 5); THE Generation_Optimization_Layer SHALL NOT cache the rejected draft result and SHALL immediately release associated storage resources for the rejected draft
6. THE Generation_Optimization_Layer SHALL expose an API endpoint that accepts a draft identifier and a user action (confirm or reject) to advance the Draft-to-HiRes workflow
7. IF the Upscaler algorithm fails, THEN THE Generation_Optimization_Layer SHALL return an error indicating the upscale failure while preserving the draft result for retry within a configurable retention period (default: 24 hours)
8. IF a draft result is neither confirmed nor rejected within the configurable retention period (default: 24 hours), THEN THE Generation_Optimization_Layer SHALL mark the draft as expired and release associated resources
9. IF the user exceeds the maximum number of regeneration attempts, THEN THE Generation_Optimization_Layer SHALL return an error indicating the regeneration limit has been reached and preserve the most recent draft for confirmation

### Requirement 4: 输入端视觉 Token 压缩

**User Story:** 作为平台用户，我希望系统自动压缩我上传的参考图以减少输入端的 Token 消耗，以便降低多模态生成的调用费用。

#### Acceptance Criteria

1. WHEN a Generation_Request includes reference images (maximum 10 images per request, each not exceeding 20 MB), THE Token_Compressor SHALL perform semantic-level compression on each input image, targeting a configurable token reduction ratio (default: 50%, range: 20%-90%) compared to the original image token count
2. WHEN the Token_Compressor processes a reference image, THE Token_Compressor SHALL segment the foreground subject from the background and extract the subject's feature representation, discarding background regions that do not contribute to subject identity
3. WHEN compression is complete, THE Token_Compressor SHALL output a Feature_Vector with a configurable maximum dimensionality (default: 512 dimensions) suitable for injection into the generation model via the configured adapter mechanism
4. THE Token_Compressor SHALL record the token savings achieved in the request metadata, where original token count is calculated as image file size in bytes divided by 4, and compressed token count is the Feature_Vector dimension count
5. IF the Token_Compressor encounters an image format other than the supported set (PNG, JPEG, WebP, BMP), THEN THE Token_Compressor SHALL pass the original image through without compression and log a warning indicating the unsupported format
6. IF the Token_Compressor fails to complete compression within a configurable timeout (default: 30 seconds) per image, THEN THE Token_Compressor SHALL pass the original image through without compression and log a timeout warning
7. WHERE the Token_Compressor feature is disabled via configuration, THE Generation_Optimization_Layer SHALL pass reference images through without modification

### Requirement 5: 特征向量缓存与资产复用

**User Story:** 作为平台用户，我希望系统缓存已提取的角色特征向量，以便在后续生成同一角色时直接复用，避免重复提取并保持角色一致性。

#### Acceptance Criteria

1. WHEN the Token_Compressor extracts a Feature_Vector, THE Feature_Cache SHALL store the vector using a composite cache key derived from the user-scoped character identifier and the extraction model version, with a configurable TTL (default: 30 days)
2. WHEN a Generation_Request references a previously cached character identifier, THE Feature_Cache SHALL perform a cache lookup within a configurable timeout (default: 500ms) and return the cached Feature_Vector instead of re-extracting from the reference image
3. WHEN a cached Feature_Vector is retrieved, THE Generation_Optimization_Layer SHALL inject the vector into the generation request using the injection method specified in the Generation_Request (supporting IP-Adapter and ControlNet), defaulting to IP-Adapter if not specified
4. WHEN the Feature_Cache returns a cached Feature_Vector, THE Feature_Cache SHALL extend the TTL of the cached entry by the configured TTL duration
5. IF a cache lookup fails due to infrastructure issues or timeout, THEN THE Feature_Cache SHALL fall back to extracting the Feature_Vector from the original reference image and log the failure
6. IF the fallback extraction also fails because the original reference image is unavailable, THEN THE Generation_Optimization_Layer SHALL return an error indicating that the character feature vector could not be retrieved
7. THE Feature_Cache SHALL scope character identifiers per API Key, ensuring that different API Keys' identifiers with the same name do not collide

### Requirement 6: 优化层配置管理

**User Story:** 作为平台管理员，我希望能通过配置灵活启用或禁用各优化策略，以便根据业务需求调整优化层行为。

#### Acceptance Criteria

1. THE Generation_Optimization_Layer SHALL provide an independent boolean enable/disable configuration item for each optimization strategy (AI_Director, Model_Router, Draft-to-HiRes, Token_Compressor), default value for all strategies SHALL be enabled (true)
2. THE Generation_Optimization_Layer SHALL load configuration from environment variables and YAML configuration files, with environment variables taking precedence over configuration file values
3. WHEN configuration values are missing, THE Generation_Optimization_Layer SHALL use the documented default values for each configuration item and log a warning message that includes the configuration key name and the default value applied
4. IF a configuration value is invalid (wrong type or out of permitted range), THEN THE Generation_Optimization_Layer SHALL reject the invalid value, retain the previous valid configuration for that item, and log an error message indicating the invalid key and value
5. WHEN a configuration file modification is detected, THE Generation_Optimization_Layer SHALL apply the new configuration within 5 seconds without requiring a service restart
6. WHEN a request reaches a strategy that is disabled via configuration, THE Generation_Optimization_Layer SHALL bypass that strategy and pass the request to the next pipeline stage without modification
7. THE Generation_Optimization_Layer SHALL expose ALL configurable parameters referenced in Requirements 1-5 (including but not limited to: AI_Director timeout, rewrite model name, maximum prompt length, minimum prompt length, prompt confirmation enable/disable, Complexity_Score thresholds, routing hint keywords, draft resolution, preview video duration, preview video frame rate, target frame rate, maximum regeneration attempts, draft retention period, token compression ratio, Feature_Vector dimensionality, compression timeout, cache TTL, cache lookup timeout) as items in the YAML configuration file, with each item having a documented default value that is applied when the user does not provide a custom value

### Requirement 7: 成本追踪与指标上报

**User Story:** 作为平台运营者，我希望系统记录每次优化带来的成本节省数据，以便量化优化层的商业价值并持续改进策略。

#### Acceptance Criteria

1. WHEN a Generation_Request completes the optimization pipeline, THE Generation_Optimization_Layer SHALL record the cost savings in USD (precision: 6 decimal places) for each applicable strategy as separate values: model tier downgrade savings (premium model price minus actual model price per request), token compression savings (original token count minus compressed token count multiplied by the per-token price of the target model), and AI_Director optimization net savings (estimated reduction in retry cost based on the configurable assumed retry rate, default: 0.3, minus the AI_Director model invocation cost)
2. THE Generation_Optimization_Layer SHALL expose cost optimization metrics as Prometheus counters and gauges via the existing metrics endpoint, including: a counter for total cost savings in USD labeled by strategy type (model_routing, token_compression, prompt_optimization), a counter for total optimization invocations labeled by strategy type, and a gauge for cumulative net savings
3. WHEN the AI_Director optimizes a prompt, THE Generation_Optimization_Layer SHALL increment a Prometheus counter for successful prompt optimizations and add the AI_Director model invocation cost in USD to a Prometheus counter labeled with the optimization model name
4. WHEN the Model_Router routes a request to a model priced lower than the premium model configured for that request type, THE Generation_Optimization_Layer SHALL calculate the price difference in USD using the per-request prices from the configured model pricing table and record it as a model routing cost saving
5. IF cost savings calculation fails due to missing pricing data or computation error, THEN THE Generation_Optimization_Layer SHALL log a warning, record zero savings for the affected strategy, and continue processing the Generation_Request without interruption

### Requirement 8: 提示词模板管理

**User Story:** 作为平台用户，我希望能创建、保存和管理提示词模板，以便在后续生成请求中直接引用模板，跳过 AI 导演的自动改写步骤。

#### Acceptance Criteria

1. THE Generation_Optimization_Layer SHALL provide API endpoints for creating, reading, updating, and deleting Prompt_Template resources
2. WHEN a user creates a Prompt_Template, THE Generation_Optimization_Layer SHALL store the template with a unique name (unique within the owning API Key's scope, 1-64 characters, allowing alphanumeric characters, hyphens, and underscores), the template content (maximum 10,000 characters), and an optional description (maximum 500 characters)
3. WHEN a user lists Prompt_Template resources, THE Generation_Optimization_Layer SHALL return all templates owned by the requesting API Key, supporting pagination with a configurable page size (default: 20, maximum: 100)
4. THE Prompt_Template content SHALL support placeholder variables (using `{{variable_name}}` syntax) that are substituted with user-provided values at generation time
5. WHEN a Generation_Request references a Prompt_Template that does not exist, THE Generation_Optimization_Layer SHALL return an error indicating the template was not found
6. IF a Prompt_Template contains placeholder variables that are not provided in the Generation_Request, THEN THE Generation_Optimization_Layer SHALL return a validation error listing the missing variables
7. IF a user attempts to create a Prompt_Template with a name that already exists within their own API Key's templates, THEN THE Generation_Optimization_Layer SHALL return a validation error indicating the name is already in use
8. IF a user attempts to update or delete a Prompt_Template that is owned by another API Key, THEN THE Generation_Optimization_Layer SHALL return an authorization error and leave the template unchanged

### Requirement 9: API Key 分组管理

**User Story:** 作为平台管理员，我希望能对 API Key 进行分组（如按团队、项目或用途），以便更好地分类管理和查看各组的成本统计。

#### Acceptance Criteria

1. THE Generation_Optimization_Layer SHALL support an optional `group` field in each API Key configuration, allowing the administrator to assign a group label (e.g., "marketing-team", "internal-dev", "customer-project-a")
2. WHEN an API Key has a group assigned, THE Generation_Optimization_Layer SHALL include the group label in cost tracking metrics as an additional Prometheus label
3. WHEN listing cost metrics, THE Generation_Optimization_Layer SHALL support filtering and aggregation by API Key group
4. IF an API Key does not have a group assigned, THEN THE Generation_Optimization_Layer SHALL use "default" as the group label for metrics purposes
5. THE API Key group SHALL NOT affect resource isolation logic — templates and feature caches remain isolated per individual API Key regardless of group membership
