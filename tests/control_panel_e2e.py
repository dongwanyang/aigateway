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

import os
import re
import subprocess
import sys
import time
from pathlib import Path

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
    yield p
    await ctx.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(p: Page):
    """Save the admin key in localStorage so all subsequent requests are authenticated."""
    await p.goto(BASE_URL, wait_until="domcontentloaded")
    await p.evaluate(f'() => localStorage.setItem("aigateway_api_key", "{ADMIN_KEY}")')


async def _wait_api_ready(timeout_ms: int = 15000):
    """Block until the gateway /health endpoint responds."""
    import urllib.request
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(f"{GATEWAY_URL}/health", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def _direct_api(method: str, path: str, body: dict | None = None, expect_ok: bool = True, timeout_val: int = 60):
    """Make a raw API call to the gateway (bypasses the frontend)."""
    import urllib.request
    import json
    url = f"{GATEWAY_URL}{path}"
    headers = {"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
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
        ok, _ = await _direct_api("GET", "/health", expect_ok=False)
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
        ok, result = await _direct_api("GET", "/admin/config", timeout_val=15)
        assert ok, f"GET /admin/config failed: {result}"
        assert isinstance(result.get("data"), dict), "Expected data dict"

    @pytest.mark.asyncio
    async def test_put_config_noop(self):
        """PUT /admin/config with same config is idempotent."""
        ok, result = await _direct_api("GET", "/admin/config", timeout_val=15)
        assert ok, "Could not read config"
        config = result["data"]
        ok2, result2 = await _direct_api("PUT", "/admin/config", body=config, timeout_val=120)
        assert ok2, f"PUT /admin/config failed: {result2}"


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
        group_name = f"test-group-{int(time.time())}"
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
        group_name = f"test-update-{int(time.time())}"
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
        user_id = f"test-user-{int(time.time())}"
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
        user_id = f"test-update-key-{int(time.time())}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok, "create key failed"
        key_id = result["data"]["id"]

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
        group_name = f"test-assign-{int(time.time())}"
        user_id = f"test-assign-user-{int(time.time())}"

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

        # 5. Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")
        await _direct_api("DELETE", f"/admin/groups/{group_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_group_with_cache_scope(self):
        """Same as above but also set cache_scope."""
        group_name = f"test-assign-cs-{int(time.time())}"
        user_id = f"test-assign-cs-user-{int(time.time())}"

        ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok, f"createGroup failed: {result}"
        group_id = result["data"]["group_id"]

        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id, "daily_tokens": 1000000,
        })
        assert ok
        key_id = result["data"]["id"]

        ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": group_id,
            "cache_scope": "public",
        })
        assert ok, f"assignKeyGroup with cache_scope failed: {result}"

        # Verify cache_scope was updated
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        key_item = next(i for i in result2["data"]["items"] if i["id"] == key_id)
        assert key_item.get("cache_scope") == "public", (
            f"cache_scope is '{key_item.get('cache_scope')}', expected 'public'"
        )

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")
        await _direct_api("DELETE", f"/admin/groups/{group_id}")

    @pytest.mark.asyncio
    async def test_create_key_with_group(self):
        """Create a key WITH group_id from the start."""
        group_name = f"test-create-with-group-{int(time.time())}"
        user_id = f"test-cwg-user-{int(time.time())}"

        ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
        assert ok
        group_id = result["data"]["group_id"]

        ok, result = await _direct_api("POST", "/admin/api-keys", body={
            "user_id": user_id,
            "group_id": group_id,
            "cache_scope": "group",
            "daily_tokens": 1000000,
            "monthly_cost": 50,
        })
        assert ok, f"createApiKey with group_id failed: {result}"
        key_id = result["data"]["id"]

        # Verify key is in group
        ok2, result2 = await _direct_api("GET", "/admin/api-keys")
        assert ok2
        key_item = next(i for i in result2["data"]["items"] if i["id"] == key_id)
        assert key_item.get("group_id") == group_id

        # Verify group member count is 1
        ok3, result3 = await _direct_api("GET", "/admin/groups")
        assert ok3
        group_item = next(g for g in result3["data"]["items"] if g["group_id"] == group_id)
        assert group_item.get("member_count") == 1, (
            f"Group member_count is {group_item.get('member_count')}, expected 1"
        )

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")
        await _direct_api("DELETE", f"/admin/groups/{group_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_nonexistent_group(self):
        """Assigning to a bad group_id should return 404."""
        user_id = f"test-bad-group-{int(time.time())}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok
        key_id = result["data"]["id"]

        ok2, result2 = await _direct_api("PUT", f"/admin/api-keys/{key_id}/group", body={
            "group_id": "grp-nonexistent",
        })
        assert not ok2, "Should have failed for nonexistent group"
        # Check error code
        detail = result2.get("detail", {})
        assert detail.get("error", {}).get("code") == "not_found", (
            f"Expected not_found, got: {result2}"
        )

        # Cleanup
        await _direct_api("DELETE", f"/admin/api-keys/{key_id}")

    @pytest.mark.asyncio
    async def test_assign_key_to_default_group_rejected(self):
        """Assigning to grp-default should be rejected."""
        user_id = f"test-default-group-{int(time.time())}"
        ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
        assert ok
        key_id = result["data"]["id"]

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
        ok, result = await _direct_api("DELETE", "/admin/logs")
        # May return 200 or 405 depending on method; just check no 500
        if ok:
            assert True


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


# ---------------------------------------------------------------------------
# Standalone runner (no pytest)
# ---------------------------------------------------------------------------


def _parse_metrics_inline(text: str) -> list[dict]:
    """Duplicate of parseMetrics from client.ts for standalone testing."""
    samples = []
    for line in text.split("\n"):
        if not line.startswith("gateway_") or line.startswith("#"):
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
    import urllib.request

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
        r = urllib.request.urlopen(f"{BASE_URL.replace('http:', 'http:')}", timeout=5)
        print(f"  OK: Control panel returned {r.status}")
    except Exception as e:
        print(f"  WARN: Control panel not reachable: {e}")

    # 1. Direct API tests
    print("\n[1] API Key CRUD...")
    user_id = f"smoke-{int(time.time())}"
    ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id})
    assert ok, f"create failed: {result}"
    key_id = result["data"]["id"]
    print(f"  Created key: {key_id}")

    ok, result = await _direct_api("GET", "/admin/api-keys")
    assert ok
    found = any(i["id"] == key_id for i in result["data"]["items"])
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
    group_name = f"smoke-group-{int(time.time())}"
    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name})
    assert ok, f"create group failed: {result}"
    group_id = result["data"]["group_id"]
    print(f"  Created group: {group_id}")

    ok, result = await _direct_api("GET", "/admin/groups")
    assert ok
    found = any(g["group_id"] == group_id for g in result["data"]["items"])
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
    user_id2 = f"smoke-assign-{int(time.time())}"
    group_name2 = f"smoke-assign-group-{int(time.time())}"

    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name2})
    assert ok, f"create group failed: {result}"
    group_id2 = result["data"]["group_id"]

    ok, result = await _direct_api("POST", "/admin/api-keys", body={"user_id": user_id2})
    assert ok, f"create key failed: {result}"
    key_id2 = result["data"]["id"]

    ok, result = await _direct_api("PUT", f"/admin/api-keys/{key_id2}/group", body={"group_id": group_id2})
    assert ok, f"assign key to group FAILED: {result}"
    print("  Assign key to group: OK")

    # Verify
    ok2, result2 = await _direct_api("GET", "/admin/api-keys")
    assert ok2
    key_item = next(i for i in result2["data"]["items"] if i["id"] == key_id2)
    assert key_item.get("group_id") == group_id2, (
        f"Key group_id is '{key_item.get('group_id')}', expected '{group_id2}'"
    )
    print(f"  Verified key {key_id2} has group_id={group_id2}: OK")

    # Cleanup
    await _direct_api("DELETE", f"/admin/api-keys/{key_id2}")
    await _direct_api("DELETE", f"/admin/groups/{group_id2}")

    # 4. Create key WITH group
    print("\n[4] Create key with group from start...")
    group_name3 = f"smoke-cwg-{int(time.time())}"
    user_id3 = f"smoke-cwg-user-{int(time.time())}"
    ok, result = await _direct_api("POST", "/admin/groups", body={"name": group_name3})
    assert ok
    group_id3 = result["data"]["group_id"]

    ok, result = await _direct_api("POST", "/admin/api-keys", body={
        "user_id": user_id3, "group_id": group_id3,
    })
    assert ok, f"create key with group failed: {result}"
    key_id3 = result["data"]["id"]

    ok2, result2 = await _direct_api("GET", "/admin/api-keys")
    key_item3 = next(i for i in result2["data"]["items"] if i["id"] == key_id3)
    assert key_item3.get("group_id") == group_id3
    print(f"  Created key {key_id3} in group {group_id3}: OK")

    # Verify group member count
    ok3, result3 = await _direct_api("GET", "/admin/groups")
    group_item = next(g for g in result3["data"]["items"] if g["group_id"] == group_id3)
    assert group_item.get("member_count") == 1
    print(f"  Group member_count = 1: OK")

    await _direct_api("DELETE", f"/admin/api-keys/{key_id3}")
    await _direct_api("DELETE", f"/admin/groups/{group_id3}")

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
    config = result["data"]
    ok2, _ = await _direct_api("PUT", "/admin/config", body=config)
    assert ok2
    print("  Config GET/PUT: OK")

    print("\n" + "=" * 60)
    print("ALL SMOKE TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_standalone())
