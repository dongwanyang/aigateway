"""
Media Optimization Layer 单元测试
===================================

测试覆盖:
- ContentTypeDetector: 所有 MIME type 和 URL extension 映射
- MediaCacheManager: 序列化/反序列化往返
- MediaOptimizationLayer: 消息处理流程
- PipelineContext V2: 命名空间隔离
- ImagePipeline: 处理流程（带 mock）
- 正确性属性验证
"""

import asyncio
import json
import sys
import os
import pytest

# 确保导入路径正确
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))


from aigateway_core.prefix.media.types import MediaType, ProcessorPhase, MediaContent, ProcessorResult
from aigateway_core.prefix.media.detector import ContentTypeDetector
from aigateway_core.prefix.media.cache import MediaCacheManager
from aigateway_core.prefix.media.config import (
    ImagePipelineConfig,
    AudioPipelineConfig,
    VideoPipelineConfig,
    DocumentPipelineConfig,
    MediaOptimizationConfig,
    GenerationConfig,
)
from aigateway_core.prefix.media.mol import MediaOptimizationLayer
from aigateway_core.prefix.media.generation import PromptEnhancer, GenerationPipeline
from aigateway_core.dispatch.context import PipelineContext


# ==================================================================
# ContentTypeDetector Tests
# ==================================================================


class TestContentTypeDetector:
    """ContentTypeDetector 测试。"""

    def setup_method(self):
        self.detector = ContentTypeDetector()

    def test_detect_text(self):
        """纯文本 ContentPart → MediaType.TEXT"""
        part = {"type": "text", "text": "Hello world"}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.TEXT

    def test_detect_image_url(self):
        """image_url ContentPart → MediaType.IMAGE"""
        part = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/photo.jpg"},
        }
        result = self.detector.detect(part)
        assert result.media_type == MediaType.IMAGE
        assert result.source_url == "https://example.com/photo.jpg"

    def test_detect_image_png(self):
        """PNG 图片 URL"""
        part = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png"},
        }
        result = self.detector.detect(part)
        assert result.media_type == MediaType.IMAGE
        assert result.mime_type == "image/png"

    def test_detect_input_audio(self):
        """input_audio ContentPart → MediaType.AUDIO"""
        part = {
            "type": "input_audio",
            "input_audio": {"data": "SGVsbG8=", "format": "wav"},
        }
        result = self.detector.detect(part)
        assert result.media_type == MediaType.AUDIO
        assert result.raw_data is not None

    def test_detect_video_mp4_by_extension(self):
        """MP4 视频 URL → MediaType.VIDEO"""
        part = {"type": "file", "file": {"url": "https://example.com/video.mp4"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.VIDEO

    def test_detect_video_webm(self):
        """WebM 视频"""
        part = {"type": "file", "file": {"url": "https://example.com/clip.webm"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.VIDEO

    def test_detect_audio_mp3(self):
        """MP3 音频"""
        part = {"type": "file", "file": {"url": "https://example.com/song.mp3"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.AUDIO

    def test_detect_audio_wav(self):
        """WAV 音频"""
        part = {"type": "file", "file": {"url": "https://example.com/recording.wav"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.AUDIO

    def test_detect_document_pdf(self):
        """PDF 文档"""
        part = {"type": "file", "file": {"url": "https://example.com/doc.pdf"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.DOCUMENT

    def test_detect_document_docx(self):
        """DOCX 文档"""
        part = {"type": "file", "file": {"url": "https://example.com/report.docx"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.DOCUMENT

    def test_detect_document_csv(self):
        """CSV 文件"""
        part = {"type": "file", "file": {"url": "https://example.com/data.csv"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.DOCUMENT

    def test_detect_unknown_defaults_to_image(self):
        """未知 URL 类型默认为 IMAGE"""
        part = {"type": "file", "file": {"url": "https://example.com/unknown"}}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.IMAGE

    def test_detect_empty_part(self):
        """空 part 默认为 TEXT"""
        part = {}
        result = self.detector.detect(part)
        assert result.media_type == MediaType.TEXT


# ==================================================================
# MediaCacheManager Tests
# ==================================================================


class TestMediaCacheManager:
    """MediaCacheManager 测试。"""

    def test_compute_hash_deterministic(self):
        """相同输入产生相同 hash（幂等性）"""
        h1 = MediaCacheManager.compute_hash(
            "https://example.com/img.jpg", "image/jpeg", "config1"
        )
        h2 = MediaCacheManager.compute_hash(
            "https://example.com/img.jpg", "image/jpeg", "config1"
        )
        assert h1 == h2
        assert len(h1) == 32

    def test_compute_hash_different_inputs(self):
        """不同输入产生不同 hash"""
        h1 = MediaCacheManager.compute_hash(
            "https://example.com/img1.jpg", "image/jpeg", "config1"
        )
        h2 = MediaCacheManager.compute_hash(
            "https://example.com/img2.jpg", "image/jpeg", "config1"
        )
        assert h1 != h2

    def test_compute_hash_different_config(self):
        """相同 URL 不同配置产生不同 hash"""
        h1 = MediaCacheManager.compute_hash(
            "https://example.com/img.jpg", "image/jpeg", "config_a"
        )
        h2 = MediaCacheManager.compute_hash(
            "https://example.com/img.jpg", "image/jpeg", "config_b"
        )
        assert h1 != h2

    def test_serialize_deserialize_roundtrip(self):
        """序列化/反序列化往返一致性"""
        # 创建一个临时 cache manager（不需要 Redis）
        class FakeRedis:
            redis = None
        mgr = MediaCacheManager(redis_client=FakeRedis())

        content = MediaContent(
            media_type=MediaType.IMAGE,
            extracted_text="Hello from OCR",
            token_savings=500,
            metadata={"width": 1920, "height": 1080},
            source_url="https://example.com/test.jpg",
            mime_type="image/jpeg",
            size_bytes=12345,
        )

        serialized = mgr._serialize(content)
        assert isinstance(serialized, bytes)

        deserialized = mgr._deserialize(serialized)
        assert deserialized.media_type == MediaType.IMAGE
        assert deserialized.extracted_text == "Hello from OCR"
        assert deserialized.token_savings == 500
        assert deserialized.metadata == {"width": 1920, "height": 1080}
        assert deserialized.source_url == "https://example.com/test.jpg"
        assert deserialized.mime_type == "image/jpeg"
        assert deserialized.size_bytes == 12345

    def test_compute_config_hash(self):
        """配置 hash 确定性"""
        config = {"quality": 85, "max_width": 1920}
        h1 = MediaCacheManager.compute_config_hash(config)
        h2 = MediaCacheManager.compute_config_hash(config)
        assert h1 == h2
        assert len(h1) == 16


# ==================================================================
# PipelineContext V2 Tests
# ==================================================================


class TestPipelineContextV2:
    """PipelineContext V2 命名空间测试。"""

    def test_media_optimization_namespace(self):
        """media_optimization 命名空间自动创建"""
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")
        ns = ctx.media_optimization
        assert isinstance(ns, dict)
        assert "detected_types" in ns
        assert "total_savings" in ns

    def test_generation_pipeline_namespace(self):
        """generation_pipeline 命名空间自动创建"""
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")
        ns = ctx.generation_pipeline
        assert isinstance(ns, dict)
        assert ns["prompt_enhanced"] is False
        assert ns["enhancement_level"] == "off"

    def test_namespace_isolation(self):
        """各命名空间互不影响"""
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")
        ctx.media_optimization["total_savings"] = 1000
        ctx.generation_pipeline["selected_model"] = "gpt-4o"

        assert ctx.media_optimization["total_savings"] == 1000
        assert ctx.generation_pipeline["selected_model"] == "gpt-4o"
        # 其他命名空间不受影响
        assert ctx.prompt_cache.get("cache_hit") is None

    def test_is_multimodal_default(self):
        """is_multimodal 默认为 False"""
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")
        assert ctx.is_multimodal is False

    def test_total_token_savings_default(self):
        """total_token_savings 默认为 0"""
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")
        assert ctx.total_token_savings == 0


# ==================================================================
# MediaOptimizationLayer Tests
# ==================================================================


class TestMediaOptimizationLayer:
    """MOL 消息处理测试。"""

    def _make_mol(self):
        """创建一个带 mock pipeline 的 MOL。"""
        from aigateway_core.prefix.media.base import MediaPipeline
        from aigateway_core.prefix.media.types import MediaContent, MediaType

        class MockImagePipeline(MediaPipeline):
            media_type = MediaType.IMAGE
            processors = []

            async def execute(self, content, ctx):
                content.extracted_text = "[OCR: mock text from image]"
                content.token_savings = 100
                return content

        class MockAudioPipeline(MediaPipeline):
            media_type = MediaType.AUDIO
            processors = []

            async def execute(self, content, ctx):
                content.extracted_text = "[转录: mock audio transcript]"
                content.token_savings = 200
                return content

        pipelines = {
            MediaType.IMAGE: MockImagePipeline(),
            MediaType.AUDIO: MockAudioPipeline(),
        }
        return MediaOptimizationLayer(pipelines=pipelines)

    @pytest.mark.asyncio
    async def test_text_message_passthrough(self):
        """纯文本消息不变 (Property 5)"""
        mol = self._make_mol()
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        messages = [{"role": "user", "content": "Hello world"}]
        result = await mol.process_messages(messages, ctx)

        assert result == messages

    @pytest.mark.asyncio
    async def test_multimodal_image_processing(self):
        """图片消息被 MOL 处理"""
        mol = self._make_mol()
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What's in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/test.png"},
                    },
                ],
            }
        ]
        result = await mol.process_messages(messages, ctx)

        # 图片应被替换为 text part
        content_parts = result[0]["content"]
        assert len(content_parts) == 2
        assert content_parts[0] == {"type": "text", "text": "What's in this image?"}
        assert content_parts[1]["type"] == "text"
        assert "mock text from image" in content_parts[1]["text"]

    @pytest.mark.asyncio
    async def test_multimodal_audio_processing(self):
        """音频消息被 MOL 处理"""
        mol = self._make_mol()
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Summarize this audio"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "SGVsbG8=", "format": "wav"},
                    },
                ],
            }
        ]
        result = await mol.process_messages(messages, ctx)

        content_parts = result[0]["content"]
        assert len(content_parts) == 2
        assert content_parts[1]["type"] == "text"
        assert "mock audio transcript" in content_parts[1]["text"]

    @pytest.mark.asyncio
    async def test_token_savings_non_negative(self):
        """Property 2: token_savings >= 0"""
        mol = self._make_mol()
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Test"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/img.jpg"},
                    },
                ],
            }
        ]
        await mol.process_messages(messages, ctx)

        mol_ns = ctx.extra.get("media_optimization", {})
        assert mol_ns.get("total_savings", 0) >= 0

    @pytest.mark.asyncio
    async def test_unsupported_type_passthrough(self):
        """不支持的媒体类型原样透传"""
        mol = self._make_mol()
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        # 视频 URL 但 MOL 没有注册 Video Pipeline
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Watch this"},
                    {"type": "file", "file": {"url": "https://example.com/video.mp4"}},
                ],
            }
        ]
        result = await mol.process_messages(messages, ctx)

        # 视频 part 应被原样保留（因为 MOL 只有 IMAGE 和 AUDIO pipeline）
        content_parts = result[0]["content"]
        assert content_parts[1] == {"type": "file", "file": {"url": "https://example.com/video.mp4"}}


# ==================================================================
# PromptEnhancer Tests
# ==================================================================


class TestPromptEnhancer:
    """Prompt Enhancement 测试。"""

    @pytest.mark.asyncio
    async def test_off_level_passthrough(self):
        """off 级别不修改请求"""
        enhancer = PromptEnhancer(level="off")
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        request = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "gpt-4o",
        }
        result = await enhancer.enhance(request, ctx)
        assert result == request

    @pytest.mark.asyncio
    async def test_light_adds_system_prompt(self):
        """light 级别添加 system prompt"""
        enhancer = PromptEnhancer(level="light")
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        request = {
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = await enhancer.enhance(request, ctx)

        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_light_skips_if_system_exists(self):
        """light 级别: 已有 system prompt 时不重复添加"""
        enhancer = PromptEnhancer(level="light")
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        request = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
        }
        result = await enhancer.enhance(request, ctx)
        assert len(result["messages"]) == 2

    @pytest.mark.asyncio
    async def test_aggressive_adds_cot(self):
        """aggressive 级别注入 CoT"""
        enhancer = PromptEnhancer(level="aggressive")
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        request = {
            "messages": [{"role": "user", "content": "Solve this problem"}],
        }
        result = await enhancer.enhance(request, ctx)

        user_msg = result["messages"][-1]
        assert "step by step" in user_msg["content"]


# ==================================================================
# Configuration Tests
# ==================================================================


class TestMediaConfiguration:
    """配置模型测试。"""

    def test_default_image_config(self):
        """ImagePipelineConfig 默认值"""
        cfg = ImagePipelineConfig()
        assert cfg.max_width == 1920
        assert cfg.max_height == 1080
        assert cfg.quality == 85
        assert cfg.output_format == "webp"

    def test_default_audio_config(self):
        """AudioPipelineConfig 默认值"""
        cfg = AudioPipelineConfig()
        assert cfg.max_duration_sec == 600
        assert cfg.whisper_model == "faster-whisper"

    def test_default_video_config(self):
        """VideoPipelineConfig 默认值"""
        cfg = VideoPipelineConfig()
        assert cfg.max_frames == 10
        assert cfg.frame_interval_sec == 5.0

    def test_default_document_config(self):
        """DocumentPipelineConfig 默认值"""
        cfg = DocumentPipelineConfig()
        assert "pdf" in cfg.supported_formats
        assert cfg.chunk_size == 512

    def test_media_optimization_config(self):
        """MediaOptimizationConfig 总配置"""
        cfg = MediaOptimizationConfig()
        assert cfg.enabled is True
        assert cfg.media_cache_ttl == 604800
        assert cfg.max_concurrent_processors == 4
        assert isinstance(cfg.image, ImagePipelineConfig)

    def test_generation_config(self):
        """GenerationConfig 默认值"""
        cfg = GenerationConfig()
        assert cfg.enhancement_level == "off"
        assert cfg.vision_model == "gpt-4o"


# ==================================================================
# MediaContent Correctness Properties
# ==================================================================


class TestCorrectnessProperties:
    """设计文档中的正确性属性验证。"""

    @pytest.mark.asyncio
    async def test_property_1_openai_format_preserved(self):
        """P1: 处理后的消息仍符合 OpenAI ContentPart 格式"""
        from aigateway_core.prefix.media.base import MediaPipeline
        from aigateway_core.prefix.media.types import MediaContent, MediaType

        class MockPipeline(MediaPipeline):
            media_type = MediaType.IMAGE
            processors = []

            async def execute(self, content, ctx):
                content.extracted_text = "extracted text"
                return content

        mol = MediaOptimizationLayer(pipelines={MediaType.IMAGE: MockPipeline()})
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
                ],
            }
        ]
        result = await mol.process_messages(messages, ctx)

        # 验证每个 part 都有 type 字段
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    assert "type" in part
                    if part["type"] == "text":
                        assert "text" in part
                    elif part["type"] == "image_url":
                        assert "image_url" in part

    def test_property_2_token_savings_non_negative(self):
        """P2: token_savings >= 0"""
        content = MediaContent(media_type=MediaType.IMAGE)
        assert content.token_savings >= 0

        content.token_savings = 100
        assert content.token_savings >= 0

    def test_property_3_cache_key_deterministic(self):
        """P3: 相同输入的 cache_key 是确定性的"""
        key1 = MediaCacheManager.compute_hash("url1", "image/jpeg", "cfg1")
        key2 = MediaCacheManager.compute_hash("url1", "image/jpeg", "cfg1")
        assert key1 == key2

    @pytest.mark.asyncio
    async def test_property_5_text_message_invariant(self):
        """P5: 纯文本消息处理后不变"""
        from aigateway_core.prefix.media.base import MediaPipeline
        from aigateway_core.prefix.media.types import MediaType

        mol = MediaOptimizationLayer(pipelines={})
        ctx = PipelineContext(request={"messages": []}, trace_id="test-trace")

        original = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = await mol.process_messages(original, ctx)

        assert result == original


# ==================================================================
# Plugin Integration Test
# ==================================================================


class TestMediaOptimizationPlugin:
    """MediaOptimizationPlugin 集成测试。"""

    @pytest.mark.asyncio
    async def test_plugin_skips_text_only(self):
        """纯文本请求 → 插件不处理"""
        from aigateway_core.prefix.media.plugin import MediaOptimizationPlugin

        plugin = MediaOptimizationPlugin(config={"enabled": True})
        ctx = PipelineContext(
            trace_id="test-trace",
            request={
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "gpt-4o",
            }
        )

        result = await plugin.execute(ctx)
        # 纯文本，不应标记为多模态
        assert result.is_multimodal is False

    @pytest.mark.asyncio
    async def test_plugin_disabled(self):
        """禁用时直接返回"""
        from aigateway_core.prefix.media.plugin import MediaOptimizationPlugin

        plugin = MediaOptimizationPlugin(config={"enabled": False})
        ctx = PipelineContext(
            trace_id="test-trace",
            request={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Test"},
                            {"type": "image_url", "image_url": {"url": "http://x.com/a.jpg"}},
                        ],
                    }
                ],
            }
        )

        result = await plugin.execute(ctx)
        assert result.is_multimodal is False
