"""共享 pytest fixtures + e2e 前置健康检查.

原有 _reset_trace_collector 保留(用于单元测试的 ContextVar 隔离)。
新增 e2e 层的全局常量、健康检查、以及测试数据隔离前缀。
"""
import os
import tempfile

import httpx
import pytest

# ---- 全局常量(Phase 1 各窗口从这里 import) ----
BASE = "http://localhost:8000"
UI_BASE = "http://localhost:3000"
REDIS_URL = "redis://localhost:6379/0"
QDRANT_URL = "http://localhost:6333"
PROM_URL = "http://localhost:9090"
GRAFANA_URL = "http://localhost:3001"
# 宿主 config.yaml —— 必须指向 gateway 容器实际 bind-mount 的文件。
# worktree 下的 config.yaml 才是被 mount 进 /app/config.yaml 的那份,
# 不是主 checkout 的 config.yaml（路径随项目目录重命名变化，故相对解析）。
HOST_CONFIG_YAML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
AGNES_TEXT_MODEL = "agnes-2.0-flash"
AGNES_IMAGE_MODEL = "agnes-image-2.1-flash"
AGNES_VIDEO_MODEL = "agnes-video-v2.0"

ADMIN_KEY = os.environ.get("AI_GATEWAY_ADMIN_KEY")


def _explicit_e2e_ui(args) -> bool:
    """调用方是否显式指向 tests/e2e 或 tests/ui(而非泛指 tests/)。"""
    for a in args or []:
        s = str(a).rstrip("/")
        # e2e/ui paths trigger health-check gate
        if s.endswith(("tests/e2e", "tests/ui")) or "tests/e2e/" in str(a) or "tests/ui/" in str(a):
            return True
    return False


def _ensure_local_auth_db_path() -> None:
    """本地单测(非 Docker)默认 auth DB 路径为 /app/data/auth.db,该目录在宿主机
    上不存在或不可写 → SQLiteStore 初始化 PermissionError → create_app lifespan 失败。

    未显式设置 AI_GATEWAY_AUTH_DB_PATH 时,指向系统临时目录,使 TestClient 能正常启动。
    真实部署/容器内 /app/data 存在,不受影响。
    """
    if os.environ.get("AI_GATEWAY_AUTH_DB_PATH"):
        return
    os.environ["AI_GATEWAY_AUTH_DB_PATH"] = os.path.join(
        tempfile.gettempdir(), "aigateway_test_auth.db"
    )


def pytest_configure(config):
    """e2e 前置检查:环境变量 + gateway 健康。

    仅当调用方显式指向 tests/e2e 或 tests/ui 时才 gate(需要真实 gateway + admin key)。
    跑 `pytest tests/` 全量时不预先探测服务；CI 应按层分别执行，
    e2e/ui 层会在被显式选择时强制检查真实依赖。
    """
    if not _explicit_e2e_ui(config.args):
        _ensure_local_auth_db_path()
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


def pytest_collection_modifyitems(config, items):
    """Reject source-level skip/xfail markers instead of silently going green."""
    forbidden = {"skip", "skipif", "xfail"}
    violations = [
        f"{item.nodeid}: {name}"
        for item in items
        for name in forbidden
        if item.get_closest_marker(name) is not None
    ]
    if violations:
        pytest.exit(
            "禁止 skip/skipif/xfail；测试必须执行或明确失败：\n"
            + "\n".join(violations),
            returncode=2,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Turn dynamic skips into failures.

    A missing service or malformed response is evidence that the tested path is
    not healthy.  Reporting it as skipped made CI green while production
    operations were broken.
    """
    outcome = yield
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid}: runtime skip is forbidden; "
            "assert the precondition or fail with the real error"
        )



@pytest.fixture(autouse=True)
def _reset_trace_collector():
    """每个测试前重置 TraceCollector ContextVar,防止跨用例泄漏.

    只在 aigateway_core 可导入的环境下生效(单元测试直接 import 该包;
    e2e 测试通过 HTTP 调 gateway,不需要该 fixture 但保持全局 autouse 无副作用)。
    """
    tc = None
    try:
        from aigateway_core.shared.trace_event import TraceCollector
        tc = TraceCollector
    except ImportError:
        # Core not available — skip isolation for this test
        yield
        return
    tc._current.set(None)
    yield
    try:
        tc._current.set(None)
    except Exception:
        # Teardown: best-effort cleanup
        pass


# ---- 让 tests/fixtures/*.py 里的 fixture 被 pytest 全局识别 ----
pytest_plugins = [
    "tests.fixtures.data",
    "tests.fixtures.clients",
    "tests.fixtures.prom",
    "tests.fixtures.config",
    "tests.fixtures.trace",
]
