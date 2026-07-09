"""
ContentTypeDetector — 内容类型检测器
====================================

从 OpenAI ContentPart 推断媒体类型，路由到正确的 Pipeline。
"""

from __future__ import annotations

import mimetypes
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .types import MediaContent, MediaType


class ContentTypeDetector:
    """内容类型检测器 — 从 OpenAI ContentPart 推断媒体类型。

    检测逻辑:
    1. type == "text" → MediaType.TEXT
    2. type == "image_url" → MediaType.IMAGE
    3. type == "input_audio" → MediaType.AUDIO
    4. type == "file" → 根据 URL 后缀判断
    5. URL 后缀匹配 → VIDEO/AUDIO/DOCUMENT
    6. MIME type 匹配 → 精确分类
    """

    MIME_MAP: Dict[str, MediaType] = {
        # Image
        "image/jpeg": MediaType.IMAGE,
        "image/png": MediaType.IMAGE,
        "image/gif": MediaType.IMAGE,
        "image/webp": MediaType.IMAGE,
        "image/svg+xml": MediaType.IMAGE,
        "image/bmp": MediaType.IMAGE,
        "image/tiff": MediaType.IMAGE,
        # Video
        "video/mp4": MediaType.VIDEO,
        "video/webm": MediaType.VIDEO,
        "video/avi": MediaType.VIDEO,
        "video/quicktime": MediaType.VIDEO,
        "video/x-msvideo": MediaType.VIDEO,
        # Audio
        "audio/mpeg": MediaType.AUDIO,
        "audio/wav": MediaType.AUDIO,
        "audio/ogg": MediaType.AUDIO,
        "audio/flac": MediaType.AUDIO,
        "audio/webm": MediaType.AUDIO,
        "audio/mp4": MediaType.AUDIO,
        "audio/x-wav": MediaType.AUDIO,
        # Document
        "application/pdf": MediaType.DOCUMENT,
        "application/msword": MediaType.DOCUMENT,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": MediaType.DOCUMENT,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": MediaType.DOCUMENT,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": MediaType.DOCUMENT,
        "text/csv": MediaType.DOCUMENT,
        "text/markdown": MediaType.DOCUMENT,
        "text/html": MediaType.DOCUMENT,
    }

    EXT_MAP: Dict[str, MediaType] = {
        # Video
        ".mp4": MediaType.VIDEO,
        ".webm": MediaType.VIDEO,
        ".avi": MediaType.VIDEO,
        ".mov": MediaType.VIDEO,
        ".mkv": MediaType.VIDEO,
        # Audio
        ".mp3": MediaType.AUDIO,
        ".wav": MediaType.AUDIO,
        ".ogg": MediaType.AUDIO,
        ".flac": MediaType.AUDIO,
        ".m4a": MediaType.AUDIO,
        ".opus": MediaType.AUDIO,
        # Document
        ".pdf": MediaType.DOCUMENT,
        ".docx": MediaType.DOCUMENT,
        ".xlsx": MediaType.DOCUMENT,
        ".pptx": MediaType.DOCUMENT,
        ".csv": MediaType.DOCUMENT,
        ".md": MediaType.DOCUMENT,
        ".html": MediaType.DOCUMENT,
        ".htm": MediaType.DOCUMENT,
    }

    def detect(self, content_part: Dict[str, Any]) -> MediaContent:
        """从 ContentPart 检测媒体类型并构建 MediaContent。"""
        part_type = content_part.get("type", "text")

        if part_type == "text":
            return MediaContent(media_type=MediaType.TEXT)

        if part_type == "image_url":
            url = content_part.get("image_url", {}).get("url", "")
            # 处理 base64 data URI
            if url.startswith("data:"):
                raw, mime = self._decode_data_uri(url)
                # 根据实际 MIME type 分类（data URI 可能是视频/音频/文档）
                media_type = MediaType.IMAGE
                if mime and mime in self.MIME_MAP:
                    media_type = self.MIME_MAP[mime]
                return MediaContent(
                    media_type=media_type,
                    raw_data=raw,
                    mime_type=mime,
                    size_bytes=len(raw) if raw else 0,
                )
            # URL 类型：根据扩展名判断
            media_type = self._detect_from_url(url) if url else MediaType.IMAGE
            return MediaContent(
                media_type=media_type,
                source_url=url,
                mime_type=self._guess_mime(url),
            )

        if part_type == "input_audio":
            audio_data = content_part.get("input_audio", {})
            raw = audio_data.get("data")
            if isinstance(raw, str):
                import base64

                try:
                    raw = base64.b64decode(raw)
                except Exception:
                    raw = raw.encode()
            return MediaContent(
                media_type=MediaType.AUDIO,
                raw_data=raw,
                mime_type=f"audio/{audio_data.get('format', 'wav')}",
                size_bytes=len(raw) if raw else 0,
            )

        if part_type == "file":
            file_data = content_part.get("file", {})
            url = file_data.get("url", "")
            media_type = self._detect_from_url(url)
            return MediaContent(
                media_type=media_type,
                source_url=url,
                mime_type=self._guess_mime(url),
            )

        # Generic URL-based detection
        url = content_part.get("url", "")
        if url:
            media_type = self._detect_from_url(url)
            return MediaContent(
                media_type=media_type,
                source_url=url,
                mime_type=self._guess_mime(url),
            )

        return MediaContent(media_type=MediaType.TEXT)

    def _detect_from_url(self, url: str) -> MediaType:
        """从 URL 后缀推断媒体类型。"""
        path = urlparse(url).path.lower()
        for ext, media_type in self.EXT_MAP.items():
            if path.endswith(ext):
                return media_type

        # 尝试 MIME 猜测
        mime = self._guess_mime(url)
        if mime and mime in self.MIME_MAP:
            return self.MIME_MAP[mime]

        return MediaType.IMAGE  # 默认假设为图片

    def _guess_mime(self, url: str) -> Optional[str]:
        """从 URL 猜测 MIME type。"""
        if not url:
            return None
        mime, _ = mimetypes.guess_type(url)
        return mime

    @staticmethod
    def _decode_data_uri(uri: str) -> tuple:
        """解码 base64 data URI。

        Returns:
            (raw_bytes, mime_type)，解码失败返回 (None, None)。
        """
        import base64

        try:
            header, _, data = uri.partition(",")
            # header 形如 data:image/png;base64
            mime = None
            if header.startswith("data:"):
                mime_part = header[len("data:"):].split(";", 1)[0]
                mime = mime_part or None
            if ";base64" in header:
                raw = base64.b64decode(data)
            else:
                from urllib.parse import unquote_to_bytes

                raw = unquote_to_bytes(data)
            return raw, mime
        except Exception:
            return None, None
