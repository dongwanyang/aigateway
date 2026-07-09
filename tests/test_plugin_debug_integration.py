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
