# Requirements Document

## Introduction

本需求描述将 7 个开源工具集成到现有 Enterprise Multimodal AI Gateway 项目中，并调整 Prompt Compress 插件在理解型管道中的执行位置。集成后，各占位实现将被真实的 ML 推理引擎替代，同时新增 RAG 检索和对话历史压缩两个管道插件。所有集成均遵循现有的 fail-open（故障透传）设计原则。

## Glossary

- **Gateway**: AI Gateway 系统，位于客户端和 LLM 提供商之间的智能代理服务
- **Understanding_Pipeline**: 理解型管道，处理 Chat/Completion 请求的插件链
- **Generation_Pipeline**: 生成优化管道，处理图片/视频生成请求的插件链
- **Plugin**: 管道中的执行单元，实现 `async execute(ctx: PipelineContext) -> PipelineContext` 接口
- **PipelineContext**: 在管道插件间传递的上下文对象，包含请求数据和中间处理结果
- **Prompt_Compress_Plugin**: 理解型管道中的 Prompt 压缩插件，负责在 LLM 调用前压缩冗长提示词
- **LLMLingua**: 微软开源的 Token 级提示词压缩库，使用小型语言模型实现高压缩率
- **Token_Compressor**: 生成优化管道中的视觉 Token 压缩策略，对参考图进行语义级特征提取
- **CLIP**: OpenAI 发布的对比语言-图像预训练模型，能将图像编码为语义特征向量
- **SigLIP**: Google 改进版 CLIP，具有更好的 zero-shot 性能
- **ComfyUI**: 开源的 Stable Diffusion 节点式工作流引擎，提供 WebSocket/REST API
- **Draft_Generator**: 生成优化管道中的渐进式生成策略，管理草图到高清的工作流
- **LlamaIndex**: 开源的 RAG 框架，提供文档加载、索引、检索、重排序能力
- **RAG_Retriever_Plugin**: 新增的理解型管道插件，负责从向量数据库检索相关文档上下文
- **Conv_Compressor_Plugin**: 新增的理解型管道插件，负责压缩长对话历史为摘要
- **LangChain**: 开源的 LLM 应用开发框架，提供 ConversationSummaryBufferMemory 等组件
- **PaddleOCR**: 百度开源的 OCR 工具包，对中文识别精度优于 Tesseract
- **Unstructured**: 开源的文档解析库，提供统一接口处理 PDF/DOCX/PPTX/HTML/CSV/Markdown
- **Qdrant**: 已部署的向量数据库，用于语义缓存和 RAG 向量检索
- **Redis**: 已部署的缓存服务，用于 L2 缓存和特征向量缓存
- **Feature_Vector**: 图像经 CLIP 编码后的语义特征向量，用于表示图像内容
- **Feature_Cache_Manager**: 已实现的特征向量缓存管理器，基于 Redis 存储

## Requirements

### 需求 1: Prompt Compress 管道位置调整

**用户故事:** 作为系统架构师，我希望 Prompt Compress 插件在缓存查找和内容丰富阶段之后执行，以便对包含 RAG 上下文和对话历史的完整提示词进行压缩，而非仅压缩原始用户输入。

#### 验收标准

1. THE Prompt_Compress_Plugin SHALL declare `depends_on` as `["semantic_cache"]` in the plugin configuration
2. WHEN RAG_Retriever_Plugin and Conv_Compressor_Plugin are implemented, THE Prompt_Compress_Plugin SHALL declare `depends_on` as `["rag_retriever", "conv_compressor"]`
3. THE Prompt_Compress_Plugin SHALL execute AFTER semantic cache lookup and content enrichment stages in the Understanding_Pipeline
4. THE Prompt_Compress_Plugin SHALL execute BEFORE model_router in the Understanding_Pipeline
5. THE Prompt_Compress_Plugin SHALL compress the full prompt content including system messages, conversation history, user message, and any RAG-injected context

### 需求 2: LLMLingua-2 提示词压缩集成

**用户故事:** 作为开发者，我希望 Prompt Compress 插件使用 LLMLingua-2 进行真实的 Token 级压缩，以便显著降低发送到 LLM 的 Token 数量和 API 调用成本。

#### 验收标准

1. THE Prompt_Compress_Plugin SHALL use the `llmlingua` Python package (LLMLingua-2) as the compression engine
2. THE Prompt_Compress_Plugin SHALL apply token-level compression using LLMLingua-2's small language model
3. THE Prompt_Compress_Plugin SHALL support a configurable `compression_ratio` parameter with a default value of 0.5
4. WHEN compression is executed, THE Prompt_Compress_Plugin SHALL compress the full prompt including system messages, conversation history, user message, and RAG context as a single coherent text block
5. IF LLMLingua-2 compression fails or raises an exception, THEN THE Prompt_Compress_Plugin SHALL pass through the original uncompressed prompt and log a warning
6. IF the `llmlingua` package is not installed, THEN THE Prompt_Compress_Plugin SHALL operate in passthrough mode and log a warning at startup
7. WHEN compression completes successfully, THE Prompt_Compress_Plugin SHALL record the original token count, compressed token count, and actual compression ratio in PipelineContext

### 需求 3: CLIP/SigLIP 视觉特征提取集成

**用户故事:** 作为开发者，我希望 Token Compressor 使用真实的 CLIP 模型提取图像语义特征向量，以替代当前基于哈希的占位实现，从而获得有意义的视觉语义表示。

#### 验收标准

1. THE Token_Compressor SHALL use `transformers` CLIPModel or `sentence-transformers` clip-ViT-L-14 to extract semantic feature vectors from reference images
2. WHEN an image is processed, THE Token_Compressor SHALL perform real visual feature extraction instead of hash-based placeholder generation
3. THE Token_Compressor SHALL produce feature vectors that capture semantic visual information of the image content
4. THE Token_Compressor SHALL cache extracted feature vectors in Redis via the existing Feature_Cache_Manager
5. IF the CLIP model is not available or feature extraction fails, THEN THE Token_Compressor SHALL fall back to the existing hash-based deterministic feature generation and log a warning
6. THE Token_Compressor SHALL load the CLIP model once at initialization and reuse the loaded model instance across requests
7. THE Token_Compressor SHALL respect the existing `max_vector_dimensions` configuration to truncate or project feature vectors to the configured dimension limit

### 需求 4: ComfyUI API 草图生成集成

**用户故事:** 作为开发者，我希望 Draft Generator 通过 ComfyUI API 执行真实的图像/视频生成，以替代当前返回占位字节的实现，让用户能够预览真实的低分辨率草图。

#### 验收标准

1. THE Draft_Generator SHALL connect to ComfyUI via WebSocket and REST API for workflow submission and result retrieval
2. WHEN an image draft is requested, THE Draft_Generator SHALL build and submit a ComfyUI workflow JSON that generates a 512x512 low-resolution preview image
3. WHEN a draft is confirmed for upscale, THE Draft_Generator SHALL build and submit a ComfyUI workflow JSON using Real-ESRGAN or SUPIR upscaling nodes
4. WHEN a video draft is requested, THE Draft_Generator SHALL build and submit a ComfyUI workflow JSON using AnimateDiff or LTX-Video nodes for keyframe generation
5. THE Draft_Generator SHALL support the existing confirm/reject lifecycle via draft_id
6. IF ComfyUI API is unavailable or workflow execution fails, THEN THE Draft_Generator SHALL fall back to the existing placeholder implementation and log a warning
7. THE Draft_Generator SHALL support configurable ComfyUI server URL and connection timeout parameters

### 需求 5: LlamaIndex RAG 检索插件

**用户故事:** 作为开发者，我希望在理解型管道中增加 RAG 检索插件，自动从向量数据库中检索与用户查询相关的文档上下文，以增强 LLM 的回答准确性。

#### 验收标准

1. THE RAG_Retriever_Plugin SHALL implement the standard Plugin interface with `async execute(ctx: PipelineContext) -> PipelineContext`
2. THE RAG_Retriever_Plugin SHALL use LlamaIndex VectorStoreIndex with Qdrant as the vector store backend
3. WHEN a user query is received, THE RAG_Retriever_Plugin SHALL retrieve the top-K most relevant document chunks from Qdrant
4. THE RAG_Retriever_Plugin SHALL support configurable parameters including `top_k`, `similarity_threshold`, and `rerank_enabled`
5. WHEN `rerank_enabled` is true, THE RAG_Retriever_Plugin SHALL apply a reranking step to improve retrieval precision
6. THE RAG_Retriever_Plugin SHALL inject retrieved context into PipelineContext for downstream plugins (including Prompt_Compress_Plugin) to consume
7. IF Qdrant is unavailable or retrieval fails, THEN THE RAG_Retriever_Plugin SHALL pass through the original context without injecting any RAG content and log a warning
8. THE RAG_Retriever_Plugin SHALL declare `depends_on` as `["semantic_cache"]` in the Understanding_Pipeline
9. THE RAG_Retriever_Plugin SHALL support document loading, text chunking, embedding generation, and index upsert operations for populating the vector store

### 需求 6: LangChain 对话历史压缩插件

**用户故事:** 作为开发者，我希望在理解型管道中增加对话历史压缩插件，将过长的对话历史自动摘要压缩，以减少上下文 Token 消耗同时保留对话语义。

#### 验收标准

1. THE Conv_Compressor_Plugin SHALL implement the standard Plugin interface with `async execute(ctx: PipelineContext) -> PipelineContext`
2. THE Conv_Compressor_Plugin SHALL use LangChain's ConversationSummaryBufferMemory to summarize long conversation history
3. WHEN conversation history message count exceeds configurable `max_history` threshold, THE Conv_Compressor_Plugin SHALL compress older messages into a summary
4. THE Conv_Compressor_Plugin SHALL support configurable `max_history` (message count threshold) and `summary_interval` (summarization trigger frequency) parameters
5. WHEN compression is applied, THE Conv_Compressor_Plugin SHALL replace the original long conversation history in PipelineContext with the compressed version (summary + recent messages)
6. IF LangChain summarization fails, THEN THE Conv_Compressor_Plugin SHALL pass through the original conversation history and log a warning
7. THE Conv_Compressor_Plugin SHALL declare `depends_on` as `["semantic_cache"]` in the Understanding_Pipeline

### 需求 7: PaddleOCR 中文 OCR 升级

**用户故事:** 作为开发者，我希望将图像管线的 OCR 引擎从 Tesseract 升级为 PaddleOCR，以获得更高的中文文字识别精度和表格检测能力。

#### 验收标准

1. WHEN the `ocr_backend` is configured as `"paddleocr"`, THE OCRExtractor SHALL use PaddleOCR as the primary OCR engine
2. THE OCRExtractor SHALL achieve improved Chinese text recognition accuracy compared to Tesseract through PaddleOCR's detection and recognition models
3. WHEN PaddleOCR detects table structures in an image, THE OCRExtractor SHALL preserve table layout information in the extracted text output
4. IF PaddleOCR is not installed or initialization fails, THEN THE OCRExtractor SHALL fall back to Tesseract as the OCR backend and log a warning
5. THE OCRExtractor SHALL support `"paddleocr"` and `"tesseract"` as valid `ocr_backend` configuration values
6. WHEN using PaddleOCR, THE OCRExtractor SHALL support the same `languages` configuration parameter for specifying recognition languages

### 需求 8: Unstructured 文档解析升级

**用户故事:** 作为开发者，我希望将文档解析器从多库组合（PyMuPDF + python-docx + BeautifulSoup）替换为 Unstructured 统一接口，以简化维护并获得更好的布局分析和表格提取能力。

#### 验收标准

1. WHEN the `unstructured` package is available, THE DocumentParser SHALL use Unstructured as the primary document parsing engine
2. THE DocumentParser SHALL support PDF, DOCX, PPTX, HTML, CSV, and Markdown formats through Unstructured's unified `partition` interface
3. WHEN parsing documents with complex layouts, THE DocumentParser SHALL leverage Unstructured's auto layout analysis to preserve document structure
4. WHEN parsing documents containing tables, THE DocumentParser SHALL extract table content with structure preserved
5. IF the `unstructured` package is not installed, THEN THE DocumentParser SHALL fall back to the existing multi-library implementation (PyMuPDF + python-docx + BeautifulSoup) and log a warning
6. THE DocumentParser SHALL produce output compatible with the existing TextChunker downstream processor

### 需求 9: 统一参数配置与默认值

**用户故事:** 作为运维人员，我希望所有新集成的开源工具的参数均可通过 YAML 配置文件自定义，同时提供合理的默认值开箱即用，以便在不修改任何配置的情况下即可启动服务。

#### 验收标准

1. WHEN a new open-source integration is added, THE configuration system SHALL define a dedicated config dataclass with all tunable parameters and sensible default values
2. THE configuration system SHALL allow all integration parameters to be overridden via `config.yaml` under corresponding sections (e.g., `plugins.[name].config.*`, `media_optimization.*`, `generation_optimization.*`)
3. THE configuration system SHALL allow environment variables with prefix `AI_GATEWAY_` to override any YAML-defined parameter value
4. WHEN a configuration parameter is not specified in YAML or environment variables, THE system SHALL use the default value defined in the config dataclass
5. THE configuration system SHALL validate parameter types and value ranges at load time; IF a value is invalid, THEN the system SHALL retain the previous valid value and log a warning
6. WHEN `hot_reload` is enabled, THE configuration system SHALL apply parameter changes within 5 seconds without restarting the service
7. THE following integration-specific parameters SHALL be configurable with these defaults:
   - LLMLingua: `compression_ratio` (default: 0.5), `model_name` (default: "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"), `target_token` (default: -1, meaning auto), `force_tokens` (default: [])
   - CLIP: `model_name` (default: "openai/clip-vit-large-patch14"), `device` (default: "cpu"), `batch_size` (default: 1)
   - ComfyUI: `server_url` (default: "http://localhost:8188"), `connect_timeout` (default: 10), `execution_timeout` (default: 300)
   - LlamaIndex RAG: `top_k` (default: 5), `similarity_threshold` (default: 0.7), `rerank_enabled` (default: false), `chunk_size` (default: 512), `chunk_overlap` (default: 64)
   - Conv Compressor: `max_history` (default: 20), `summary_model` (default: "gpt-4o-mini"), `max_token_limit` (default: 4000)
   - PaddleOCR: `lang` (default: "ch"), `use_angle_cls` (default: true), `det_model_dir` (default: null, use built-in), `rec_model_dir` (default: null, use built-in)
   - Unstructured: `strategy` (default: "auto"), `languages` (default: ["chi_sim", "eng"]), `extract_images` (default: false)
