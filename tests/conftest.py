"""共享 pytest fixtures + e2e 前置健康检查.

原有 _reset_trace_collector 保留(用于单元测试的 ContextVar 隔离)。
新增 e2e 层的全局常量、健康检查、以及测试数据隔离前缀。
"""
import os
import sys
import pytest
import httpx

# ---- 全局常量(Phase 1 各窗口从这里 import) ----
BASE = "http://localhost:8000"
UI_BASE = "http://localhost:3000"
REDIS_URL = "redis://localhost:6379/0"
QDRANT_URL = "http://localhost:6333"
PROM_URL = "http://localhost:9090"
GRAFANA_URL = "http://localhost:3001"
HOST_CONFIG_YAML = "/home/ubuntu/gateway2/config.yaml"
AGNES_TEXT_MODEL = "agnes-2.0-flash"
AGNES_IMAGE_MODEL = "agnes-image-2.1-flash"
AGNES_VIDEO_MODEL = "agnes-video-v2.0"

ADMIN_KEY = os.environ.get("AI_GATEWAY_ADMIN_KEY")


def pytest_configure(config):
    """e2e 前置检查:环境变量 + gateway 健康。"""
    # 单元测试子集(不含 tests/e2e 或 tests/ui)不需要 gateway,跳过
    invoked_paths = " ".join(config.args or [])
    if "tests/e2e" not in invoked_paths and "tests/ui" not in invoked_paths and invoked_paths.strip() != "tests":
        return

    if not ADMIN_KEY:
        pytest.exit(
            "AI_GATEWAY_ADMIN_KEY env var not set. Run: "
            "export AI_GATEWAY_ADMIN_KEY=gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o",
            returncode=2,
        )
    # /health 走 dispatcher 的完整前置链,这个环境实测约 7-8s,给 15s 余量
    try:
        r = httpx.get(f"{BASE}/health", timeout=15)
    except Exception as exc:
        pytest.exit(f"Gateway {BASE}/health unreachable: {exc}", returncode=2)
    if r.status_code != 200:
        pytest.exit(
            f"Gateway {BASE}/health returned {r.status_code}; "
            f"start with: docker compose up -d",
            returncode=2,
        )


@pytest.fixture(autouse=True)
def _reset_trace_collector():
    """每个测试前重置 TraceCollector ContextVar,防止跨用例泄漏.

    只在 aigateway_core 可导入的环境下生效(单元测试直接 import 该包;
    e2e 测试通过 HTTP 调 gateway,不需要该 fixture 但保持全局 autouse 无副作用)。
    """
    try:
        from aigateway_core.trace_event import TraceCollector
        TraceCollector._current.set(None)
    except ImportError:
        pass
    yield
    try:
        from aigateway_core.trace_event import TraceCollector
        TraceCollector._current.set(None)
    except ImportError:
        pass


# ---- 让 tests/fixtures/*.py 里的 fixture 被 pytest 全局识别 ----
pytest_plugins = [
    "tests.fixtures.data",
    "tests.fixtures.clients",
    "tests.fixtures.prom",
    "tests.fixtures.config",
]
