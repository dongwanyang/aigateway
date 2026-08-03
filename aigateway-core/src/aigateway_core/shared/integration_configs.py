"""
集成配置 — 开源工具集成配置数据模型
====================================

模型名称、网络地址、文件系统路径和报告点名的工作流资源由 config.yaml
或环境变量提供。通用算法参数与尚未迁移的生成模型保持现有兼容默认值，
后续按报告顺序继续外置。
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

    ``scheduler_managed`` is derived from deployment topology rather than user
    input. It is true only when this endpoint represents a local worker in the
    Gateway GPU pool; external/remote ComfyUI endpoints must never drain or lock
    local Gateway devices.
    """

    server_url: str = ""
    public_url: str = ""
    manager_enabled: bool = True
    connect_timeout: int = 10
    execution_timeout: int = 1200
    ws_reconnect_attempts: int = 3
    required: bool = True
    scheduler_managed: bool = False
    workflow_version: str = ""
    checkpoint_name: str = ""
    allowed_checkpoints: list[str] = field(default_factory=list)
    # Per-checkpoint server-side minimum VRAM for dynamically selectable
    # standard-workflow checkpoints. A discovered file is not selectable
    # unless it is both allowlisted and present in this mapping.
    checkpoint_vram_gb: dict[str, float] = field(default_factory=dict)
    max_concurrency: int = 1
    min_free_gb: float = 30.0
    model_budget_gb: float = 80.0
    output_budget_gb: float = 10.0
    output_retention_hours: int = 24
    models_path: str = ""
    output_path: str = ""
    workflow_path: str = ""
    upscale_enabled: bool = True
    upscale_model: str = ""
    allowed_upscale_models: list[str] = field(default_factory=list)
    max_upscale_long_edge: int = 4096
    sdxl_required_vram_gb: float = 8.0
    upscale_required_vram_gb: float = 4.0
    qwen_image_enabled: bool = True
    qwen_image_auto_select: bool = False
    qwen_image_diffusion_model: str = "qwen_image_fp8_e4m3fn.safetensors"
    qwen_image_text_encoder: str = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    qwen_image_vae: str = "qwen_image_vae.safetensors"
    qwen_image_draft_steps: int = 12
    qwen_image_max_draft_edge: int = 768
    qwen_image_required_vram_gb: float = 12.0
    allowed_qwen_image_diffusion_models: list[str] = field(
        default_factory=lambda: ["qwen_image_fp8_e4m3fn.safetensors"]
    )
    allowed_qwen_image_text_encoders: list[str] = field(
        default_factory=lambda: ["qwen_2.5_vl_7b_fp8_scaled.safetensors"]
    )
    allowed_qwen_image_vaes: list[str] = field(
        default_factory=lambda: ["qwen_image_vae.safetensors"]
    )
    video_enabled: bool = True
    video_workflow_version: str = ""
    video_diffusion_model: str = "wan2.2_ti2v_5B_fp16.safetensors"
    video_text_encoder: str = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    video_vae: str = "wan2.2_vae.safetensors"
    allowed_video_diffusion_models: list[str] = field(
        default_factory=lambda: ["wan2.2_ti2v_5B_fp16.safetensors"]
    )
    allowed_video_text_encoders: list[str] = field(
        default_factory=lambda: ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"]
    )
    allowed_video_vaes: list[str] = field(
        default_factory=lambda: ["wan2.2_vae.safetensors"]
    )
    video_width: int = 512
    video_height: int = 288
    video_frames: int = 17
    video_fps: float = 8.0
    video_steps: int = 20
    video_cfg: float = 5.0
    video_shift: float = 8.0
    video_execution_timeout: int = 1200
    video_required_vram_gb: float = 12.0


@dataclass
class RAGRetrieverConfig:
    """LlamaIndex RAG 检索配置。"""

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
    qdrant_url: str = ""
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
