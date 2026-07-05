"""Playwright browser + page fixtures for tests/ui/*.

Auth strategy: control-panel has no login page; useAuth reads
localStorage['aigateway_api_key']. We inject via add_init_script so it
runs before any page script.
"""
import pytest
from playwright.sync_api import sync_playwright

from tests.conftest import ADMIN_KEY, UI_BASE  # noqa: F401 -- re-exports for ui tests


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    ctx.add_init_script(
        f"localStorage.setItem('aigateway_api_key', '{ADMIN_KEY}');"
    )
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture
def console_errors(page):
    """Return a list that captures every console.error emitted while the fixture is alive."""
    errors: list = []
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    return errors
