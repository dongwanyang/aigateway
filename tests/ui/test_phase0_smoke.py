"""Phase 0 UI smoke: control-panel page loads and localStorage is pre-populated."""
from tests.ui.conftest import UI_BASE


def test_control_panel_loads(page, console_errors):
    page.goto(f"{UI_BASE}/", wait_until="domcontentloaded")
    # 至少应有一个 <div id="root"> 或 body 存在
    assert page.locator("body").count() == 1
    stored = page.evaluate("() => localStorage.getItem('aigateway_api_key')")
    assert stored and stored.startswith("gw-")
