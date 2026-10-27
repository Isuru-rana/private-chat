"""SearXNG client. App -> SearXNG is plain loopback; SearXNG itself routes upstream via Tor."""
import os

import httpx

from .net import WebError

SEARX_URL = os.environ.get("PC_SEARX", "http://127.0.0.1:8888")
MAX_RESULTS = 8
_DOWN = "search is unavailable (SearXNG not running)"


async def search(query: str, *, client: httpx.AsyncClient | None = None) -> list[dict]:
    own = client is None
    c = client or httpx.AsyncClient(trust_env=False, timeout=30)
    try:
        r = await c.get(f"{SEARX_URL}/search", params={"q": query, "format": "json"})
    except httpx.HTTPError:
        raise WebError(_DOWN) from None
    finally:
        if own:
            await c.aclose()
    if r.status_code != 200:
        raise WebError(_DOWN)
    try:
        rows = r.json()["results"]
        out, seen = [], set()
        for row in rows:
            url = row.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            out.append({"title": str(row.get("title") or ""), "url": url,
                        "snippet": str(row.get("content") or "")})
            if len(out) == MAX_RESULTS:
                break
        return out
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WebError("search returned an unreadable response") from None
