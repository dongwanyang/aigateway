"""
集成配置 — 开源工具集成配置数据模型
====================================

定义 7 个开源集成工具的配置 dataclass，
每个 dataclass 的默认值与需求文档 9.7 一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PromptCompressConfig:
    """LLMLingua-2 Prompt 压缩配置。

    Attributes:
        enabled: 是否启用 Prompt 压缩 (默认: True)
        compression_ratio: 压缩率 (默认: 0.5)
        model_name: LLMLingua-2 使用的模型名称
        target_token: 目标 token 数，-1 表示自动 (默认: -1)
        force_tokens: 强制保留的 token 列表 (默认: [])
        device: 运行设备 (默认: "cpu")
    """

    enabled: bool = True
    compression_ratio: float = 0.5
    model_name: str = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
    target_token: int = -1
    force_tokens: List[str] = field(default_factory=list)
    device: str = "cpu"


@dataclass
class CLIPConfig:
    """CLIP 视觉特征提取配置。

    Attributes:
        model_name: CLIP 模型名称 (默认: "openai/clip-vit-large-patch14")
        device: 运行设备 (默认: "cpu")
        batch_size: 批量处理大小 (默认: 1)
    """

    model_name: str = "openai/clip-vit-large-patch14"
    device: str = "cpu"
    batch_size: int = 1


@dataclass
class ComfyUIConfig:
    """ComfyUI API 连接配置。

    Attributes:
        server_url: ComfyUI 服务地址 (默认: "http://localhost:8188")
        connect_timeout: 连接超时时间/秒 (默认: 10)
        execution_timeout: 工作流执行超时时间/秒 (默认: 300)
        ws_reconnect_attempts: WebSocket 重连尝试次数 (默认: 3)
    """

    server_url: str = "http://localhost:8188"
    connect_timeout: int = 10
    execution_timeout: int = 300
    ws_reconnect_attempts: int = 3


@dataclass
class RAGRetrieverConfig:
    """LlamaIndex RAG 检索配置。

    Attributes:
        enabled: 是否启用 RAG 检索 (默认: True)
        top_k: 检索返回的文档块数量 (默认: 5)
        similarity_threshold: 相似度阈值 (默认: 0.7)
        rerank_enabled: 是否启用重排序 (默认: False)
        rerank_model: 重排序模型名称
        chunk_size: 文档分块大小 (默认: 512)
        chunk_overlap: 分块重叠字符数 (默认: 64)
        collection_name: Qdrant 集合名称 (默认: "rag_documents")
    """

    enabled: bool = True
    top_k: int = 5
    similarity_threshold: float = 0.7
    rerank_enabled: bool = False
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    chunk_size: int = 512
    chunk_overlap: int = 64
    collection_name: str = "rag_documents"


@dataclass
class ConvCompressorConfig:
    """对话历史压缩配置。

    Attributes:
        enabled: 是否启用对话压缩 (默认: True)
        max_history: 消息数阈值，超过则触发压缩 (默认: 20)
        summary_model: 摘要生成使用的模型 (默认: "gpt-4o-mini")
        max_token_limit: 摘要最大 token 数 (默认: 4000)
        summary_interval: 每隔 N 条消息触发一次摘要 (默认: 5)
    """

    enabled: bool = True
    max_history: int = 20
    summary_model: str = "gpt-4o-mini"
    max_token_limit: int = 4000
    summary_interval: int = 5


@dataclass
class PaddleOCRConfig:
    """PaddleOCR 配置。

    Attributes:
        lang: 识别语言 (默认: "ch")
        use_angle_cls: 是否启用角度分类器 (默认: True)
        det_model_dir: 检测模型目录，None 使用内置模型
        rec_model_dir: 识别模型目录，None 使用内置模型
    """

    lang: str = "ch"
    use_angle_cls: bool = True
    det_model_dir: Optional[str] = None
    rec_model_dir: Optional[str] = None


@dataclass
class UnstructuredConfig:
    """Unstructured 文档解析配置。

    Attributes:
        strategy: 解析策略 (默认: "auto")，可选 "auto" | "fast" | "hi_res"
        languages: 识别语言列表 (默认: ["chi_sim", "eng"])
        extract_images: 是否提取文档中的图片 (默认: False)
    """

    strategy: str = "auto"
    languages: List[str] = field(default_factory=lambda: ["chi_sim", "eng"])
    extract_images: bool = False
