"""Real-browser checks of the app token (skipped unless Python Playwright is installed).

Run with:  uv run --with playwright pytest tests/test_browser_lock.py
(set PC_CHROMIUM to a Chromium binary if Playwright's own browser isn't installed)."""
import os
import socket
import threading
import time

import pytest
import uvicorn

from privatechat.app import create_app
from privatechat.crypto import FAST_KDF
from test_api import APP_TOKEN, PW, Clock, fake_llm, slots_ok

sync_api = pytest.importorskip("playwright.sync_api")

BRAVE_DROPS_UNLOAD = ("Brave drops requests sent while a page unloads (keepalive fetch and sendBeacon alike), "
                      "so tab-close locking can't be tested there; see SECURITY_REVIEW.md I8")


def _is_brave(exe: str | None) -> bool:
    return bool(exe) and any("brave" in p.lower() for p in (exe, os.path.realpath(exe)))


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def no_tor():
        return False

    async def counter(text):
        return len(text) // 3 + 1

    app = create_app(tmp_path, kdf=FAST_KDF, llm=fake_llm, clock=Clock(), slots_check=slots_ok,
                     tor_check=no_tor, counter=counter, app_token=APP_TOKEN)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("PC_CHROMIUM")
        b = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        yield b
        b.close()


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.mark.skipif(_is_brave(os.environ.get("PC_CHROMIUM")), reason=BRAVE_DROPS_UNLOAD)
def test_closing_the_tab_locks_the_vault(server, browser):
    app, url = server
    page = browser.new_page()
    page.goto(f"{url}/?k={APP_TOKEN}")
    page.wait_for_function("document.querySelector('#empty') !== null")
    page.evaluate("""async (pw) => {
        const r = await api('/api/vault/create', { method: 'POST', body: { password: pw } });
        state.token = (await r.json()).token;
    }""", PW)
    assert app.state.vault.is_unlocked
    page.close(run_before_unload=True)  # fires pagehide -> keepalive lock request
    assert _wait(lambda: not app.state.vault.is_unlocked)


def test_reload_keeps_working(server, browser):
    app, url = server
    page = browser.new_page()
    page.goto(f"{url}/?k={APP_TOKEN}")
    page.wait_for_function("document.querySelector('#empty') !== null")
    page.reload()
    page.wait_for_function("document.querySelector('#empty') !== null")
    assert "k=" in page.url
    assert page.evaluate("(async () => (await api('/api/chats')).status)()") == 200


def test_page_without_key_explains_and_reads_nothing(server, browser):
    app, url = server
    page = browser.new_page()
    page.goto(f"{url}/")
    page.wait_for_function("document.body.textContent.includes('no valid app key')")
    assert page.locator("#messages").count() == 0
