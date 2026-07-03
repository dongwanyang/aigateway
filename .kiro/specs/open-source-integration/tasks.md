# Implementation Plan: 开源工具集成

## Overview

将 7 个开源 ML/NLP 工具集成到现有 AI Gateway 管道架构中，调整 Prompt Compress 插件的执行位置，并新增 RAG 检索和对话历史压缩两个管道插件。所有集成遵循 fail-open 设计原则，使用可选依赖方式，保证未安装对应包时系统正常运行。

实现语言: Python 3.12，基于 FastAPI + async/await 异步模式。

## Tasks

- [x] 1. 配置基础设施和数据模型
  - [x] 1.1 创建集成配置 dataclass 文件
    - 在 `aigateway-core/src/aigateway_core/generation_optimization/` 下创建 `integration_configs.py`
    - 定义 `PromptCompressConfig`、`CLIPConfig`、`ComfyUIConfig`、`RAGRetrieverConfig`、`ConvCompressorConfig`、`PaddleOCRConfig`、`UnstructuredConfig` 七个 dataclass
    - 每个 dataclass 的字段默认值必须与需求文档 9.7 一致
    - _需求: 9.1, 9.4, 9.7_

  - [x] 1.2 扩展 YAML 配置加载逻辑
    - 修改 `aigateway-core/src/aigateway_core/config.py`，新增对应配置节的解析
    - 实现环境变量覆盖（`AI_GATEWAY_` 前缀）优先于 YAML 值
    - 实现类型校验和范围校验，无效值保留旧配置并记录警告
    - 支持 `hot_reload` 时 5 秒内应用配置变更
    - _需求: 9.2, 9.3, 9.5, 9.6_

  - [x]* 1.3 编写配置属性测试
    - **Property 13: 配置加载层级（YAML → 环境变量 → 默认值）**
    - **Property 14: 配置校验与旧值保留**
    - **Property 15: 配置默认值与文档一致**
    - **验证: 需求 9.2, 9.3, 9.4, 9.5, 9.7**

  - [x] 1.3.1 扩展 PipelineContext 命名空间
    - 在 `aigateway-core/src/aigateway_core/context.py` 中新增 `NS_RAG_RETRIEVER`、`NS_CONV_COMPRESSOR` 常量
    - 添加 `rag_context` 和 `conv_summary` 属性访问器
    - _需求: 5.6, 6.5_

  - [x] 1.4 更新 pyproject.toml 可选依赖
    - 添加 `llmlingua`、`clip`、`comfyui`、`llamaindex`、`langchain`、`paddleocr`、`unstructured`、`all-integrations` extras 组
    - _需求: 9.1_

- [x] 2. 管道位置调整与 LLMLingua-2 集成
  - [x] 2.1 调整 PromptCompressPlugin 依赖关系
    - 修改 `aigateway-core/src/aigateway_core/pipeline.py` 中 `PromptCompressPlugin.depends_on`
    - 初始阶段设为 `["semantic_cache"]`（后续 RAG/Conv 实现后改为 `["rag_retriever", "conv_compressor"]`）
    - 确保拓扑排序后 prompt_compress 在 semantic_cache 之后、model_router 之前执行
    - _需求: 1.1, 1.3, 1.4_

  - [ ]* 2.2 编写管道排序属性测试
    - **Property 1: 管道执行顺序不变量**
    - **验证: 需求 1.3, 1.4**

  - [x] 2.3 实现 LLMLingua-2 压缩引擎
    - 重构 `PromptCompressPlugin.__init__` 接受 `PromptCompressConfig`
    - 实现 `_build_prompt_text(messages)` — 将 messages 拼接为单一文本块（含 system/history/user/RAG）
    - 实现 `_rebuild_messages(compressed, original_messages)` — 重建压缩后 messages
    - 在 `execute()` 中调用 `llmlingua.PromptCompressor.compress_prompt()`
    - 记录 `original_tokens`、`compressed_tokens`、`compression_ratio` 到 `ctx.prompt_compress`
    - ImportError 时标记 passthrough 模式并记录 WARNING
    - 运行时异常时透传原始 prompt 并记录 WARNING
    - _需求: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 1.5_

  - [ ]* 2.4 编写 Prompt 压缩属性测试
    - **Property 2: Prompt 组装完整性**
    - **Property 4: 压缩指标记录不变量**
    - **验证: 需求 1.5, 2.4, 2.7**

  - [ ]* 2.5 编写 PromptCompressPlugin 单元测试
    - 测试 LLMLingua mock 正常压缩场景
    - 测试 ImportError passthrough 场景
    - 测试运行时异常 fallback 场景
    - _需求: 2.5, 2.6_

- [x] 3. 检查点 — 确保所有测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 4. CLIP 视觉特征提取集成
  - [x] 4.1 实现 CLIP 模型加载与特征提取
    - 修改 `aigateway-core/src/aigateway_core/generation_optimization/strategies/token_compressor.py`
    - 在 `__init__` 中实现 CLIP 模型一次性加载（`CLIPModel.from_pretrained` + `CLIPProcessor`）
    - 实现 `_do_compress` 中的真实特征提取流程：PIL 解码 → 预处理 → `get_image_features()` → 维度截断/投影
    - CLIP 不可用时回退到现有 hash-based 实现
    - 缓存提取的特征向量到 Redis（Feature_Cache_Manager）
    - _需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ]* 4.2 编写特征向量属性测试
    - **Property 5: 特征向量维度约束**
    - **Property 6: 特征向量缓存一致性**
    - **验证: 需求 3.4, 3.7**

  - [ ]* 4.3 编写 TokenCompressor 单元测试
    - 测试 CLIP mock 正常提取场景
    - 测试模型不可用 fallback 到 hash-based 场景
    - 测试 max_vector_dimensions 截断逻辑
    - _需求: 3.5, 3.7_

- [x] 5. ComfyUI API 草图生成集成
  - [x] 5.1 实现 ComfyUI 连接与工作流提交
    - 修改 `aigateway-core/src/aigateway_core/generation_optimization/strategies/draft_generator.py`
    - 实现 `_submit_workflow(workflow_json)` — POST /prompt 提交工作流
    - 实现 `_poll_result(prompt_id)` — WebSocket/轮询获取结果
    - 实现 `_check_comfyui()` — 初始化时检测 ComfyUI 可用性
    - _需求: 4.1, 4.7_

  - [x] 5.2 实现工作流 JSON 构建器
    - 实现 `_build_image_draft_workflow(request)` — 512x512 低分辨率图片生成
    - 实现 `_build_upscale_workflow(draft_data, target_resolution)` — Real-ESRGAN/SUPIR 放大
    - 实现 `_build_video_draft_workflow(request)` — AnimateDiff/LTX-Video 关键帧生成
    - ComfyUI 不可用时回退到现有 placeholder 实现
    - 支持 confirm/reject 生命周期（draft_id）
    - _需求: 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 5.3 编写 ComfyUI 工作流属性测试
    - **Property 7: ComfyUI 工作流结构正确性**
    - **验证: 需求 4.2, 4.3, 4.4**

  - [ ]* 5.4 编写 DraftGenerator 单元测试
    - 测试工作流 JSON 结构验证
    - 测试 ComfyUI 不可用 fallback 场景
    - 测试 WebSocket 重连逻辑
    - _需求: 4.6, 4.7_

- [x] 6. 检查点 — 确保所有测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 7. LlamaIndex RAG 检索插件
  - [x] 7.1 创建 RAGRetrieverPlugin 插件文件
    - 新建 `aigateway-core/src/aigateway_core/plugins/rag_retriever_plugin.py`
    - 实现标准 Plugin 接口 `async execute(ctx: PipelineContext) -> PipelineContext`
    - 初始化 LlamaIndex `VectorStoreIndex` + `QdrantVectorStore`
    - 配置 `depends_on: ["semantic_cache"]`
    - _需求: 5.1, 5.2, 5.8_

  - [x] 7.2 实现 RAG 检索与上下文注入
    - 实现用户查询提取 → `as_retriever(top_k)` 检索 → 可选 rerank → 结果注入
    - 将检索到的文档块写入 `ctx.extra["rag_retriever"]["retrieved_chunks"]`
    - 同时注入为 system message 前缀
    - Qdrant 不可用时透传原始上下文并记录 WARNING
    - _需求: 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 7.3 实现文档 Ingest 接口
    - 实现 `ingest_documents(documents)` — 文档加载、分块、embedding 生成、索引 upsert
    - 使用现有 embedding 配置（Qwen3-Embedding-0.6B）
    - _需求: 5.9_

  - [ ]* 7.4 编写 RAG 检索属性测试
    - **Property 8: RAG 检索结果数量约束**
    - **Property 9: RAG 上下文注入正确性**
    - **验证: 需求 5.3, 5.6**

  - [ ]* 7.5 编写 RAGRetrieverPlugin 单元测试
    - 测试正常检索场景（mock Qdrant）
    - 测试 Qdrant 不可用 fallback 场景
    - 测试 rerank 启用/禁用场景
    - _需求: 5.5, 5.7_

- [x] 8. LangChain 对话历史压缩插件
  - [x] 8.1 创建 ConvCompressorPlugin 插件文件
    - 新建 `aigateway-core/src/aigateway_core/plugins/conv_compressor_plugin.py`
    - 实现标准 Plugin 接口 `async execute(ctx: PipelineContext) -> PipelineContext`
    - 初始化 LangChain `ConversationSummaryBufferMemory`
    - 配置 `depends_on: ["semantic_cache"]`
    - _需求: 6.1, 6.2, 6.7_

  - [x] 8.2 实现对话压缩逻辑
    - 当消息数 > `max_history` 时触发压缩
    - 将旧消息摘要为 summary_message + 保留最近 N 条消息
    - LangChain 调用失败时透传原始对话历史
    - 记录压缩前后 token 数到 `ctx.extra["conv_compressor"]`
    - _需求: 6.3, 6.4, 6.5, 6.6_

  - [ ]* 8.3 编写对话压缩属性测试
    - **Property 10: 对话压缩阈值与近期消息保留**
    - **验证: 需求 6.3, 6.5**

  - [ ]* 8.4 编写 ConvCompressorPlugin 单元测试
    - 测试消息数未达阈值时不压缩
    - 测试超阈值时正确压缩并保留近期消息
    - 测试 LangChain 失败 fallback 场景
    - _需求: 6.3, 6.5, 6.6_

- [x] 9. 更新管道依赖关系
  - [x] 9.1 最终调整 PromptCompressPlugin depends_on
    - 将 `PromptCompressPlugin.depends_on` 从 `["semantic_cache"]` 更新为 `["rag_retriever", "conv_compressor"]`
    - 在 `_register_builtin_plugins` 中注册 `RAGRetrieverPlugin` 和 `ConvCompressorPlugin`
    - 验证拓扑排序后的执行顺序正确
    - _需求: 1.2, 1.3, 1.4_

- [x] 10. 检查点 — 确保所有测试通过
  - 确保所有测试通过，ask the user if questions arise.

- [x] 11. PaddleOCR 中文 OCR 升级
  - [x] 11.1 实现 PaddleOCR 后端集成
    - 修改 `aigateway-core/src/aigateway_core/media/pipelines.py` 中的 OCR 相关逻辑
    - 实现 `_init_paddleocr()` — 初始化引擎，失败回退 Tesseract
    - 实现 `_extract_paddleocr(image_data)` — PaddleOCR 提取，按位置排序保留表格布局
    - 支持 `ocr_backend` 配置项（`"paddleocr"` / `"tesseract"`）
    - 支持 `languages` 配置参数
    - _需求: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [ ]* 11.2 编写 OCRExtractor 单元测试
    - 测试 PaddleOCR 后端正常调用（mock）
    - 测试 PaddleOCR 未安装回退 Tesseract
    - 测试表格布局保留逻辑
    - _需求: 7.4, 7.5_

- [x] 12. Unstructured 文档解析升级
  - [x] 12.1 实现 Unstructured 解析后端
    - 修改 `aigateway-core/src/aigateway_core/media/pipelines.py` 中的文档解析逻辑
    - 实现 `_check_unstructured()` — 检测可用性
    - 实现 `_parse_with_unstructured(data, mime_type)` — 使用 `partition` 统一解析
    - 保留结构信息（表格 HTML、布局元素）
    - Unstructured 不可用时回退到现有多库实现
    - 确保输出与 TextChunker 兼容
    - _需求: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ]* 12.2 编写文档解析属性测试
    - **Property 11: 文档解析格式分发**
    - **Property 12: 文档解析输出与 TextChunker 兼容**
    - **验证: 需求 8.2, 8.6**

  - [ ]* 12.3 编写 DocumentParser 单元测试
    - 测试 Unstructured 可用时的正常解析
    - 测试 Unstructured 不可用回退多库实现
    - 测试各种 MIME 类型分发
    - _需求: 8.2, 8.5_

- [ ] 13. Fail-Open 统一属性测试
  - [ ]* 13.1 编写 Fail-Open 透传属性测试
    - **Property 3: Fail-Open 透传不变量**
    - 覆盖所有 7 个集成组件的 ImportError 和运行时异常场景
    - 验证 ctx 核心请求数据无损透传
    - **验证: 需求 2.5, 3.5, 4.6, 5.7, 6.6, 8.5**

- [x] 14. 集成联调与最终验证
  - [x] 14.1 更新 config.yaml 示例配置
    - 在 `config.yaml` 中添加所有新增集成的配置节
    - 包括 `plugins` 列表中的 rag_retriever、conv_compressor 配置
    - 包括 `media_optimization` 中的 paddleocr、unstructured 配置
    - 包括 `generation_optimization` 中的 clip、comfyui 配置
    - _需求: 9.2_

  - [x] 14.2 注册新插件到 pipeline 注册表
    - 在 `_register_builtin_plugins` 中注册 `RAGRetrieverPlugin` 和 `ConvCompressorPlugin`
    - 确保插件加载顺序和 depends_on 拓扑正确
    - _需求: 1.2, 5.8, 6.7_

- [x] 15. 最终检查点 — 确保所有测试通过
  - 确保所有测试通过，ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选任务，可跳过以加速 MVP 交付
- 每个任务引用了具体需求编号，确保可追溯性
- 所有集成使用 optional dependencies，未安装包时自动降级
- 属性测试使用 `hypothesis` 框架，mock 隔离外部依赖
- 单元测试使用 `pytest` + `unittest.mock` + `fakeredis`
- 检查点确保增量验证，避免问题累积
- 设计文档中定义了 15 个正确性属性，全部分配到对应任务中

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.4"] },
    { "id": 1, "tasks": ["1.2", "1.3.1"] },
    { "id": 2, "tasks": ["1.3", "2.1"] },
    { "id": 3, "tasks": ["2.2", "2.3"] },
    { "id": 4, "tasks": ["2.4", "2.5", "4.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "5.1"] },
    { "id": 6, "tasks": ["5.2"] },
    { "id": 7, "tasks": ["5.3", "5.4", "7.1", "8.1"] },
    { "id": 8, "tasks": ["7.2", "7.3", "8.2"] },
    { "id": 9, "tasks": ["7.4", "7.5", "8.3", "8.4"] },
    { "id": 10, "tasks": ["9.1"] },
    { "id": 11, "tasks": ["11.1", "12.1"] },
    { "id": 12, "tasks": ["11.2", "12.2", "12.3"] },
    { "id": 13, "tasks": ["13.1", "14.1", "14.2"] }
  ]
}
```
