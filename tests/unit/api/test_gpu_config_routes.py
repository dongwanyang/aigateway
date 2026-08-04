from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from aigateway_api.auth_middleware import authenticate_admin
from aigateway_api.config_security import config_revision
from aigateway_api.gpu_routes import router
from aigateway_core.shared.config import ConfigManager
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[3]


def _app(tmp_path: Path, *, authenticated: bool = True) -> tuple[FastAPI, Path]:
    config_path = tmp_path / "config.yaml"
    shutil.copy2(REPO_ROOT / "config.yaml", config_path)
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.state.config_manager = ConfigManager(str(config_path))
    if authenticated:
        async def _authenticated_admin() -> dict[str, object]:
            return {
                "auth_type": "browser_session",
                "scopes": ["admin"],
            }

        app.dependency_overrides[authenticate_admin] = _authenticated_admin
    return app, config_path


@pytest.mark.asyncio
async def test_gpu_config_requires_admin_session(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, authenticated=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put("/admin/gpu/config", json={"policy": "manual"})
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_gpu_config_is_transactional_and_classifies_restart_fields(
    tmp_path: Path,
) -> None:
    app, config_path = _app(tmp_path)
    revision = config_revision(str(config_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/gpu/config",
            headers={"If-Match": f'"{revision}"'},
            json={
                "gateway_fallback": "wait",
                "generation_wait_timeout_seconds": 42,
                "gateway_devices": ["GPU-test"],
                "comfyui_dynamic_vram_enabled": True,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["applied_fields"] == [
        "gateway_fallback",
        "generation_wait_timeout_seconds",
    ]
    assert body["data"]["restart_required_fields"] == [
        "comfyui_dynamic_vram_enabled",
        "gateway_devices",
    ]
    assert body["data"]["restart_required"] is True
    assert body["revision"] != revision


@pytest.mark.asyncio
async def test_gpu_config_preserves_host_generated_topology(tmp_path: Path) -> None:
    app, config_path = _app(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["gpu_scheduler"].update(
        {
            "inventory_source": "host_generated",
            "devices": [
                {
                    "index": 0,
                    "uuid": "GPU-test",
                    "name": "Test GPU",
                    "total_memory_gb": 16,
                    "free_memory_gb": 16,
                }
            ],
            "workers": [
                {
                    "worker_id": "comfyui-gpu-0",
                    "device_uuid": "GPU-test",
                    "server_url": "http://comfyui:8188",
                    "capabilities": ["image"],
                }
            ],
        }
    )
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    app.state.config_manager = ConfigManager(str(config_path))
    revision = config_revision(str(config_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/gpu/config",
            headers={"If-Match": f'"{revision}"'},
            json={"generation_wait_timeout_seconds": 90},
        )

    assert response.status_code == 200, response.text
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    scheduler = persisted["gpu_scheduler"]
    assert scheduler["inventory_source"] == "host_generated"
    assert scheduler["devices"][0]["uuid"] == "GPU-test"
    assert scheduler["workers"][0]["device_uuid"] == "GPU-test"


@pytest.mark.asyncio
async def test_gpu_config_rejects_invalid_values_and_revision_conflicts(
    tmp_path: Path,
) -> None:
    app, config_path = _app(tmp_path)
    revision = config_revision(str(config_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invalid = await client.put(
            "/admin/gpu/config",
            headers={"If-Match": f'"{revision}"'},
            json={"lease_ttl_seconds": 5, "lease_heartbeat_seconds": 5},
        )
        assert invalid.status_code == 422

        first = await client.put(
            "/admin/gpu/config",
            headers={"If-Match": f'"{revision}"'},
            json={"oom_quarantine_seconds": 10},
        )
        assert first.status_code == 200
        conflict = await client.put(
            "/admin/gpu/config",
            headers={"If-Match": f'"{revision}"'},
            json={"oom_quarantine_seconds": 20},
        )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"]["code"] == "config_version_conflict"
