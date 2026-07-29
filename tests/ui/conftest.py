"""Playwright browser + page fixtures for tests/ui/*.

The control panel authenticates with an HttpOnly browser-session cookie.
API keys must never be injected into browser storage.
"""
import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
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
