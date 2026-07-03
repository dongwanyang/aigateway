# Implementation Plan: Generation Optimization Layer

## Overview

基于 AI Gateway 生成优化层的需求和设计文档，将实现划分为增量式编码任务。该优化层以插件形式集成到现有 PipelineEngine，包含 AI 导演、智能路由、Draft-to-HiRes、Token 压缩、特征缓存、成本追踪和模板管理七大模块。

## Tasks

- [x] 1. 搭建项目结构和核心配置
  - [x] 1.1 创建 generation_optimization 模块目录结构
    - 在 `aigateway-core/src/aigateway_core/` 下创建 `generation_optimization/` 包
    - 创建子目录: `strategies/`, `plugins/`
    - 创建 `__init__.py`, `config.py`, `metrics.py`, `exceptions.py`
    - _需求: 6.1, 6.2, 6.7_

  - [x] 1.2 实现 GenerationOptimizationConfig 配置数据结构
    - 实现所有 dataclass 配置类: `AIDirectorConfig`, `ModelRouterConfig`, `DraftWorkflowConfig`, `TokenCompressorConfig`, `FeatureCacheConfig`, `CostTrackingConfig`, `PromptTemplateConfig`
    - 实现 `GenerationOptimizationConfig` 主配置类聚合所有子配置
    - 支持从 YAML 加载 `generation_optimization` 配置节，环境变量优先于 YAML 值
    - 实现配置校验逻辑: 类型检查和范围检查，无效值保留旧值并记录错误日志
    - _需求: 6.1, 6.2, 6.3, 6.4, 6.7_

  - [x] 1.3 实现配置热重载和异常类定义
    - 实现文件监控（watchdog）检测 YAML 配置变更，5 秒内应用新配置
    - 实现所有异常类: `GenerationOptimizationError`, `PromptOptimizationError`, `ModelRoutingError`, `TokenCompressionError`, `DraftWorkflowError`, `FeatureCacheError`, `TemplateValidationError`, `ConfigValidationError`
    - _需求: 6.4, 6.5_

  - [x]* 1.4 编写配置模块属性测试
    - **Property 19: 配置环境变量优先级** — 验证任何同时存在于 YAML 和环境变量中的配置项，环境变量值优先
    - **Property 20: 无效配置保留旧值** — 验证无效配置值（类型错误或超出范围）不会覆盖已有的有效配置
    - **验证: 需求 6.2, 6.4**

- [x] 2. 实现核心数据模型
  - [x] 2.1 实现生成请求和结果数据结构
    - 实现 `GenerationRequest` dataclass，包含 prompt、reference_images、target_model、routing_hint、required_modality、template_name、template_variables、character_id、target_resolution、target_fps、injection_method、api_key_id、request_id 等字段
    - 实现 `ComplexityEvaluation`, `RoutingDecision`, `PromptOptimizationResult`, `CompressionResult`, `DraftResult`, `UpscaleResult`, `PromptTemplate`, `CostSavingRecord` 数据结构
    - _需求: 1.1, 2.1, 2.7, 3.1, 4.3, 5.1, 7.1, 8.2_

- [x] 3. 实现 AI 导演策略
  - [x] 3.1 实现 AIDirectorStrategy 核心逻辑
    - 实现 `optimize_prompt()` 方法: 调用低成本文本模型将用户 prompt 改写为含 subject/action/environment/camera 的结构化格式
    - 确保输出不超过配置的 `max_prompt_length`（默认 2000 字符）
    - 超时处理（默认 10 秒），超时或失败时降级到原始 prompt
    - 短 prompt（< min_prompt_length）自动扩展逻辑
    - _需求: 1.1, 1.2, 1.5, 1.6_

  - [x] 3.2 实现 Prompt 确认流程
    - 当 `prompt_confirmation_enabled=True` 时，返回优化后 prompt 等待用户确认/编辑
    - 当 `prompt_confirmation_enabled=False` 时，直接附加到请求元数据继续处理
    - 用户确认或提交编辑版本后附加到 Generation_Request 并继续
    - _需求: 1.4_

  - [x] 3.3 实现 AIDirectorPlugin 插件封装
    - 注册到 PluginRegistry，`depends_on=["prompt_cache"]`
    - 在 `execute()` 中创建子 span，记录 trace_id
    - 禁用时透传请求不做修改
    - 根据是否有参考图选择模态: 有参考图用 mllm 模型，无参考图用 llm 模型
    - _需求: 1.7, 1.8, 2.10_

  - [x]* 3.4 编写 AI Director 属性测试和单元测试
    - **Property 1: Prompt 优化输出长度约束** — 验证任何输入 prompt，输出不超过 max_prompt_length
    - **Property 2: 禁用策略透传不变性（AI Director 部分）** — 验证禁用时 prompt 不被修改
    - **Property 3: AI Director 故障降级保留原始 Prompt** — 验证模型调用失败时输出等于原始 prompt
    - 单元测试: 模板应用、短 prompt 扩展、超时降级
    - **验证: 需求 1.2, 1.6, 1.7**

- [x] 4. 实现意图评估和模型路由
  - [x] 4.1 实现 IntentEvaluatorStrategy
    - 实现 `evaluate()` 方法，分析请求复杂度并打分 0-100
    - 评估维度: subject_count（主体数量）、interaction_type（物理交互）、camera_movement（镜头运动）、target_resolution（目标分辨率）
    - 2 秒超时限制
    - _需求: 2.1, 2.3_

  - [x] 4.2 实现 ModelRouterStrategy
    - 实现 `route()` 方法: 先按 Model_Modality 筛选（llm/mllm/generative）
    - 再按 capability_score >= complexity_score 筛选合格模型
    - 在合格模型中选择价格最低的（动态选择，非固定层级）
    - 支持 routing_hint: "best quality" 选最高 capability，"cheapest" 选最低价格，具体名称直接选
    - 支持 model_override 绕过路由或拒绝不存在的模型
    - 模型不可用时按 fallback_models 列表降级，跨 provider 降级
    - _需求: 2.2, 2.4, 2.5, 2.6, 2.9, 2.10_

  - [x] 4.3 实现 IntentEvaluatorPlugin 和 GenModelRouterPlugin
    - IntentEvaluatorPlugin: `depends_on=["ai_director"]`, 创建子 span，记录 complexity_score
    - GenModelRouterPlugin: `depends_on=["draft_generator"]`, 记录路由决策到请求元数据（模型、provider、原因、分数）
    - 评估失败时回退到配置的 default_model 并记录日志
    - _需求: 2.7, 2.8, 1.8_

  - [x]* 4.4 编写模型路由属性测试
    - **Property 4: 复杂度评分范围不变量** — 验证评分始终在 [0, 100]
    - **Property 5: 模型路由决策正确性** — 验证选择 capability >= score 且价格最低的模型
    - **Property 6: 模型覆盖绕过路由** — 验证指定有效模型时直接使用该模型
    - **Property 7: 无效模型覆盖拒绝** — 验证指定不存在模型时返回错误
    - **Property 8: 路由提示优先** — 验证 "best quality"/"cheapest" 提示的正确行为
    - **Property 9: 路由元数据完整性** — 验证元数据包含完整的路由信息
    - **Property 10: 意图评估失败回退到默认模型** — 验证评估失败时使用 default_model
    - **验证: 需求 2.1-2.9**

- [x] 5. 检查点 - 核心路由与 Prompt 优化
  - 确保所有测试通过，ask the user if questions arise.

- [x] 6. 实现 Token 压缩和特征缓存
  - [x] 6.1 实现 TokenCompressorStrategy
    - 实现 `compress()` 方法: 前景/背景分割 → 主体特征提取 → 输出 Feature_Vector
    - 压缩率可配置（默认 50%，范围 20%-90%）
    - Feature_Vector 维度不超过配置的 max_vector_dimensions（默认 512）
    - 仅支持 PNG/JPEG/WebP/BMP 格式，不支持的格式透传原图并记录警告
    - 单图超时处理（默认 30 秒），超时透传原图
    - 每请求最多 10 张图，单图不超过 20MB
    - Token 计算: original = file_size_bytes / 4, compressed = vector_dimensions
    - _需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 6.2 实现 FeatureCacheManager
    - Redis Key 格式: `aigateway:feature:{api_key_id}:{character_id}:{model_version}`
    - 实现 `get_feature()`, `store_feature()`, `extend_ttl()` 方法
    - 缓存查找超时 500ms，命中时自动续期 TTL
    - 缓存以 API Key 隔离，不同 API Key 同名 character_id 不冲突
    - 缓存失败时降级到从原始图重新提取
    - 原始图也不可用时返回错误
    - _需求: 5.1, 5.2, 5.4, 5.5, 5.6, 5.7_

  - [x] 6.3 实现 TokenCompressorPlugin
    - `depends_on=["intent_evaluator"]`, 创建子 span
    - 先查询 Feature Cache，命中则跳过压缩
    - 未命中则压缩后存入缓存
    - 禁用时透传参考图不做修改
    - 记录 token 节省到请求元数据
    - _需求: 4.7, 5.2, 5.3, 1.8_

  - [x] 6.4 编写 Token 压缩和特征缓存属性测试
    - **Property 13: Token 压缩故障透传** — 验证不支持格式或超时时输出原始图像
    - **Property 14: Feature Vector 维度约束** — 验证输出维度不超过 max_vector_dimensions
    - **Property 15: Token 节省计算公式正确性** — 验证 original = size/4, compressed = dimensions
    - **Property 16: 特征缓存存取一致性** — 验证存入的向量可完整取出
    - **Property 17: 特征缓存 API Key 隔离** — 验证不同 API Key 同名 character 互不污染
    - **Property 18: 缓存命中 TTL 续期** — 验证每次命中后 TTL 被延长
    - **验证: 需求 4.3-4.6, 5.1-5.7**

- [x] 7. 实现渐进式生成工作流 (Draft-to-HiRes)
  - [x] 7.1 实现 DraftGeneratorStrategy
    - 实现 `generate_draft()`: 图片请求生成 512x512 预览（30 秒内）
    - 视频请求按时间间隔动态生成关键帧: 默认每 5 秒一帧，最少 2 帧（首末帧），用户可显式指定数量覆盖
    - 实现 `confirm_draft()`: 触发 Upscaler 放大到目标分辨率（默认 1920x1080，最大 4096x4096）
    - 实现 `reject_draft()`: 重新生成草图，不缓存被拒绝的草图、立即释放资源
    - 重试次数限制（默认 5 次），耗尽后返回错误并保留最近草图
    - draft_id 唯一标识，24 小时过期自动释放资源
    - _需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9_

  - [x] 7.2 实现视频预览和帧插值逻辑
    - 确认视频关键帧后生成预览视频: 默认 30 秒、8fps
    - 帧插值到目标帧率（默认 60fps，范围 24-120fps）
    - _需求: 3.4_

  - [x] 7.3 实现 DraftGeneratorPlugin 和 Draft API 端点
    - DraftGeneratorPlugin: `depends_on=["token_compressor"]`, 创建子 span
    - 实现 `/drafts/{draft_id}/action` API 端点，接受 confirm/reject 操作
    - Redis 存储草图数据，Key: `aigateway:draft:{draft_id}`
    - _需求: 3.6, 1.8_

  - [x]* 7.4 编写 Draft 工作流属性测试和单元测试
    - **Property 11: 视频草图关键帧数量下界不变量** — 验证至少 2 帧，且总数 = max(2, ceil(duration / interval))
    - **Property 12: 重新生成次数上限** — 验证达到 max_regeneration_attempts 后拒绝再次生成
    - 单元测试: 草图过期清理、确认后放大、被拒绝草图不缓存并释放资源
    - **验证: 需求 3.2, 3.5, 3.9**

- [x] 8. 检查点 - 压缩、缓存和 Draft 工作流
  - 确保所有测试通过，ask the user if questions arise.

- [x] 9. 实现成本追踪与 Prometheus 指标
  - [x] 9.1 实现 GenerationCostTracker
    - 实现 `record_model_routing_saving()`: premium_price - actual_price
    - 实现 `record_token_compression_saving()`: (original - compressed) × per_token_price
    - 实现 `record_prompt_optimization_saving()`: 减少重试节省 - AI Director 调用成本（retry_rate 默认 0.3）
    - 精度: 6 位小数 (USD)
    - 计算失败时记录零节省并继续
    - _需求: 7.1, 7.4, 7.5_

  - [x] 9.2 实现 Prometheus 指标上报和 API Key 分组
    - 注册指标: `gen_opt_savings_usd_total` (counter, labels: strategy, api_key_group), `gen_opt_invocations_total` (counter, labels: strategy, api_key_group), `gen_opt_net_savings_usd` (gauge), `gen_opt_prompt_optimizations_total` (counter), `gen_opt_director_cost_usd_total` (counter, labels: model)
    - 支持 API Key 的 `group` 字段作为 Prometheus label
    - 未分组的 API Key 使用 "default" 作为 group 标签
    - 支持按 API Key group 过滤和聚合成本指标
    - _需求: 7.2, 7.3, 9.1, 9.2, 9.3, 9.4_

  - [x] 9.3 实现 CostTrackerPlugin
    - `depends_on=["gen_model_router"]`, 创建子 span
    - 汇总各策略节省并记录到请求元数据和 Prometheus
    - _需求: 7.1, 1.8_

  - [x]* 9.4 编写成本追踪属性测试
    - **Property 21: 模型路由成本节省计算** — 验证节省 = premium_price - actual_price
    - **Property 22: 成本计算失败安全记录** — 验证计算失败时记录零节省并不中断请求
    - **验证: 需求 7.4, 7.5**

- [x] 10. 实现提示词模板管理
  - [x] 10.1 实现 PromptTemplateManager
    - 实现 CRUD 方法: `create()`, `get()`, `list()`, `update()`, `delete()`
    - Redis 存储，Key: `aigateway:prompt_template:{api_key_id}:{template_name}`
    - 模板名称验证: 1-64 字符，字母数字/连字符/下划线
    - 内容最大 10000 字符，描述最大 500 字符
    - 分页查询（默认 20 条/页，最大 100 条）
    - 实现 `render()` 方法: 替换 `{{variable_name}}` 占位符
    - _需求: 8.1, 8.2, 8.3, 8.4_

  - [x] 10.2 实现模板 API 端点和权限校验
    - 实现 REST API 端点: POST/GET/PUT/DELETE `/templates`
    - 同 API Key 内模板名称唯一性校验
    - 跨 API Key 访问控制: 拒绝更新/删除非自己 API Key 的模板
    - 引用不存在模板时返回错误
    - 缺失占位符变量时返回验证错误（列出缺失变量名）
    - _需求: 8.5, 8.6, 8.7, 8.8_

  - [x]* 10.3 编写模板管理属性测试
    - **Property 23: 模板占位符完整替换** — 验证所有占位符被替换，无残留 `{{...}}`
    - **Property 24: 缺失模板变量检测** — 验证缺失变量时返回错误并列出缺失变量名
    - **Property 25: 模板名称 API Key 内唯一性** — 验证同 API Key 不允许重名，不同 API Key 允许
    - **Property 26: 跨 API Key 模板访问控制** — 验证跨 Key 更新/删除被拒绝
    - **验证: 需求 8.4, 8.6, 8.7, 8.8**

- [x] 11. 实现 API Key 分组管理
  - [x] 11.1 实现 API Key group 字段支持
    - 在 API Key 配置中支持可选 `group` 字段
    - group 字段不影响资源隔离逻辑（模板、缓存仍按单独 API Key 隔离）
    - 未分组 API Key 使用 "default" 标签
    - 将 group 标签注入成本追踪指标
    - _需求: 9.1, 9.2, 9.4, 9.5_

- [x] 12. 检查点 - 成本追踪、模板和分组
  - 确保所有测试通过，ask the user if questions arise.

- [x] 13. 全链路追踪集成与管线组装
  - [x] 13.1 实现全链路追踪 trace_id 贯穿
    - 每个插件 `execute()` 方法从 `ctx.trace_id` 获取 trace_id
    - 通过 `TracingManager.create_plugin_span()` 创建子 span
    - 子 span 记录策略特定属性（complexity_score、compression_ratio、routing_decision 等）
    - 异常时通过 `mark_span_error()` 标记
    - 下游 LLM 调用通过 `inject_trace_context()` 传播 trace_id
    - _需求: 1.8_

  - [x] 13.2 注册所有插件到 PipelineEngine
    - 将 6 个优化插件注册到 PluginRegistry: ai_director, intent_evaluator, token_compressor, draft_generator, gen_model_router, cost_tracker
    - 声明正确的 `depends_on` 依赖关系确保拓扑排序
    - 根据配置启用/禁用各插件
    - 禁用的策略跳过并传递到下一阶段
    - _需求: 6.1, 6.6_

  - [x]* 13.3 编写管线集成测试
    - 测试完整管线端到端流程（Mock 外部模型调用）
    - 验证插件拓扑排序正确性
    - 验证 trace_id 在所有阶段传播
    - 验证各策略独立禁用时管线正常运行
    - **验证: 需求 1.8, 6.1, 6.6**

- [x] 14. 最终检查点 - 全部集成验证
  - 确保所有测试通过，ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务，可跳过以加速 MVP 交付
- 每个任务引用了具体的需求条款以确保可追溯性
- 模型路由基于 model_capabilities 评分 + 价格动态选择，非固定三层
- 模型模态分类为 llm（纯文本）、mllm（多模态理解）、generative（生成）三大类
- 用户隔离基于 API Key（api_key_id），非 user_id
- 视频关键帧按时间间隔动态生成（默认每 5 秒一帧，最少 2 帧）
- 全链路 trace_id 贯穿所有优化阶段
- Prompt 确认流程可通过配置开关控制
- 被拒绝的草图不缓存、立即释放资源
- 压缩率可配置（20%-90%）
- 属性测试验证核心正确性，单元测试验证具体示例和边界

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "2.1"] },
    { "id": 3, "tasks": ["3.1", "4.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "4.2"] },
    { "id": 5, "tasks": ["3.4", "4.3"] },
    { "id": 6, "tasks": ["4.4", "6.1", "6.2"] },
    { "id": 7, "tasks": ["6.3", "6.4"] },
    { "id": 8, "tasks": ["7.1"] },
    { "id": 9, "tasks": ["7.2", "7.3"] },
    { "id": 10, "tasks": ["7.4", "9.1"] },
    { "id": 11, "tasks": ["9.2", "9.3"] },
    { "id": 12, "tasks": ["9.4", "10.1"] },
    { "id": 13, "tasks": ["10.2", "10.3", "11.1"] },
    { "id": 14, "tasks": ["13.1"] },
    { "id": 15, "tasks": ["13.2"] },
    { "id": 16, "tasks": ["13.3"] }
  ]
}
```
