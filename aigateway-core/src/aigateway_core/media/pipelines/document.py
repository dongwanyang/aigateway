"""
DocumentPipeline — 文档处理管线
================================

处理器链:
1. DocumentParser — 解析为纯文本
2. TextChunker — 智能分块
3. DocumentSummarizer — 长文档摘要
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, List, Optional

from ..base import MediaPipeline, MediaProcessor
from ..config import DocumentPipelineConfig
from ..types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

if TYPE_CHECKING:
    from ...context import PipelineContext
    from ...integration_configs import UnstructuredConfig

logger = logging.getLogger(__name__)


class DocumentParser(MediaProcessor):
    """文档解析处理器 — 优先使用 Unstructured，回退到多库组合。

    当 ``unstructured`` 包可用时，通过其统一 ``partition`` 接口处理所有
    支持的文档格式（PDF、DOCX、PPTX、HTML、CSV、Markdown），并保留
    表格 HTML 和布局元素等结构信息。

    当 ``unstructured`` 不可用时，回退到已有的 PyMuPDF + python-docx +
    BeautifulSoup 多库实现。
    """

    name = "document_parser"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.DOCUMENT]

    def __init__(
        self,
        supported_formats: Optional[List[str]] = None,
        config: Optional["UnstructuredConfig"] = None,
    ) -> None:
        self.supported_formats = supported_formats or [
            "pdf", "docx", "xlsx", "pptx", "md", "csv", "html"
        ]
        # 延迟导入以避免循环依赖
        if config is None:
            from ...integration_configs import UnstructuredConfig
            config = UnstructuredConfig()
        self._config = config
        self._unstructured_available = self._check_unstructured()

    @staticmethod
    def _check_unstructured() -> bool:
        """检测 unstructured 包是否可用。"""
        try:
            from unstructured.partition.auto import partition  # noqa: F401
            return True
        except ImportError:
            logger.warning("Unstructured 未安装，回退到多库实现")
            return False

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.DOCUMENT

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """解析文档。"""
        start = time.monotonic()
        try:
            doc_data = content.raw_data
            if doc_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No document data",
                )

            mime = content.mime_type or ""
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None, self._parse, doc_data, mime
            )

            elapsed = (time.monotonic() - start) * 1000
            if text:
                return ProcessorResult(
                    success=True,
                    processor_name=self.name,
                    duration_ms=elapsed,
                    output=text,
                )
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="Failed to extract text from document",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _parse(self, data: bytes, mime_type: str) -> str:
        """同步解析文档 — Unstructured 优先，失败回退到 legacy 实现。"""
        if self._unstructured_available:
            try:
                return self._parse_with_unstructured(data, mime_type)
            except Exception as exc:
                logger.warning(
                    "Unstructured 解析异常，回退到多库实现: %s", exc
                )
                return self._parse_legacy(data, mime_type)
        return self._parse_legacy(data, mime_type)

    def _parse_with_unstructured(self, data: bytes, mime_type: str) -> str:
        """使用 Unstructured partition 统一解析。

        保留结构信息：
        - 表格元素使用 ``text_as_html`` 元数据（如果可用）
        - 其他布局元素转为纯文本表示
        - 所有部分以双换行连接，确保与 TextChunker 兼容
        """
        from io import BytesIO

        from unstructured.partition.auto import partition

        kwargs: dict = {
            "file": BytesIO(data),
            "content_type": mime_type,
            "strategy": self._config.strategy,
            "languages": self._config.languages,
        }

        elements = partition(**kwargs)

        # 构建输出 — 保留结构信息
        text_parts: List[str] = []
        for element in elements:
            # 表格元素：优先使用 HTML 表示以保留结构
            if hasattr(element, "metadata"):
                metadata = element.metadata
                # unstructured 使用 text_as_html 保存表格的 HTML 表示
                html_text = getattr(metadata, "text_as_html", None)
                if html_text:
                    text_parts.append(html_text)
                    continue
            # 其他元素：使用纯文本表示
            text_repr = str(element)
            if text_repr.strip():
                text_parts.append(text_repr)

        return "\n\n".join(text_parts)

    def _parse_legacy(self, data: bytes, mime_type: str) -> str:
        """Legacy 解析 — 基于 MIME 类型分发到各专用库。"""
        if "pdf" in mime_type:
            return self._parse_pdf(data)
        elif "word" in mime_type or "docx" in mime_type:
            return self._parse_docx(data)
        elif "csv" in mime_type:
            return data.decode("utf-8", errors="replace")
        elif "markdown" in mime_type or "text/" in mime_type:
            return data.decode("utf-8", errors="replace")
        elif "html" in mime_type:
            return self._parse_html(data)
        else:
            # 尝试纯文本
            return data.decode("utf-8", errors="replace")

    def _parse_pdf(self, data: bytes) -> str:
        """解析 PDF。"""
        try:
            import fitz  # pymupdf

            doc = fitz.open(stream=data, filetype="pdf")
            texts = []
            for page in doc:
                texts.append(page.get_text())
            return "\n".join(texts)
        except ImportError:
            # Fallback: 尝试 PyPDF2
            try:
                from io import BytesIO
                from PyPDF2 import PdfReader

                reader = PdfReader(BytesIO(data))
                texts = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        texts.append(text)
                return "\n".join(texts)
            except ImportError:
                raise ImportError("pymupdf or PyPDF2 required for PDF parsing")

    def _parse_docx(self, data: bytes) -> str:
        """解析 DOCX。"""
        from io import BytesIO
        from docx import Document

        doc = Document(BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def _parse_html(self, data: bytes) -> str:
        """解析 HTML。"""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(data, "html.parser")
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            # 简单去 tag
            import re

            text = data.decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", "", text)


class TextChunker(MediaProcessor):
    """文本分块处理器。"""

    name = "text_chunker"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.DOCUMENT]

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        strategy: str = "token",
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.strategy = strategy

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.DOCUMENT

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """分块文本。"""
        start = time.monotonic()
        text = content.extracted_text or ""
        if not text:
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=0,
                error="No text to chunk",
            )

        chunks = self._chunk(text)
        elapsed = (time.monotonic() - start) * 1000
        return ProcessorResult(
            success=True,
            processor_name=self.name,
            duration_ms=elapsed,
            output=chunks,
        )

    def _chunk(self, text: str) -> List[str]:
        """按 token 大小分块（简化为按字符数）。"""
        chars_per_token = 4  # 粗略估算
        char_size = self.chunk_size * chars_per_token
        char_overlap = self.chunk_overlap * chars_per_token

        chunks: List[str] = []
        start = 0
        while start < len(text):
            end = start + char_size
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            start = end - char_overlap

        return chunks


# ------------------------------------------------------------------
# Document Pipeline
# ------------------------------------------------------------------


class DocumentPipeline(MediaPipeline):
    """文档处理管线。"""

    media_type = MediaType.DOCUMENT

    def __init__(self, config: Optional[DocumentPipelineConfig] = None) -> None:
        cfg = config or DocumentPipelineConfig()
        self.config = cfg
        self.parser = DocumentParser(
            supported_formats=cfg.supported_formats,
        )
        self.chunker = TextChunker(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            strategy=cfg.chunking_strategy,
        )
        self.processors = [self.parser, self.chunker]

    async def execute(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> MediaContent:
        """执行文档处理管线。"""
        if content.size_bytes > self.config.max_file_size_mb * 1024 * 1024:
            logger.warning("文档文件超过大小限制")
            return content

        # 下载文档（如果只有 URL）
        if content.raw_data is None and content.source_url:
            content.raw_data = await self._download(content.source_url)
            if content.raw_data:
                content.size_bytes = len(content.raw_data)

        if content.raw_data is None:
            return content

        # Step 1: 解析
        parsed = await self.parser.process(content, ctx)
        if not parsed.success:
            return content

        full_text: str = parsed.output

        # Step 2: 如果文档很长，生成摘要
        if len(full_text) > self.config.long_doc_threshold_chars:
            # 截取前 N 字符作为摘要
            content.extracted_text = (
                f"[文档内容摘要 (共 {len(full_text)} 字符)]:\n"
                f"{full_text[:self.config.summary_preview_chars]}..."
            )
        else:
            content.extracted_text = f"[文档内容]:\n{full_text}"

        content.metadata["chunks_count"] = len(full_text) // (self.config.chunk_size * 4)
        content.metadata["total_chars"] = len(full_text)

        return content

    async def _download(self, url: str) -> Optional[bytes]:
        """下载文档。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.config.download_timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.content
                return None
        except Exception as exc:
            logger.warning("文档下载失败: %s", exc)
            return None
