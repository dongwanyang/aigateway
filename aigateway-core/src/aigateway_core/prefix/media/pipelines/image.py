"""
ImagePipeline — 图像处理管线
=============================

处理器链:
1. ImageResizeProcessor — 缩放到目标分辨率
2. ImageCompressProcessor — 质量压缩（WebP/JPEG）
3. OCRExtractor — 文字提取
4. VisionCaptionProcessor — 使用 Vision Model 生成描述（降级为 passthrough）
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, List, Optional

from ..base import MediaPipeline, MediaProcessor
from ..config import ImagePipelineConfig
from ..types import MediaContent, MediaType, ProcessorPhase, ProcessorResult

if TYPE_CHECKING:
    from aigateway_core.dispatch.context import PipelineContext

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Processors
# ------------------------------------------------------------------


class ImageResizeProcessor(MediaProcessor):
    """图像缩放处理器。"""

    name = "image_resize"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.IMAGE]

    def __init__(self, max_width: int = 1920, max_height: int = 1080) -> None:
        self.max_width = max_width
        self.max_height = max_height

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.IMAGE

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """缩放图片到目标分辨率。"""
        start = time.monotonic()
        try:
            from PIL import Image

            image_data = content.optimized_data or content.raw_data
            if image_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No image data available",
                )

            loop = asyncio.get_event_loop()
            resized = await loop.run_in_executor(
                None, self._resize, image_data
            )

            elapsed = (time.monotonic() - start) * 1000
            if resized is not None:
                original_size = len(image_data)
                new_size = len(resized)
                savings = max(0, (original_size - new_size) // 4)
                return ProcessorResult(
                    success=True,
                    processor_name=self.name,
                    duration_ms=elapsed,
                    output=resized,
                    token_savings=savings,
                )
            return ProcessorResult(
                success=True,
                processor_name=self.name,
                duration_ms=elapsed,
                output=image_data,
            )
        except ImportError:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="Pillow not installed",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _resize(self, image_data: bytes) -> Optional[bytes]:
        """同步执行缩放。"""
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        w, h = img.size

        if w <= self.max_width and h <= self.max_height:
            return None  # 无需缩放

        ratio = min(self.max_width / w, self.max_height / h)
        new_size = (int(w * ratio), int(h * ratio))
        img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        fmt = img.format or "PNG"
        img.save(buf, format=fmt)
        return buf.getvalue()


class ImageCompressProcessor(MediaProcessor):
    """图像压缩处理器。"""

    name = "image_compress"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.IMAGE]

    def __init__(self, quality: int = 85, output_format: str = "webp") -> None:
        self.quality = quality
        self.output_format = output_format

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.IMAGE

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """压缩图片。"""
        start = time.monotonic()
        try:
            from PIL import Image

            image_data = content.optimized_data or content.raw_data
            if image_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No image data",
                )

            loop = asyncio.get_event_loop()
            compressed = await loop.run_in_executor(
                None, self._compress, image_data
            )

            elapsed = (time.monotonic() - start) * 1000
            original_size = len(image_data)
            new_size = len(compressed)
            savings = max(0, (original_size - new_size) // 4)

            return ProcessorResult(
                success=True,
                processor_name=self.name,
                duration_ms=elapsed,
                output=compressed,
                token_savings=savings,
            )
        except ImportError:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="Pillow not installed",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _compress(self, image_data: bytes) -> bytes:
        """同步压缩。"""
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        if img.mode == "RGBA" and self.output_format.lower() in ("jpeg", "jpg"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        fmt = self.output_format.upper()
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=self.quality)
        elif fmt in ("JPEG", "JPG"):
            img.save(buf, format="JPEG", quality=self.quality, optimize=True)
        else:
            img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


class OCRExtractor(MediaProcessor):
    """OCR 文字提取处理器 — 支持 PaddleOCR 和 Tesseract 双后端。

    当 `ocr_backend` 配置为 "paddleocr" 时优先使用 PaddleOCR 引擎，
    PaddleOCR 未安装或初始化失败时自动回退到 Tesseract。
    """

    name = "ocr_extractor"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.IMAGE]

    # PaddleOCR 语言代码映射（常用语言 → PaddleOCR lang 参数）
    _PADDLE_LANG_MAP: dict = {
        "chi_sim": "ch",
        "chi_tra": "chinese_cht",
        "eng": "en",
        "jpn": "japan",
        "kor": "korean",
        "fra": "fr",
        "deu": "german",
        "rus": "ru",
        "spa": "es",
        "por": "pt",
        "ara": "ar",
        "hin": "hi",
        "ita": "it",
        "ch": "ch",
        "en": "en",
    }

    def __init__(
        self, backend: str = "tesseract", languages: Optional[List[str]] = None
    ) -> None:
        self.backend = backend
        self.languages = languages or ["eng"]
        self._paddleocr_engine: Optional[object] = None

        if backend == "paddleocr":
            self._init_paddleocr()

    def _init_paddleocr(self) -> None:
        """初始化 PaddleOCR 引擎。失败则回退到 Tesseract。"""
        try:
            from paddleocr import PaddleOCR

            lang = self._map_language_code(self.languages)
            self._paddleocr_engine = PaddleOCR(
                use_angle_cls=True,
                lang=lang,
                show_log=False,
            )
            logger.info("PaddleOCR 引擎初始化成功 (lang=%s)", lang)
        except ImportError:
            logger.warning("PaddleOCR 未安装，回退到 Tesseract")
            self.backend = "tesseract"
            self._paddleocr_engine = None
        except Exception as exc:
            logger.warning("PaddleOCR 初始化失败，回退到 Tesseract: %s", exc)
            self.backend = "tesseract"
            self._paddleocr_engine = None

    def _map_language_code(self, languages: List[str]) -> str:
        """将语言列表映射为 PaddleOCR 支持的 lang 参数。

        PaddleOCR 一次只支持单一语言参数，优先使用列表中第一个可映射的语言。
        """
        for lang in languages:
            mapped = self._PADDLE_LANG_MAP.get(lang.lower())
            if mapped:
                return mapped
        # 默认中文
        return "ch"

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.IMAGE

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """提取图片中的文字。"""
        start = time.monotonic()
        try:
            image_data = content.optimized_data or content.raw_data
            if image_data is None:
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=0,
                    error="No image data",
                )

            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._extract, image_data)

            elapsed = (time.monotonic() - start) * 1000
            if text and text.strip():
                # OCR 成功提取文字 → 可用文本替代图片，节约 token
                original_tokens = len(image_data) // 4  # 粗略估算图片 token
                text_tokens = len(text) // 4
                savings = max(0, original_tokens - text_tokens)
                return ProcessorResult(
                    success=True,
                    processor_name=self.name,
                    duration_ms=elapsed,
                    output=text.strip(),
                    token_savings=savings,
                )
            return ProcessorResult(
                success=True,
                processor_name=self.name,
                duration_ms=elapsed,
                output="",
            )
        except ImportError:
            elapsed = (time.monotonic() - start) * 1000
            logger.debug("OCR 后端不可用 (%s)", self.backend)
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=f"OCR backend '{self.backend}' not installed",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )

    def _extract(self, image_data: bytes) -> str:
        """根据 backend 选择 OCR 引擎执行提取。"""
        if self.backend == "paddleocr" and self._paddleocr_engine:
            return self._extract_paddleocr(image_data)
        return self._extract_tesseract(image_data)

    def _extract_paddleocr(self, image_data: bytes) -> str:
        """PaddleOCR 提取，按位置排序保留表格布局信息。"""
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        img_array = np.array(img)

        results = self._paddleocr_engine.ocr(img_array, cls=True)

        # 按 y 坐标排序，保留文档布局
        lines: List[tuple] = []
        if results:
            for line_result in results:
                if line_result:
                    for box, (text, confidence) in line_result:
                        # box[0][1] 是左上角 y 坐标，box[0][0] 是左上角 x 坐标
                        y_coord = box[0][1]
                        x_coord = box[0][0]
                        lines.append((y_coord, x_coord, text))

        # 按 y 坐标排序（行），同行内按 x 坐标排序（列）
        # 使用 y 坐标分组（容差范围内视为同一行）
        if not lines:
            return ""

        lines.sort(key=lambda x: (x[0], x[1]))

        # 简单行分组：y 坐标差值小于阈值的归为同一行
        grouped_lines: List[List[tuple]] = []
        current_group: List[tuple] = [lines[0]]
        y_tolerance = 15  # 像素容差

        for i in range(1, len(lines)):
            if abs(lines[i][0] - current_group[-1][0]) <= y_tolerance:
                current_group.append(lines[i])
            else:
                grouped_lines.append(current_group)
                current_group = [lines[i]]
        grouped_lines.append(current_group)

        # 同一行内按 x 坐标排序，用空格连接
        output_lines: List[str] = []
        for group in grouped_lines:
            group.sort(key=lambda x: x[1])
            line_text = "  ".join(text for _, _, text in group)
            output_lines.append(line_text)

        return "\n".join(output_lines)

    def _extract_tesseract(self, image_data: bytes) -> str:
        """Tesseract OCR 提取。"""
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        import pytesseract

        lang = "+".join(self.languages)
        return pytesseract.image_to_string(img, lang=lang)


class VisionCaptionProcessor(MediaProcessor):
    """Vision Caption 处理器 — 生成图片描述。

    通过 LiteLLM Bridge 调用配置的 Vision Model 生成图片文字描述。
    如果 LiteLLM 不可用或调用失败，则优雅降级（跳过 caption 步骤）。
    """

    name = "vision_caption"
    phase = ProcessorPhase.PRE_LLM
    supported_types = [MediaType.IMAGE]

    def __init__(self, model: str = "agnes-2.0-flash", max_tokens: int = 150, temperature: float = 0.3) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def supports(self, content: MediaContent) -> bool:
        return content.media_type == MediaType.IMAGE

    async def process(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> ProcessorResult:
        """生成图片描述（需要 Vision Model 支持）。"""
        start = time.monotonic()
        # 如果已有 extracted_text（OCR 结果），跳过 Caption
        if content.extracted_text:
            return ProcessorResult(
                success=True,
                processor_name=self.name,
                duration_ms=0,
                output=content.extracted_text,
            )

        # 尝试通过 LiteLLM Bridge 调用 Vision Model
        try:
            from aigateway_api.app_state import get_state
            litellm_bridge = getattr(get_state(), "litellm_bridge", None)
        except Exception:
            litellm_bridge = None

        if litellm_bridge is None:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="LiteLLM Bridge not available for captioning",
            )

        # 构建 vision 请求
        try:
            import base64
            image_data = content.optimized_data or content.raw_data
            if image_data is None:
                elapsed = (time.monotonic() - start) * 1000
                return ProcessorResult(
                    success=False,
                    processor_name=self.name,
                    duration_ms=elapsed,
                    error="No image data for captioning",
                )

            b64 = base64.b64encode(image_data).decode()
            mime = content.mime_type or "image/png"
            data_url = f"data:{mime};base64,{b64}"

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用一段简洁的中文描述这张图片的主要内容，不超过100字。"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]

            result = await litellm_bridge.completion(
                messages=messages,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=False,
            )

            data = result.get("data", {})
            choices = data.get("choices", [])
            if choices:
                caption = choices[0].get("message", {}).get("content", "")
                if caption:
                    elapsed = (time.monotonic() - start) * 1000
                    original_tokens = len(image_data) // 4
                    caption_tokens = len(caption) // 4
                    savings = max(0, original_tokens - caption_tokens)
                    return ProcessorResult(
                        success=True,
                        processor_name=self.name,
                        duration_ms=elapsed,
                        output=f"[图片描述]: {caption}",
                        token_savings=savings,
                    )

            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error="Vision model returned empty caption",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ProcessorResult(
                success=False,
                processor_name=self.name,
                duration_ms=elapsed,
                error=str(exc),
            )


# ------------------------------------------------------------------
# Image Pipeline
# ------------------------------------------------------------------


class ImagePipeline(MediaPipeline):
    """图像处理管线。

    处理器链:
    1. ImageResizeProcessor — 缩放到目标分辨率
    2. ImageCompressProcessor — 质量压缩
    3. OCRExtractor — 文字提取
    4. VisionCaptionProcessor — Caption 生成（降级时跳过）

    策略:
    - 纯文字图片 → OCR 提取文本替代原图
    - 纯视觉图片 → Caption 或保留原图
    - 混合图片 → OCR + Caption 组合
    """

    media_type = MediaType.IMAGE

    def __init__(self, config: Optional[ImagePipelineConfig] = None) -> None:
        cfg = config or ImagePipelineConfig()
        self.config = cfg
        self.resize_processor = ImageResizeProcessor(
            max_width=cfg.max_width,
            max_height=cfg.max_height,
        )
        self.compress_processor = ImageCompressProcessor(
            quality=cfg.quality,
            output_format=cfg.output_format,
        )
        self.ocr_extractor = OCRExtractor(
            backend=cfg.ocr_backend,
            languages=cfg.ocr_languages,
        )
        self.caption_processor = VisionCaptionProcessor(
            model=cfg.caption_model,
            max_tokens=cfg.caption_max_tokens,
            temperature=cfg.caption_temperature,
        )
        self.processors = [
            self.resize_processor,
            self.compress_processor,
            self.ocr_extractor,
            self.caption_processor,
        ]

    async def execute(
        self, content: MediaContent, ctx: "PipelineContext"
    ) -> MediaContent:
        """执行图像处理管线。"""
        # 检查文件大小限制
        if content.size_bytes > self.config.max_file_size_mb * 1024 * 1024:
            logger.warning(
                "图片文件超过大小限制: %d bytes > %d MB",
                content.size_bytes,
                self.config.max_file_size_mb,
            )
            return content

        # 下载图片（如果只有 URL）
        if content.raw_data is None and content.source_url:
            content.raw_data = await self._download(content.source_url)
            if content.raw_data:
                content.size_bytes = len(content.raw_data)

        if content.raw_data is None:
            return content

        # Step 1: Resize
        resized = await self.resize_processor.process(content, ctx)
        if resized.success and resized.output:
            content.optimized_data = resized.output

        # Step 2: Compress
        compressed = await self.compress_processor.process(content, ctx)
        if compressed.success and compressed.output:
            content.optimized_data = compressed.output
            content.token_savings += compressed.token_savings

        # Step 3: OCR
        ocr_result = await self.ocr_extractor.process(content, ctx)
        if ocr_result.success and ocr_result.output:
            content.extracted_text = ocr_result.output
            content.token_savings += ocr_result.token_savings

        # Step 4: Caption（如果 OCR 没有提取到文字）
        if not content.extracted_text:
            caption_result = await self.caption_processor.process(content, ctx)
            if caption_result.success and caption_result.output:
                content.extracted_text = caption_result.output

        return content

    async def _download(self, url: str) -> Optional[bytes]:
        """下载图片内容。"""
        try:
            import httpx

            headers = {
                "User-Agent": "AIGateway/1.0 (Media Optimization Layer)",
                "Accept": "image/*,*/*",
            }
            async with httpx.AsyncClient(timeout=self.config.download_timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                logger.warning("图片下载失败: HTTP %d from %s", resp.status_code, url)
                return None
        except Exception as exc:
            logger.warning("图片下载异常: %s", exc)
            return None
