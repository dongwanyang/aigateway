"""Integration test for plugin enable/disable + per-plugin debug + 5 global debug dimensions.

Phases:
  Phase 1: 13 plugins individually — enable plugin + per_plugin debug + corresponding global dimension
  Phase 2: 5 global debug dimensions individually
  Phase 3: Incremental plugin accumulation (2→13) with all dims on
  Phase 4: Incremental global dimension accumulation (1→5) with all plugins on
  Phase 5: Full-on conflict detection — all 13 plugins + all per_plugin debug + all 5 dims
"""
import copy
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from starlette.testclient import TestClient

# Paths
REPO_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_PATH = REPO_ROOT / "config.yaml"
BACKUP_PATH = REPO_ROOT / "config.yaml.test-bak"
REPORT_PATH = REPO_ROOT / "docs" / "test" / "plugin_debug_test_report.md"

# Add source paths for imports
sys.path.insert(0, str(REPO_ROOT / "aigateway-api" / "src"))
sys.path.insert(0, str(REPO_ROOT / "aigateway-core" / "src"))

# Admin API key from config.yaml
ADMIN_KEY = "gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"
HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}

# Base chat request template
CHAT_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.7,
    "max_tokens": 50,
}

# ------------------------------------------------------------------
# Fixtures: config backup & restore
# ------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def config_backup():
    """Backup config.yaml before all tests, restore after."""
    # Backup
    if CONFIG_PATH.exists():
        shutil.copy2(str(CONFIG_PATH), str(BACKUP_PATH))
    yield
    # Restore
    if BACKUP_PATH.exists():
        shutil.copy2(str(BACKUP_PATH), str(CONFIG_PATH))
        try:
            BACKUP_PATH.unlink()
        except FileNotFoundError:
            pass


def reset_debug_state(client: TestClient) -> None:
    """Reset all debug switches to off via admin API."""
    # Reset global debug dimensions
    client.put(
        "/admin/global-config",
        json={"hot_reload": True, "debug_mode": True, "debug": {
            "frontend": False, "entry": False, "cache": False,
            "bridge": False, "plugins_enabled": False,
        }},
        headers=HEADERS,
    )
    # Reset all per_plugin debug to false
    for plugin_name in ALL_PLUGIN_NAMES:
        try:
            client.post(
                f"/admin/plugins/{plugin_name}/debug",
                json={"enabled": False},
                headers=HEADERS,
            )
        except Exception:
            pass  # Some plugins don't support per_plugin debug
