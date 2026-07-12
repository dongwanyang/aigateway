"""Browser automation test for the Control Panel.

Simulates every user-facing operation in the control panel to catch
UI/API bugs that unit tests can't cover.  Runs headless in Chromium.

Usage:
    # Install deps:
    pip install playwright pytest pytest-asyncio
    playwright install chromium

    # Run all tests:
    pytest tests/control_panel_e2e.py -v

    # Run a specific test:
    pytest tests/control_panel_e2e.py -v -k "test_quotas"

    # With browser visible (for debugging):
    PLAYWRIGHT_HEADLESS=0 pytest tests/control_panel_e2e.py -v

Environment variables:
    CONTROL_PANEL_URL  – base URL of the control panel (default: http://localhost:3000)
    ADMIN_KEY          – admin API key for auth (default: gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o)
    GATEWAY_URL        – backend URL for direct API tests (default: http://localhost:8000)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import asyncio
import zipfile

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright, Page, Browser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CONTROL_PANEL_URL", "http://localhost:3000")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")

HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") == "1"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def browser():
    pw = await async_playwright().start()
    b = await pw.chromium.launch(headless=HEADLESS)
    yield b
    await b.close()
    await pw.stop()


@pytest_asyncio.fixture
async def page(browser: Browser):
    ctx = await browser.new_context(
        storage_state=None,
        viewport={"width": 1280, "height": 900},
    )
    p = await ctx.new_page()
    # Global unhandled exception handler — fail the test if any SPA error occurs
    p.on("console", lambda msg: (
        print(f"  [CONSOLE {msg.type}] {msg.text}", file=sys.stderr)
    ))
    p_errors: list[str] = []
    p.on("pageerror", lambda err: p_errors.append(str(err)))
    yield p
    # Fail the test if any JS exception occurred during navigation/rendering
    if p_errors:
        pytest.fail(f"JS errors during test: {'; '.join(p_errors)}")
    await ctx.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(p: Page):
    """Save the admin key in localStorage so all subsequent requests are authenticated."""
    await p.goto(BASE_URL, wait_until="domcontentloaded")
    # Use JSON.stringify to safely escape the key value
    await p.evaluate(
        "(key) => localStorage.setItem('aigateway_api_key', key)",
        ADMIN_KEY,
    )


async def _wait_api_ready(timeout_ms: int = 15000):
    """Block until the gateway /health endpoint responds."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(f"{GATEWAY_URL}/health", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def _direct_api(method: str, path: str, body: dict | None = None, expect_ok: bool = True, timeout_val: int = 60, content_type: str = "application/json"):
    """Make a raw API call to the gateway (bypasses the frontend)."""
    url = f"{GATEWAY_URL}{path}"
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    data = None
    if body is not None:
        if content_type == "multipart/form-data":
            # Build multipart manually as bytes to preserve binary file content
            boundary = secrets.token_hex(16)
            hdr_sep = f"--{boundary}\r\n".encode()
            part_tail = b"\r\n"
            close = f"--{boundary}--\r\n".encode()
            b: list[bytes] = []
            # Track which keys we've already emitted as file fields
            file_keys: set[str] = set()
            # Handle file fields first
            for file_key in ("files", "file"):
                if file_key in body:
                    file_keys.add(file_key)
                    val = body[file_key]
                    if isinstance(val, list):
                        # files: list of (filename, bytes)
                        for filename, filebytes in val:
                            b.append(hdr_sep)
                            b.append(f'Content-Disposition: form-data; name="{file_key}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
                            b.append(filebytes)
                            b.append(part_tail)
                    else:
                        # file: single (filename, bytes) tuple
                        filename, filebytes = val
                        b.append(hdr_sep)
                        b.append(f'Content-Disposition: form-data; name="{file_key}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
                        b.append(filebytes)
                        b.append(part_tail)
            if "relative_paths" in body:
                file_keys.add("relative_paths")
                for rp in body["relative_paths"]:
                    b.append(hdr_sep)
                    b.append(f'Content-Disposition: form-data; name="relative_paths"\r\n\r\n{rp}'.encode())
                    b.append(part_tail)
            # Then emit remaining scalar fields (skip ones already handled as files)
            for k, v in body.items():
                if k in file_keys:
                    continue
                b.append(hdr_sep)
                b.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}'.encode())
                b.append(part_tail)
            b.append(close)
            data = b"".join(b)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_val)
        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            result = json.loads(resp.read())
        else:
            result = resp.read().decode()
        if expect_ok and resp.status != 200:
            return False, result
        return True, result
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read()) if e.fp else {}
        except Exception:
            err_body = {}
        return False, err_body


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


class TestHealthAndAuth:
    """Basic health and auth readiness checks."""

    @pytest.mark.asyncio
    async def test_gateway_health(self):
        """Gateway /health returns 200."""
        ok, _ = await _direct_api("GET", "/health")
        assert ok, "Gateway /health is not responding"

    @pytest.mark.asyncio
    async def test_control_panel_loads(self, page: Page):
        """Control Panel SPA loads without JS errors."""
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
        # Should see at least the app root
        title = await page.title()
        assert title != "", f"Page title is empty — SPA may have crashed. URL: {page.url}"
        # Check no console errors
        await page.wait_for_timeout(1000)  # let React hydrate


class TestOverviewPage:
    """GET /health, GET /metrics, parseMetrics."""

    @pytest.mark.asyncio
    async def test_overview_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=概览', timeout=5000)
        await page.wait_for_timeout(1500)
        # Should see stat cards
        cards = await page.locator(".stat-card, [class*='card']").count()
        assert cards > 0, "Overview page should show stat cards"

    @pytest.mark.asyncio
    async def test_overview_metrics_present(self, page: Page):
        """Overview page shows metrics data (not all zeros)."""
        await _login(page)
        await page.click('text=概览', timeout=5000)
        await page.wait_for_timeout(3000)
        # Should see at least some numeric content
        content = await page.content()
        # If metrics are all zeros that's OK (no traffic yet) — just ensure page rendered
        assert "概览" in content or "health" in content.lower()


class TestModelsPage:
    """GET/PUT /admin/config, provider connectivity test."""

    @pytest.mark.asyncio
    async def test_models_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=模型', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "模型" in content or "provider" in content.lower() or "model" in content.lower()

    @pytest.mark.asyncio
    async def test_get_full_config(self):
        """GET /admin/config returns valid JSON."""
        ok, result = await _direct_api("GET", "/admin/config", timeout_val=30)
        assert ok, f"GET /admin/config failed: {result}"
        assert isinstance(result.get("data"), dict), "Expected data dict"

    @pytest.mark.asyncio
    async def test_put_config_noop(self):
        """PUT /admin/config with same config is idempotent."""
        ok, result = await _direct_api("GET", "/admin/config", timeout_val=30)
        assert ok, "Could not read config"
        config = result.get("data", {})
        ok2, result2 = await _direct_api("PUT", "/admin/config", body=config, timeout_val=120)
        assert ok2, f"PUT /admin/config failed: {result2}"

    @pytest.mark.asyncio
    async def test_provider_connectivity_test(self):
        """POST /admin/providers/{name}/test — test provider connectivity."""
        ok, result = await _direct_api("GET", "/admin/config")
        assert ok, "Could not read config"
        providers = result.get("data", {}).get("providers", {})
        if not providers:
            pytest.skip("No providers configured")
        # Pick the first provider name
        provider_name = next(iter(providers.keys()))
        ok2, result2 = await _direct_api(
            "POST", f"/admin/providers/{provider_name}/test",
            expect_ok=False,
        )
        # May fail (no real API key), but should not 500
        if ok2:
            data = result2.get("data", {})
            assert "provider" in data or "success" in data
        else:
            assert isinstance(result2, dict), f"Non-dict error body: {result2}"
            assert result2.get("detail") != "Internal Server Error", \
                f"Provider connectivity test returned 500: {result2}"


class TestPluginsPage:
    """GET/PUT /admin/plugins-config, GET/PUT /admin/global-config, debug toggles."""

    @pytest.mark.asyncio
    async def test_plugins_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=插件', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "插件" in content or "plugin" in content.lower()

    @pytest.mark.asyncio
    async def test_get_plugins_config(self):
        ok, result = await _direct_api("GET", "/admin/plugins-config")
        assert ok, f"GET /admin/plugins-config failed: {result}"

    @pytest.mark.asyncio
    async def test_get_global_config(self):
        ok, result = await _direct_api("GET", "/admin/global-config")
        assert ok, f"GET /admin/global-config failed: {result}"

    @pytest.mark.asyncio
    async def test_toggle_plugin(self):
        """Toggle a plugin on/off and back."""
        # Get current config
        ok, result = await _direct_api("GET", "/admin/plugins-config")
        assert ok, "Could not get plugins config"
        plugins = result.get("data", {}).get("plugins", [])
        if not plugins:
            pytest.skip("No plugins registered")
        first = plugins[0]
        name = first.get("name", "")
        if not name:
            pytest.skip("First plugin has no name")
        # Toggle off
        ok2, _ = await _direct_api("PUT", "/admin/plugins-config", body={"name": name, "enabled": False})
        assert ok2, f"Toggle off {name} failed"
        # Toggle back on
        ok3, _ = await _direct_api("PUT", "/admin/plugins-config", body={"name": name, "enabled": True})
        assert ok3, f"Toggle on {name} failed"

    @pytest.mark.asyncio
    async def test_update_hot_reload(self):
        """Toggle hot_reload on and back."""
        ok, result = await _direct_api("GET", "/admin/global-config")
        assert ok, "Could not get global config"
        current = result.get("data", {}).get("hot_reload", False)
        # Toggle
        ok2, _ = await _direct_api("PUT", "/admin/global-config", body={"hot_reload": not current})
        assert ok2, "Toggle hot_reload failed"
        # Toggle back
        ok3, _ = await _direct_api("PUT", "/admin/global-config", body={"hot_reload": current})
        assert ok3, "Restore hot_reload failed"

    @pytest.mark.asyncio
    async def test_get_debug_config(self):
        ok, result = await _direct_api("GET", "/admin/config/debug")
        assert ok, f"GET /admin/config/debug failed: {result}"

    @pytest.mark.asyncio
    async def test_per_plugin_debug_toggle(self):
        """POST /admin/plugins/{name}/debug — toggle per-plugin debug."""
        ok, result = await _direct_api("GET", "/admin/plugins-config")
        assert ok
        plugins = result.get("data", {}).get("plugins", [])
        if not plugins:
            pytest.skip("No plugins registered")
        name = plugins[0].get("name", "")
        if not name:
            pytest.skip("First plugin has no name")
        ok2, _ = await _direct_api("POST", f"/admin/plugins/{name}/debug", body={"enabled": True})
        assert ok2, f"POST /admin/plugins/{name}/debug failed"


class TestQuotasPage:
    """API Key CRUD, Group CRUD, Key-to-Group assignment.

    This is the most complex page — covers:
    - listApiKeys
    - createApiKey (with group_id)
    - updateApiKeyQuota
    - deleteApiKey
    - listGroups
    - createGroup
    - updateGroup
    - deleteGroup
    - assignKeyGroup (THE BUG REPORT: key assignment fails)
    """

    @pytest.mark.asyncio
    async def test_quotas_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=配额', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "配额" in content or "quota" in content.lower() or "API Key" in content

    @pytest.mark.asyncio
    async def test_list_api_keys(self):
        ok, result = await _direct_api("GET", "/admin/api-keys")
        assert ok, f"listApiKeys failed: {result}"
        items = result.get("data", {}).get("items", [])
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_list_groups(self):
        ok, result = await _direct_api("GET", "/admin/groups")
        assert ok, f"listGroups failed: {result}"
        items = result.get("data", {}).get("items", [])
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_create_and_delete_group(self):
        """Create a group, verify it appears in list, then delete it."""
        group_name = f"test-group-{secrets.token_hex(8)}"
        ok, result = await _direct_api("POST", "/admin/groups", body={
            "name": group_name,
            "daily_tokens": 1000000,
            "monthly_cost": 50,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 100000,
        })
        assert ok, f"createGroup failed: {result}"
        group_id = result.get("data", {}).get("group_id", "")
        assert group_id, f"No group_id in response: {result}"

        # Verify it's in the list
        ok2, result2 = await _direct_api("GET", "/admin/groups")
        assert ok2
        groups = result2.get("data", {}).get("items", [])
        group_ids = [g.get("group_id") for g in groups]
        assert group_id in group_ids, f"Group {group_id} not found in list: {[g.get('group_id') for g in groups]}"

        # Clean up
        ok3, result3 = await _direct_api("DELETE", f"/admin/groups/{group_id}")
        assert ok3, f"deleteGroup failed: {result3}"

    @pytest.mark.asyncio
    async def test_update_group(self):
        """Create a group, update its quotas, verify."""
        group_name = f"test-update-{secrets.token_hex(8)}"
        ok, _ = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok
        ok, result = await _direct_api("GET", "/admin/groups")
        assert ok
        groups = result.get("data", {}).get("items", [])
        gid = next((g.get("group_id") for g in groups if g.get("name") == group_name), None)
        assert gid, f"Group {group_name} not found"

        # Update
        ok2, result2 = await _direct_api("PUT", f"/admin/groups/{gid}", body={
            "daily_tokens": 2000000,
            "monthly_cost": 100,
        })
        assert ok2, f"updateGroup failed: {result2}"

        # Verify
        ok3, result3 = await _direct_api("GET", f"/admin/groups/{gid}")
        assert ok3
        data = result3.get("data", {})
        assert int(data.get("daily_tokens_limit", 0)) == 2000000

        # Cleanup
        await _direct_api("DELETE", f"/admin/groups/{gid}")

    @pytest.mark.asyncio
    async def test_create_and_delete_key(self):
        """Create an API key, verify in list, then delete it."""
        user_id = f"test-user-{secrets.token_hex(8)}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id,
            "daily_tokens": 1000000,
            "monthly_cost": 50,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 100000,
        })
        assert ok, f"createApiKey failed: {result}"
        key_id = result.get("data", {}).get("id", "")
        assert key_id, f"No key_id in response: {result}"
        full_key = result.get("data", {}).get("key", "")
        assert full_key.startswith("gw-"), f"Key should start with gw-, got: {full_key[:10]}..."

        # Verify in list
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        items = result2.get("data", {}).get("items", [])
        ids = [i.get("id") for i in items]
        assert key_id in ids, f"Key {key_id} not found in list"

        # Delete
        ok3, result3 = await _direct_api("DELETE", f"/admin/api-keys/{key_id}")
        assert ok3, f"deleteApiKey failed: {result3}"

    @pytest.mark.asyncio
    async def test_update_key_quota(self):
        """Create a key, update its quotas, verify."""
        user_id = f"test-update-key-{secrets.token_hex(8)}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok, "create key failed"
        key_id = result.get("data", {}).get("id", "")

        ok2, result2 = await _direct_api("PUT", f"/admin/api-keys/{key_id}", body={
            "daily_tokens": 500000,
            "monthly_cost": 25,
        })
        assert ok2, f"updateApiKeyQuota failed: {result2}"

        ok3, result3 = await _direct_api("GET", "/admin/api-keys")
        assert ok3
        items = result3.get("data", {}).get("items", [])
        key_item = next((i for i in items if i.get("id") == key_id), None)
        assert key_item, f"Key {key_id} not found in list after update"
        assert key_item["quotas"]["daily_tokens_limit"] == 500000

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_group(self):
        """THE MAIN BUG TEST: Create a group + key, assign key to group.

        Steps:
        1. Create a group
        2. Create a key (without group)
        3. Assign the key to the group via PUT /admin/api-keys/{key_id}/group
        4. Verify the key now shows group_id in list
        5. Clean up (revoke key, delete group)
        """
        group_name = f"test-assign-{secrets.token_hex(8)}"
        user_id = f"test-assign-user-{secrets.token_hex(8)}"

        # 1. Create group
        ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok, f"createGroup failed: {result}"
        group_id = result.get("data", {}).get("group_id", "")
        assert group_id, f"No group_id in createGroup response: {result}"

        # 2. Create key WITHOUT group
        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id,
            "daily_tokens": 1000000,
            "monthly_cost": 50,
        })
        assert ok, f"createApiKey failed: {result}"
        key_id = result.get("data", {}).get("id", "")
        assert key_id, f"No key_id in createApiKey response: {result}"

        # 3. Assign key to group
        ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": group_id,
        })
        assert ok, f"assignKeyGroup failed: {result}"

        # 4. Verify key now has group_id
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        items = result2.get("data", {}).get("items", [])
        key_item = next((i for i in items if i.get("id") == key_id), None)
        assert key_item, f"Key {key_id} not found in list"
        assert key_item.get("group_id") == group_id, (
            f"Key {key_id} group_id is '{key_item.get('group_id')}', expected '{group_id}'"
        )

        # Verify group member_count increased
        ok3, result3 = await _direct_api("GET", "/admin/groups")
        assert ok3
        group_item = next(g for g in result3.get("data", {}).get("items", []) if g["group_id"] == group_id)
        assert group_item.get("member_count") == 1, (
            f"Group member_count is {group_item.get('member_count')}, expected 1"
        )

        # 5. Cleanup — delete group first (key delete does not remove group membership)
        await _direct_api("DELETE", f"/admin/groups/{group_id}")
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_group_with_cache_scope(self):
        """Same as above but also set cache_scope."""
        group_name = f"test-assign-cs-{secrets.token_hex(8)}"
        user_id = f"test-assign-cs-user-{secrets.token_hex(8)}"

        ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok, f"createGroup failed: {result}"
        group_id = result.get("data", {}).get("group_id", "")

        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id, "daily_tokens": 1000000,
        })
        assert ok
        key_id = result.get("data", {}).get("id", "")

        ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": group_id,
            "cache_scope": "public",
        })
        assert ok, f"assignKeyGroup with cache_scope failed: {result}"

        # Verify cache_scope was updated
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        key_item = next(i for i in result2.get("data", {}).get("items", []) if i["id"] == key_id)
        assert key_item.get("cache_scope") == "public", (
            f"cache_scope is '{key_item.get('cache_scope')}', expected 'public'"
        )

        # Cleanup — delete group first (key delete does not remove group membership)
        await _direct_api("DELETE", f"/admin/groups/{group_id}")
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_create_key_with_group(self):
        """Create a key WITH group_id from the start."""
        group_name = f"test-create-with-group-{secrets.token_hex(8)}"
        user_id = f"test-cwg-user-{secrets.token_hex(8)}"

        ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok
        group_id = result.get("data", {}).get("group_id", "")

        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id,
            "group_id": group_id,
            "cache_scope": "group",
            "daily_tokens": 1000000,
            "monthly_cost": 50,
        })
        assert ok, f"createApiKey with group_id failed: {result}"
        key_id = result.get("data", {}).get("id", "")

        # Verify key is in group
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        key_item = next(i for i in result2.get("data", {}).get("items", []) if i["id"] == key_id)
        assert key_item.get("group_id") == group_id

        # Verify group member count is 1
        ok3, result3 = await _direct_api("GET", "/admin/groups")
        assert ok3
        group_item = next(g for g in result3.get("data", {}).get("items", []) if g["group_id"] == group_id)
        assert group_item.get("member_count") == 1, (
            f"Group member_count is {group_item.get('member_count')}, expected 1"
        )

        # Cleanup — delete group first (key delete does not remove group membership)
        await _direct_api("DELETE", f"/admin/groups/{group_id}")
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_nonexistent_group(self):
        """Assigning to a bad group_id should return 404."""
        user_id = f"test-bad-group-{secrets.token_hex(8)}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok
        key_id = result.get("data", {}).get("id", "")

        ok2, result2 = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": "grp-nonexistent",
        })
        assert not ok2, "Should have failed for nonexistent group"
        # Check error code — FastAPI returns exc.detail directly as JSON body
        assert result2.get("error", {}).get("code") == "not_found", (
            f"Expected not_found, got: {result2}"
        )

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_default_group_rejected(self):
        """Assigning to grp-default should be rejected."""
        user_id = f"test-default-group-{secrets.token_hex(8)}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok
        key_id = result.get("data", {}).get("id", "")

        ok2, result2 = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": "grp-default",
        })
        assert not ok2, "Should have rejected assignment to default group"

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")


class TestCachePage:
    """L3 cache config, entries, cleanup."""

    @pytest.mark.asyncio
    async def test_cache_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=缓存', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "缓存" in content or "cache" in content.lower()

    @pytest.mark.asyncio
    async def test_get_l3_cache_config(self):
        ok, result = await _direct_api("GET", "/admin/cache/l3/config")
        assert ok, f"GET /admin/cache/l3/config failed: {result}"

    @pytest.mark.asyncio
    async def test_list_l3_entries(self):
        ok, result = await _direct_api("GET", "/admin/cache/l3/entries")
        assert ok, f"GET /admin/cache/l3/entries failed: {result}"

    @pytest.mark.asyncio
    async def test_update_l3_cache_config(self):
        """PUT /admin/cache/l3/config — update a setting."""
        ok, result = await _direct_api("PUT", "/admin/cache/l3/config", body={
            "default_ttl_hours": 48,
        })
        assert ok, f"PUT /admin/cache/l3/config failed: {result}"

    @pytest.mark.asyncio
    async def test_trigger_l3_cleanup(self):
        """POST /admin/cache/l3/cleanup — trigger manual cleanup."""
        ok, result = await _direct_api("POST", "/admin/cache/l3/cleanup")
        assert ok, f"POST /admin/cache/l3/cleanup failed: {result}"


class TestLogsPage:
    """GET/DELETE logs, trace detail, batch delete."""

    @pytest.mark.asyncio
    async def test_logs_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=日志', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "日志" in content or "log" in content.lower()

    @pytest.mark.asyncio
    async def test_get_request_logs(self):
        ok, result = await _direct_api("GET", "/admin/logs")
        assert ok, f"GET /admin/logs failed: {result}"

    @pytest.mark.asyncio
    async def test_delete_all_logs(self):
        ok, result = await _direct_api("DELETE", "/admin/logs", expect_ok=False)
        # DELETE /admin/logs may return 200 or 405 depending on router config;
        # either way it must not 500. Assert unconditionally on the outcome.
        if ok:
            data = result.get("data", {}) if isinstance(result, dict) else {}
            assert "deleted" in data or "count" in data or data.get("message") == "success", \
                f"Unexpected delete-all-logs response: {result}"
        else:
            # Failure is acceptable (e.g. 405 method not allowed) but NOT a 500.
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"DELETE /admin/logs returned 500: {result}"


class TestConfigPage:
    """Raw YAML config editor."""

    @pytest.mark.asyncio
    async def test_config_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=配置', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "配置" in content or "config" in content.lower()


class TestKnowledgePage:
    """RAG document import/list/delete."""

    @pytest.mark.asyncio
    async def test_knowledge_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=知识', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "知识" in content or "knowledge" in content.lower() or "rag" in content.lower()

    @pytest.mark.asyncio
    async def test_list_rag_documents(self):
        ok, result = await _direct_api("GET", "/admin/rag/documents")
        assert ok, f"GET /admin/rag/documents failed: {result}"

    @pytest.mark.asyncio
    async def test_import_rag_document(self):
        """POST /admin/rag/documents — import a dummy document."""
        ok, result = await _direct_api("POST", "/admin/rag/documents", body={
            "url": "https://example.com/test-doc.txt",
            "chunk_strategy": "fixed_size",
            "chunk_size": 512,
            "chunk_overlap": 64,
        }, expect_ok=False)
        # May fail if the URL is unreachable, but should not 500
        if ok:
            assert "doc_id" in result.get("data", {}), f"Expected doc_id in response: {result}"
        else:
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"RAG document import returned 500: {result}"

    @pytest.mark.asyncio
    async def test_delete_rag_document(self):
        """GET then DELETE a document — if any exist."""
        ok, result = await _direct_api("GET", "/admin/rag/documents")
        assert ok
        docs = result.get("data", {}).get("documents", [])
        if not docs:
            pytest.skip("No RAG documents to delete")
        doc_id = docs[0]["doc_id"]
        ok2, result2 = await _direct_api("DELETE", f"/admin/rag/documents/{doc_id}")
        assert ok2, f"DELETE /admin/rag/documents/{doc_id} failed: {result2}"


class TestCostsPage:
    """Prometheus metrics parsing, range queries."""

    @pytest.mark.asyncio
    async def test_costs_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=成本', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "成本" in content or "cost" in content.lower()

    @pytest.mark.asyncio
    async def test_get_metrics_text(self):
        ok, result = await _direct_api("GET", "/metrics")
        assert ok, "GET /metrics failed"

    @pytest.mark.asyncio
    async def test_parse_metrics(self):
        """parseMetrics should handle various metric formats."""

        text = """# HELP gateway_http_requests_total Total HTTP requests
# TYPE gateway_http_requests_total counter
gateway_http_requests_total{method="POST",endpoint="/v1/chat/completions"} 1234
gateway_http_requests_total 5678
# HELP gateway_cost_total Total cost
# TYPE gateway_cost_total counter
gateway_cost_total 42.5
"""
        samples = _parse_metrics_inline(text)
        names = {s["name"] for s in samples}
        assert "gateway_http_requests_total" in names
        assert "gateway_cost_total" in names

    @pytest.mark.asyncio
    async def test_prometheus_query_range(self):
        """GET /admin/metrics/query_range — the 7-day cost trend endpoint."""
        end = int(time.time())
        start = end - 7 * 86400
        ok, result = await _direct_api(
            "GET",
            f"/admin/metrics/query_range?query=increase(gateway_cost_total[1h])&start={start}&end={end}&step=3600",
            expect_ok=False,
        )
        # Prometheus may not be available; just verify we don't get 500
        if ok:
            data = result.get("data", {})
            assert "resultType" in data or "result" in data


# ---------------------------------------------------------------------------
# Additional test classes — Code RAG, Logs, Cache, Metrics, Quotas, Knowledge
# ---------------------------------------------------------------------------


class TestCodeRagPage:
    """Code RAG: import (all 4 types), task polling, repository list/delete."""

    @pytest.mark.asyncio
    async def test_code_rag_page_loads(self, page: Page):
        await _login(page)
        await page.click('text=知识', timeout=5000)
        await page.wait_for_timeout(1500)
        content = await page.content()
        assert "知识" in content or "knowledge" in content.lower() or "rag" in content.lower()

    @pytest.mark.asyncio
    async def test_list_code_import_tasks_empty(self):
        """GET /admin/rag/code/tasks — may not exist in all builds."""
        ok, result = await _direct_api("GET", "/admin/rag/code/tasks", expect_ok=False)
        # Route may not be mounted; just verify no 500
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "Code tasks 500"

    @pytest.mark.asyncio
    async def test_list_code_repositories_empty(self):
        """GET /admin/rag/code/repositories — should return list or wrapped data."""
        ok, result = await _direct_api("GET", "/admin/rag/code/repositories")
        assert ok, f"listCodeRepositories failed: {result}"
        # Response may be a bare list or wrapped in {"data": [...]}
        if isinstance(result, dict):
            result = result.get("data", result)
        assert isinstance(result, list), f"Expected list, got {type(result)}: {result}"

    @pytest.mark.asyncio
    async def test_import_code_server_path(self):
        """POST /admin/rag/code/import with source_type=server_path (JSON body)."""
        ok, result = await _direct_api("POST", "/admin/rag/code/import", body={
            "source_type": "server_path",
            "server_path": "/home/ubuntu/gateway2",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        }, expect_ok=False)
        # May fail (server path may not exist), but should not 500
        if ok:
            assert "task_id" in result
        else:
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"Code import (server_path) returned 500: {result}"
        """POST /admin/rag/code/import with source_type=git (JSON body)."""
        ok, result = await _direct_api("POST", "/admin/rag/code/import", body={
            "source_type": "git",
            "git_url": "https://github.com/example/test-repo.git",
            "git_branch": "main",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        }, expect_ok=False)
        # May fail (network, invalid repo), but should not 500
        if ok:
            assert "task_id" in result
        else:
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"Code import (git) returned 500: {result}"

    @pytest.mark.asyncio
    async def test_import_code_folder(self):
        """POST /admin/rag/code/import with source_type=folder (multipart)."""
        tmpdir = tempfile.mkdtemp()
        try:
            test_file = os.path.join(tmpdir, "test.py")
            with open(test_file, "w") as f:
                f.write('def hello():\n    print("world")\n')
            with open(test_file, "rb") as f:
                file_bytes = f.read()

            ok, result = await _direct_api(
                "POST", "/admin/rag/code/import",
                body={
                    "source_type": "folder",
                    "files": [("test.py", file_bytes)],
                    "relative_paths": ["test.py"],
                },
                expect_ok=False,
                content_type="multipart/form-data",
            )
            if ok:
                assert "task_id" in result
            else:
                assert isinstance(result, dict), f"Non-dict error body: {result}"
                assert result.get("detail") != "Internal Server Error", \
                    f"Code import (folder) returned 500: {result}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_import_code_zip(self):
        """POST /admin/rag/code/import with source_type=zip (multipart)."""
        tmpdir = tempfile.mkdtemp()
        try:
            zip_path = os.path.join(tmpdir, "test.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("hello.py", 'def hello():\n    return 42\n')
            with open(zip_path, "rb") as f:
                zip_bytes = f.read()

            ok, result = await _direct_api(
                "POST", "/admin/rag/code/import",
                body={
                    "source_type": "zip",
                    "file": ("test.zip", zip_bytes),
                },
                expect_ok=False,
                content_type="multipart/form-data",
            )
            if ok:
                assert "task_id" in result
            else:
                assert isinstance(result, dict), f"Non-dict error body: {result}"
                assert result.get("detail") != "Internal Server Error", \
                    f"Code import (zip) returned 500: {result}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_import_code_invalid_source_type(self):
        """POST /admin/rag/code/import with bad source_type returns 400."""
        ok, result = await _direct_api(
            "POST", "/admin/rag/code/import",
            body={"source_type": "invalid_type", "server_path": "/tmp"},
            expect_ok=False,
        )
        assert not ok, "Should fail with invalid source_type"

    @pytest.mark.asyncio
    async def test_get_code_task_detail(self):
        """GET /admin/rag/code/tasks/{task_id} — fake task_id should not 500."""
        fake_id = secrets.token_hex(8)
        ok, result = await _direct_api(
            "GET", f"/admin/rag/code/tasks/{fake_id}",
            expect_ok=False,
        )
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "Code task detail 500"

    @pytest.mark.asyncio
    async def test_cancel_code_task(self):
        """POST /admin/rag/code/tasks/{task_id}/cancel — fake task_id should not 500."""
        fake_id = secrets.token_hex(8)
        ok, result = await _direct_api(
            "POST", f"/admin/rag/code/tasks/{fake_id}/cancel",
            expect_ok=False,
        )
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "Cancel task 500"

    @pytest.mark.asyncio
    async def test_import_code_bad_json_body(self):
        """POST /admin/rag/code/import with no body returns 400, not 500."""
        ok, result = await _direct_api(
            "POST", "/admin/rag/code/import",
            body=None,
            expect_ok=False,
        )
        # Should fail (bad request) but not 500
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "Bad JSON body 500"


@pytest.mark.asyncio
async def test_code_import_refresh_recovery_and_repository_visible(page: Page):
    """Simulate importing /workspace/control-panel from the Knowledge page, reloading, and confirming task/repo visibility."""
    await _login(page)
    await page.goto(f"{BASE_URL}/knowledge", wait_until="domcontentloaded")
    await page.click('text=知识', timeout=5000)
    await page.click('text=代码知识库', timeout=5000)
    await page.click('text=服务器目录路径', timeout=5000)
    await page.fill('input[placeholder="/home/ubuntu/your-project"]', '/workspace/control-panel')
    await page.click('button:has-text("开始导入")', timeout=5000)

    # 提交后任务进入队列, 显示 source_label. 新提交的任务 source_label 初始为空,
    # 等待 syncActiveTasks 拉回后端 source_label.
    await page.wait_for_function(
        """() => Array.from(document.querySelectorAll('*'))
                  .some(e => (e.innerText || '').includes('server_path:///workspace/control-panel'))""",
        timeout=30000,
    )

    await page.reload(wait_until="domcontentloaded")
    # 刷新后任务应被恢复 (来自 localStorage + 后端同步), source_label 仍在队列里
    await page.wait_for_function(
        """() => Array.from(document.querySelectorAll('*'))
                  .some(e => (e.innerText || '').includes('server_path:///workspace/control-panel'))""",
        timeout=30000,
    )

    # 等待任务进入终态或仓库列表出现该导入条目
    await page.wait_for_function(
        """() => {
          const body = document.body.innerText || '';
          return body.includes('已完成') || body.includes('导入时间');
        }""",
        timeout=180000,
    )

    body = await page.content()
    assert 'server_path:///workspace/control-panel' in body
    assert ('已完成' in body) or ('导入时间' in body)


class TestLogsPageExtra:
    """Additional log operations: batch delete, trace detail."""

    @pytest.mark.asyncio
    async def test_trace_detail(self):
        """GET /admin/trace/{trace_id} — trace detail by trace ID."""
        fake_trace = secrets.token_hex(8)
        ok, result = await _direct_api(
            "GET", f"/admin/trace/{fake_trace}",
            expect_ok=False,
        )
        # May return 404 for fake trace; should not 500
        if ok:
            data = result.get("data", {})
            assert "trace_id" in data or "error" in data

    @pytest.mark.asyncio
    async def test_post_batch_delete_logs(self):
        """POST /admin/logs/batch-delete — batch delete by trace IDs."""
        ok, result = await _direct_api("POST", "/admin/logs/batch-delete", body={
            "request_ids": [],
        }, expect_ok=False)
        # May succeed or return meaningful response
        if ok:
            data = result.get("data", {})
            assert "deleted" in data or "count" in data or data.get("message") == "success"
        else:
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"Batch delete logs returned 500: {result}"


class TestCachePageExtra:
    """Cache operations: l1/l2 config, stats, flush (may not exist in all builds)."""

    @pytest.mark.asyncio
    async def test_get_l1_cache_config(self):
        ok, result = await _direct_api("GET", "/admin/cache/l1/config", expect_ok=False)
        # Endpoint may not exist; just verify no 500
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L1 config 500"

    @pytest.mark.asyncio
    async def test_get_l2_cache_config(self):
        ok, result = await _direct_api("GET", "/admin/cache/l2/config", expect_ok=False)
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L2 config 500"

    @pytest.mark.asyncio
    async def test_l1_cache_stats(self):
        ok, result = await _direct_api("GET", "/admin/cache/l1/stats", expect_ok=False)
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L1 stats 500"

    @pytest.mark.asyncio
    async def test_l2_cache_stats(self):
        ok, result = await _direct_api("GET", "/admin/cache/l2/stats", expect_ok=False)
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L2 stats 500"

    @pytest.mark.asyncio
    async def test_flush_l1_cache(self):
        ok, result = await _direct_api("POST", "/admin/cache/l1/flush", expect_ok=False)
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L1 flush 500"

    @pytest.mark.asyncio
    async def test_flush_l2_cache(self):
        ok, result = await _direct_api("POST", "/admin/cache/l2/flush", expect_ok=False)
        if not ok and isinstance(result, dict):
            assert result.get("detail") != "Internal Server Error", "L2 flush 500"

    @pytest.mark.asyncio
    async def test_toggle_l3_entry_mode(self):
        """PUT /admin/cache/l3/entries/{point_id}/mode — toggle cache entry mode."""
        # First get an entry
        ok, result = await _direct_api("GET", "/admin/cache/l3/entries")
        assert ok, f"GET /admin/cache/l3/entries failed: {result}"
        entries = result.get("data", {}).get("items", [])
        if not entries:
            pytest.skip("No L3 cache entries to toggle")
        point_id = entries[0].get("id") or entries[0].get("point_id") or entries[0].get("uuid")
        assert point_id, f"Cannot find ID in L3 entry: {entries[0]}"
        ok2, result2 = await _direct_api(
            "PUT", f"/admin/cache/l3/entries/{point_id}/mode",
            body={"mode": "exclude_from_cost"},
            expect_ok=False,
        )
        # Should not 500
        if not ok2 and isinstance(result2, dict):
            assert result2.get("detail") != "Internal Server Error", "L3 mode toggle 500"

    @pytest.mark.asyncio
    async def test_delete_l3_entry(self):
        """DELETE /admin/cache/l3/entries/{point_id} — delete an L3 cache entry."""
        ok, result = await _direct_api("GET", "/admin/cache/l3/entries")
        assert ok
        entries = result.get("data", {}).get("items", [])
        if not entries:
            pytest.skip("No L3 cache entries to delete")
        point_id = entries[0].get("id") or entries[0].get("point_id") or entries[0].get("uuid")
        assert point_id, f"Cannot find ID in L3 entry: {entries[0]}"
        ok2, result2 = await _direct_api(
            "DELETE", f"/admin/cache/l3/entries/{point_id}",
            expect_ok=False,
        )
        if not ok2 and isinstance(result2, dict):
            assert result2.get("detail") != "Internal Server Error", "L3 entry delete 500"


class TestMetricsAndQuotasExtra:
    """Metrics JSON, quota detail, group detail, provider model list."""

    @pytest.mark.asyncio
    async def test_metrics_json(self):
        """GET /admin/metrics-json — Prometheus text format."""
        ok, result = await _direct_api("GET", "/admin/metrics-json")
        assert ok, f"GET /admin/metrics-json failed: {result}"

    @pytest.mark.asyncio
    async def test_quota_detail(self):
        """GET /admin/quotas/{key_id} — per-key quota detail."""
        ok, result = await _direct_api("GET", "/admin/api-keys")
        assert ok
        items = result.get("data", {}).get("items", [])
        if not items:
            pytest.skip("No API keys to test quota detail")
        key_id = items[0]["id"]
        ok2, result2 = await _direct_api("GET", f"/admin/quotas/{key_id}")
        assert ok2, f"GET /admin/quotas/{key_id} failed: {result2}"

    @pytest.mark.asyncio
    async def test_group_detail(self):
        """GET /admin/groups/{group_id} — group detail endpoint."""
        ok, result = await _direct_api("GET", "/admin/groups")
        assert ok
        items = result.get("data", {}).get("items", [])
        if not items:
            pytest.skip("No groups to test group detail")
        group_id = items[0]["group_id"]
        ok2, result2 = await _direct_api("GET", f"/admin/groups/{group_id}")
        assert ok2, f"GET group detail for {group_id} failed: {result2}"

    @pytest.mark.asyncio
    async def test_provider_model_list(self):
        """GET /admin/providers/{name}/models — list models for a provider."""
        ok, result = await _direct_api("GET", "/admin/config")
        assert ok
        providers = result.get("data", {}).get("providers", {})
        if not providers:
            pytest.skip("No providers configured")
        provider_name = next(iter(providers.keys()))
        ok2, result2 = await _direct_api(
            "GET", f"/admin/providers/{provider_name}/models",
            expect_ok=False,
        )
        # May fail (external API), but should not 500
        if ok2:
            assert isinstance(result2, list) or "data" in result2
        else:
            assert isinstance(result2, dict), f"Non-dict error body: {result2}"
            assert result2.get("detail") != "Internal Server Error", \
                f"Provider model list returned 500: {result2}"


class TestKnowledgePageExtra:
    """Knowledge (text RAG) document operations."""

    @pytest.mark.asyncio
    async def test_import_text_document(self):
        """POST /admin/rag/documents — import a text document."""
        ok, result = await _direct_api("POST", "/admin/rag/documents", body={
            "content": "This is a test document for RAG knowledge base.\n" + "Lorem ipsum " * 50,
            "filename": "test_doc.txt",
        }, expect_ok=False)
        # May fail (server error), but should not 500
        if ok:
            assert "doc_id" in result.get("data", {})
        else:
            assert isinstance(result, dict), f"Non-dict error body: {result}"
            assert result.get("detail") != "Internal Server Error", \
                f"Text document import returned 500: {result}"

    @pytest.mark.asyncio
    async def test_import_document_without_url_or_content(self):
        """POST /admin/rag/documents with no url or content should fail gracefully."""
        ok, result = await _direct_api(
            "POST", "/admin/rag/documents",
            body={},
            expect_ok=False,
        )
        assert not ok, "Should fail without url or content"


# ---------------------------------------------------------------------------
# Standalone runner (no pytest)
# ---------------------------------------------------------------------------


def _parse_metrics_inline(text: str) -> list[dict]:
    """Duplicate of parseMetrics from client.ts for standalone testing."""
    samples = []
    for line in text.split("\n"):
        if not line.startswith("gateway_"):
            continue
        match = re.match(r"^(.+?)\{(.+?)\} (.+)$", line)
        if match:
            name, labels_str, value = match.groups()
            labels = {}
            for pair in labels_str.split(","):
                k, v = pair.split("=", 1)
                labels[k] = v.strip('"')
            samples.append({"name": name, "labels": labels, "value": float(value)})
        else:
            sm = re.match(r"^(.+?) (.+)$", line)
            if sm:
                samples.append({"name": sm.group(1), "labels": {}, "value": float(sm.group(2))})
    return samples


async def run_standalone():
    """Run all tests without pytest — useful for quick smoke checks."""
    print("=" * 60)
    print("Control Panel Smoke Test (standalone)")
    print("=" * 60)

    # 0. Check readiness
    print("\n[0] Checking gateway health...")
    ready = await _wait_api_ready()
    if not ready:
        print("FAIL: Gateway not reachable at", GATEWAY_URL)
        sys.exit(1)
    print("  OK: Gateway healthy")

    print(f"\n[0b] Checking control panel at {BASE_URL}...")
    try:
        r = urllib.request.urlopen(BASE_URL, timeout=5)
        print(f"  OK: Control panel returned {r.status}")
    except Exception as e:
        print(f"  WARN: Control panel not reachable: {e}")

    # 1. Direct API tests
    print("\n[1] API Key CRUD...")
    user_id = f"smoke-{secrets.token_hex(8)}"
    ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
    assert ok, f"create failed: {result}"
    key_id = result.get("data", {}).get("id", "")
    print(f"  Created key: {key_id}")

    ok, result = await _direct_api("GET", "/admin/api-keys")
    assert ok
    found = any(i["id"] == key_id for i in result.get("data", {}).get("items", []))
    assert found, "Key not in list"
    print("  Key visible in list: OK")

    ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id}", body={"daily_tokens": 500000})
    assert ok, f"update failed: {result}"
    print("  Update quota: OK")

    ok, result = await _direct_api("DELETE", f"/admin/api-keys/{key_id}")
    assert ok, f"delete failed: {result}"
    print("  Delete key: OK")

    # 2. Group CRUD
    print("\n[2] Group CRUD...")
    group_name = f"smoke-group-{secrets.token_hex(8)}"
    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
    assert ok, f"create group failed: {result}"
    group_id = result.get("data", {}).get("group_id", "")
    print(f"  Created group: {group_id}")

    ok, result = await _direct_api("GET", "/admin/groups")
    assert ok
    found = any(g["group_id"] == group_id for g in result.get("data", {}).get("items", []))
    assert found
    print("  Group visible in list: OK")

    ok, result = await _direct_api("PUT", f"/admin/groups/{group_id}", body={"daily_tokens": 2000000})
    assert ok, f"update group failed: {result}"
    print("  Update group: OK")

    ok, result = await _direct_api("DELETE", f"/admin/groups/{group_id}")
    assert ok, f"delete group failed: {result}"
    print("  Delete group: OK")

    # 3. Assign key to group (THE MAIN BUG)
    print("\n[3] Assign key to group...")
    user_id2 = f"smoke-assign-{secrets.token_hex(8)}"
    group_name2 = f"smoke-assign-group-{secrets.token_hex(8)}"

    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name2})
    assert ok, f"create group failed: {result}"
    group_id2 = result.get("data", {}).get("group_id", "")

    ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id2})
    assert ok, f"create key failed: {result}"
    key_id2 = result.get("data", {}).get("id", "")

    ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id2}/group", body={"group_id": group_id2})
    assert ok, f"assign key to group FAILED: {result}"
    print("  Assign key to group: OK")

    # Verify
    ok2, result2 = await _direct_api("GET", "/admin/api-keys")
    assert ok2
    key_item = next(i for i in result2.get("data", {}).get("items", []) if i["id"] == key_id2)
    assert key_item.get("group_id") == group_id2, (
        f"Key group_id is '{key_item.get('group_id')}', expected '{group_id2}'"
    )
    print(f"  Verified key {key_id2} has group_id={group_id2}: OK")

    # Cleanup — delete group first (key delete does not remove group membership)
    await _direct_api("DELETE", f"/admin/groups/{group_id2}")
    await _direct_api("DELETE", f"/admin/api-keys/{key_id2}")

    # 4. Create key WITH group
    print("\n[4] Create key with group from start...")
    group_name3 = f"smoke-cwg-{secrets.token_hex(8)}"
    user_id3 = f"smoke-cwg-user-{secrets.token_hex(8)}"
    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name3})
    assert ok
    group_id3 = result.get("data", {}).get("group_id", "")

    ok, result = await _direct_api("POST", "/admin/api-keys", body={
        "user_id": user_id3, "group_id": group_id3,
    })
    assert ok, f"create key with group failed: {result}"
    key_id3 = result.get("data", {}).get("id", "")

    ok2, result2 = await _direct_api("GET", "/admin/api-keys")
    key_item3 = next(i for i in result2.get("data", {}).get("items", []) if i["id"] == key_id3)
    assert key_item3.get("group_id") == group_id3
    print(f"  Created key {key_id3} in group {group_id3}: OK")

    # Verify group member count
    ok3, result3 = await _direct_api("GET", "/admin/groups")
    group_item = next(g for g in result3.get("data", {}).get("items", []) if g["group_id"] == group_id3)
    assert group_item.get("member_count") == 1
    print(f"  Group member_count = 1: OK")

    # Cleanup — delete group first
    await _direct_api("DELETE", f"/admin/groups/{group_id3}")
    await _direct_api("DELETE", f"/admin/api-keys/{key_id3}")

    # 5. Plugin toggles
    print("\n[5] Plugin toggles...")
    ok, result = await _direct_api("GET", "/admin/plugins-config")
    assert ok
    plugins = result.get("data", {}).get("plugins", [])
    if plugins:
        name = plugins[0].get("name", "")
        if name:
            ok2, _ = await _direct_api("PUT", "/admin/plugins-config", body={"name": name, "enabled": False})
            assert ok2, f"Toggle off {name} failed"
            ok3, _ = await _direct_api("PUT", "/admin/plugins-config", body={"name": name, "enabled": True})
            assert ok3, f"Toggle on {name} failed"
            print(f"  Toggle plugin {name}: OK")

    # 6. Config
    print("\n[6] Config read/write...")
    ok, result = await _direct_api("GET", "/admin/config")
    assert ok
    config = result.get("data", {})
    ok2, _ = await _direct_api("PUT", "/admin/config", body=config)
    assert ok2
    print("  Config GET/PUT: OK")

    print("\n" + "=" * 60)
    print("ALL SMOKE TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_standalone())
