"""Admin debug endpoints —— GET /admin/config/debug 返回 5 维度配置."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from starlette.testclient import TestClient

from aigateway_core.shared.debug_config import DebugConfig


def test_get_debug_config_endpoint_returns_all_dims():
    """GET /admin/config/debug 应返回所有维度键（结构验证）。"""
    from aigateway_api.main import create_app

    app = create_app()
    # TestClient as context manager runs lifespan (mounts admin routes).
    with TestClient(app) as client:
        resp = client.get("/admin/config/debug",
                          headers={"Authorization": "Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"})
        assert resp.status_code == 200, f"Routes not mounted (status={resp.status_code}): {resp.text}"
        data = resp.json()["data"]
        # Verify all 5 dimensions are present (values come from config.yaml)
        for dim in ("frontend", "entry", "cache", "bridge", "plugins_enabled"):
            assert dim in data, f"Missing dimension: {dim}"
        # Verify per_plugin is present
        assert "per_plugin" in data
        # Values should be booleans
        for dim in ("frontend", "entry", "cache", "bridge", "plugins_enabled"):
            assert isinstance(data[dim], bool), f"{dim} should be bool, got {type(data[dim])}"
