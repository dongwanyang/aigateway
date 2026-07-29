"""Phase 0 UI smoke: control panel loads without exposing an API key."""
from tests.conftest import UI_BASE


def test_control_panel_loads(page, console_errors):
    page.goto(f"{UI_BASE}/", wait_until="domcontentloaded")
    # 至少应有一个 <div id="root"> 或 body 存在
    assert page.locator("body").count() == 1
    stored = page.evaluate("() => localStorage.getItem('aigateway_api_key')")
    assert stored is None
