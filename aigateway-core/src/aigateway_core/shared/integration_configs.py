"""
集成配置 — 开源工具集成配置数据模型
====================================

这些 dataclass 只提供类型、安全开关和通用算法约束。模型名称、网络地址、
文件系统路径、工作流版本及模型文件名必须由 config.yaml 或环境变量提供，
避免库代码对具体部署作出隐式假设。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PromptCompressConfig:
    """LLMLingua-2 Prompt 压缩配置。"""

    enabled: bool = True
    compression_ratio: float = 0.5
    model_name: str = ""
    target_token: int = -1
    force_tokens: list[str] = field(default_factory=list)
    device: str = "cpu"


@dataclass
class CLIPConfig:
    """CLIP 视觉特征提取配置。"""

    model_name: str = ""
    device: str = "cpu"
    batch_size: int = 1


@dataclass
class ComfyUIConfig:
    """ComfyUI API 连接与工作流配置。

    URL、路径、工作流版本和模型文件均是部署配置，缺失时保留为空，调用方
    应禁用对应能力或报告 ``config_missing``，不得回退到 localhost 或固定模型。
    """

    server_url: str = ""
    public_url: str = ""
    manager_enabled: bool = False
    connect_timeout: int = 10
    execution_timeout: int = 1200
    ws_reconnect_attempts: int = 3
    required: bool = False
    workflow_version: str = ""
    checkpoint_name: str = ""
    allowed_checkpoints: list[str] = field(default_factory=list)
    max_concurrency: int = 1
    min_free_gb: float = 30.0
    model_budget_gb: float = 80.0
    output_budget_gb: float = 10.0
    output_retention_hours: int = 24
    models_path: str = ""
    output_path: str = ""
    workflow_path: str = ""
    upscale_enabled: bool = False
    upscale_model: str = ""
    allowed_upscale_models: list[str] = field(default_factory=list)
    max_upscale_long_edge: int = 4096
    qwen_image_enabled: bool = False
    qwen_image_diffusion_model: str = ""
    qwen_image_text_encoder: str = ""
    qwen_image_vae: str = ""
    qwen_image_draft_steps: int = 12
    qwen_image_max_draft_edge: int = 768
    allowed_qwen_image_diffusion_models: list[str] = field(default_factory=list)
    allowed_qwen_image_text_encoders: list[str] = field(default_factory=list)
    allowed_qwen_image_vaes: list[str] = field(default_factory=list)
    video_enabled: bool = False
    video_workflow_version: str = ""
    video_diffusion_model: str = ""
    video_text_encoder: str = ""
    video_vae: str = ""
    allowed_video_diffusion_models: list[str] = field(default_factory=list)
    allowed_video_text_encoders: list[str] = field(default_factory=list)
    allowed_video_vaes: list[str] = field(default_factory=list)
    video_width: int = 512
    video_height: int = 288
    video_frames: int = 17
    video_fps: float = 8.0
    video_steps: int = 20
    video_cfg: float = 5.0
    video_shift: float = 8.0
    video_execution_timeout: int = 1200


@dataclass
class RAGRetrieverConfig:
    """LlamaIndex RAG 检索配置。

    模型、集合名称、远程地址和 CodeGraph 路径由 YAML 提供。数值字段保留
    通用算法默认值，便于可选插件在未完整配置时安全降级。
    """

    enabled: bool = True
    top_k: int = 5
    similarity_threshold: float = 0.7
    rerank_enabled: bool = False
    rerank_model: str = ""
    rerank_device: str = "auto"
    rerank_backend: str = "local"
    rerank_api_base: str | None = None
    rerank_api_key: str | None = None
    chunk_size: int = 512
    chunk_overlap: int = 64
    collection_name: str = ""
    embedding_backend: str = "local"
    embedding_model: str = ""
    embedding_device: str = "auto"
    embedding_api_base: str | None = None
    embedding_api_key: str | None = None
    code_rag_enabled: bool = False
    code_rag_graph_hops: int = 2
    code_rag_top_k: int = 5
    code_graph_db_dir: str = ""


@dataclass
class ConvCompressorConfig:
    """对话历史压缩配置。"""

    enabled: bool = True
    max_history: int = 20
    summary_model: str = ""
    max_token_limit: int = 4000
    summary_interval: int = 5
    api_base: str = ""
    api_key: str | None = None


@dataclass
class PaddleOCRConfig:
    """PaddleOCR 配置。"""

    lang: str = "ch"
    use_angle_cls: bool = True
    det_model_dir: str | None = None
    rec_model_dir: str | None = None


@dataclass
class UnstructuredConfig:
    """Unstructured 文档解析配置。"""

    strategy: str = "auto"
    languages: list[str] = field(default_factory=lambda: ["chi_sim", "eng"])
    extract_images: bool = False
