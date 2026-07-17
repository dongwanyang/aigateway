"""
End-to-End 多模态 Gateway 测试
================================

使用真实 FastAPI 应用 + TestClient 验证多模态请求端到端流程：
- 应用启动（lifespan）
- 鉴权（override）
- Media Optimization Layer 实际运行
- LLM Bridge 调用（mock）
- 响应返回 + _meta.media_optimization

这是真正的端到端测试，验证 MOL 确实被接入请求路径。
"""

import base64
import io
import os
import sys as _sys

import pytest

# Save original sys.path before importing aigateway modules
_ORIGINAL_SYS_PATH = _sys.path.copy()

# Temporarily add paths for imports used in this test file
# 文件位于 tests/e2e/,需上溯两级到 repo root。
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "aigateway-core", "src"))
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "aigateway-api", "src"))

# 指向测试配置
os.environ["AI_GATEWAY_CONFIG_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "config.yaml"
)


def _restore_sys_path():
    """Restore sys.path to original state."""
    _sys.path[:] = _ORIGINAL_SYS_PATH


@pytest.fixture(autouse=True)
def _cleanup_sys_path():
    """Ensure sys.path is restored after each test to avoid polluting others."""
    yield
    _restore_sys_path()
    # Purge aigateway_api modules from sys.modules so subsequent tests get a
    # fresh import instead of a cached module loaded with the polluted sys.path.
    for key in list(_sys.modules):
        if key.startswith("aigateway_api.") or key == "aigateway_api":
            del _sys.modules[key]


def _make_png_bytes(width: int = 800, height: int = 600) -> bytes:
    """生成一张真实 PNG 图片。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        # Pillow 未安装时生成最小合法 PNG (1x1 red pixel)
        import struct, zlib
        sig = b'\x89PNG\r\n\x1a\n'
        # IHDR
        ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
        ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
        # IDAT
        raw = b''
        for _ in range(height):
            raw += b'\x00' + b'\xff\x00\x00' * width
        compressed = zlib.compress(raw)
        idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
        idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)
        # IEND
        iend_crc = zlib.crc32(b'IEND') & 0xffffffff
        iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
        return sig + ihdr + idat + iend
    img = Image.new("RGB", (width, height), color=(73, 109, 137))
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 50, width - 50, height - 50], fill=(200, 200, 200))
    draw.text((100, 100), "HELLO GATEWAY TEST", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _data_uri_png() -> str:
    raw = _make_png_bytes()
    b64 = base64.b64encode(raw).decode()
    return f"data:image/png;base64,{b64}"


class _MockLiteLLMBridge:
    """Mock LLM Bridge — 记录收到的 messages 以便断言。"""

    def __init__(self):
        self.last_messages = None
        self.last_model = None

    async def completion(self, messages, model, **kwargs):
        self.last_messages = messages
        self.last_model = model
        return {
            "data": {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Mock response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            "_meta": {"cache_hit": False, "cache_tier": None, "cost": 0.0},
        }


@pytest.fixture
def client_and_bridge():
    """构建 TestClient，override 鉴权，注入 mock bridge。"""
    from fastapi.testclient import TestClient
    import aigateway_api.main as main_module
    from aigateway_api.auth_middleware import authenticate

    # 处理器内部通过 `from aigateway_api.main import app` 使用模块级 app，
    # 因此测试必须使用同一个模块级 app 实例。
    app = main_module.app

    async def _fake_auth(request=None):
        return {"key_id": "test", "user_id": "tester", "status": "active"}

    app.dependency_overrides[authenticate] = _fake_auth

    mock_bridge = _MockLiteLLMBridge()

    with TestClient(app) as client:
        # lifespan 已执行，替换 litellm_bridge 为 mock
        app.state.litellm_bridge = mock_bridge
        # key_store 设为 None 时 auth override 已生效，配额检查会跳过
        app.state.key_store = None
        # 使用全新的 L1-only CacheManager 隔离缓存（避免 Redis L2 跨测试污染）
        from aigateway_core.prefix.cache.cache_manager import CacheManager
        app.state.cache_manager = CacheManager(l1_maxsize=100, l2_default_ttl=60)
        yield client, mock_bridge, app

    app.dependency_overrides.clear()


class TestE2EMultimodal:
    """端到端多模态测试。"""

    def test_health_endpoint(self, client_and_bridge):
        """基础：health 端点可用（验证 app 正常启动）。"""
        client, _, _ = client_and_bridge
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data

    def test_mol_initialized_in_app_state(self, client_and_bridge):
        """验证 MOL 已在 lifespan 中初始化并挂载到 app.state。"""
        _, _, app = client_and_bridge
        assert app.state.media_optimization_layer is not None, \
            "Media Optimization Layer 未初始化到 app.state！"

    def test_text_only_request_passthrough(self, client_and_bridge):
        """纯文本请求正常返回，media_optimization 为空。"""
        client, bridge, _ = client_and_bridge
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["data"]["choices"][0]["message"]["content"] == "Mock response"
        # 纯文本：LLM 收到原始 messages
        assert bridge.last_messages == [{"role": "user", "content": "Hello"}]

    def test_multimodal_image_end_to_end(self, client_and_bridge):
        """核心 E2E：图片请求经过 MOL 处理后到达 LLM。"""
        client, bridge, _ = client_and_bridge
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is in this image?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": _data_uri_png()},
                            },
                        ],
                    }
                ],
            },
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # 验证 _meta 包含 media_optimization 信息
        meta = data["_meta"].get("media_optimization", {})
        assert meta.get("is_multimodal") is True, f"meta={meta}"
        assert "image" in meta.get("detected_types", []), f"meta={meta}"

        # 验证 LLM 收到的 messages 中，图片 part 未被丢失
        # (OCR/caption 不可用时应保留原始图片 part — Property 1 & 4)
        llm_messages = bridge.last_messages
        assert llm_messages is not None
        user_content = llm_messages[0]["content"]
        assert isinstance(user_content, list)
        # 第一个 part 是文本
        assert user_content[0] == {"type": "text", "text": "What is in this image?"}
        # 第二个 part 存在（未被丢弃）
        assert len(user_content) == 2
        second = user_content[1]
        # 要么是 OCR 提取的文本，要么保留原始 image_url（降级）
        assert second.get("type") in ("text", "image_url")
        if second["type"] == "image_url":
            # 降级保留：图片内容未丢失
            assert second["image_url"]["url"].startswith("data:image/png")

    def test_token_savings_non_negative_e2e(self, client_and_bridge):
        """E2E: token_savings 永远 >= 0 (Property 2)。"""
        client, _, _ = client_and_bridge
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe"},
                            {"type": "image_url", "image_url": {"url": _data_uri_png()}},
                        ],
                    }
                ],
            },
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        meta = resp.json()["_meta"].get("media_optimization", {})
        assert meta.get("token_savings", 0) >= 0

    def test_text_message_invariant_e2e(self, client_and_bridge):
        """E2E Property 5: 纯文本消息处理后不变。"""
        client, bridge, _ = client_and_bridge
        original = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
        ]
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": original},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        assert bridge.last_messages == original

    def test_models_endpoint(self, client_and_bridge):
        """GET /v1/models 端点可用。"""
        client, bridge, app = client_and_bridge

        async def _list_models():
            return [{"id": "gpt-4o", "object": "model"}]

        bridge.list_models = _list_models
        resp = client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert isinstance(data["data"], list)
        assert len(data["data"]) > 0
