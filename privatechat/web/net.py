"""Tor-only HTTP client. Never a direct connection: socks5h makes the Tor exit resolve DNS."""
import asyncio
import hashlib
from http.cookiejar import DefaultCookiePolicy
import os

import httpx

TOR_PROXY = os.environ.get("PC_TOR", "127.0.0.1:9050")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept-Language": "en-US,en;q=0.5",
}


class WebError(Exception):
    """Message is safe to show to the model/user."""


def proxy_url(isolation: str) -> str:
    return f"socks5h://{isolation}:x@{TOR_PROXY}"


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def tor_client(isolation: str, *, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    kw = {"transport": transport} if transport is not None else {"proxy": proxy_url(isolation)}
    c = httpx.AsyncClient(
        follow_redirects=False, timeout=30.0, headers=HEADERS, trust_env=False, **kw
    )
    c.cookies.jar.set_policy(_NoCookies())  # constraint: no cookies, ever
    return c


def isolation_for_chat(chat_id: str) -> str:
    return "chat-" + hashlib.sha256(chat_id.encode()).hexdigest()[:16]


async def tor_reachable() -> bool:
    host, _, port = TOR_PROXY.rpartition(":")
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 2)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    w.close()
    return True
