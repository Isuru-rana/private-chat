import asyncio

import httpx
import pytest

from privatechat.web.net import WebError
from privatechat.web.search import search


def run(coro):
    return asyncio.run(coro)


def mk(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_search_parses_and_caps_results():
    rows = [{"title": f"T{i}", "url": f"https://e.com/{i}", "content": f"S{i}"} for i in range(10)]
    rows.insert(1, {"title": "bad", "url": "javascript:x()", "content": "x"})
    rows.insert(3, {"title": "dup", "url": "https://e.com/0", "content": "x"})
    rows.insert(4, {"title": None, "url": "https://e.com/none", "content": None})
    res = run(search("q", client=mk(lambda r: httpx.Response(200, json={"results": rows}))))
    assert len(res) == 8
    urls = [r["url"] for r in res]
    assert len(set(urls)) == 8 and all(u.startswith("https://") for u in urls)
    assert res[0] == {"title": "T0", "url": "https://e.com/0", "snippet": "S0"}
    assert all(isinstance(r["title"], str) and isinstance(r["snippet"], str) for r in res)


def test_search_down_raises_weberror():
    def boom(req):
        raise httpx.ConnectError("no")

    with pytest.raises(WebError, match="SearXNG not running"):
        run(search("q", client=mk(boom)))
    with pytest.raises(WebError, match="SearXNG not running"):
        run(search("q", client=mk(lambda r: httpx.Response(500))))


def test_bad_json_raises_weberror():
    with pytest.raises(WebError):
        run(search("q", client=mk(lambda r: httpx.Response(200, content=b"<html>"))))
    with pytest.raises(WebError):
        run(search("q", client=mk(lambda r: httpx.Response(200, json={"nope": 1}))))


def test_query_sent_as_param():
    seen = []

    def h(req):
        seen.append(req.url)
        return httpx.Response(200, json={"results": []})

    assert run(search("a b&c", client=mk(h))) == []
    assert seen[0].params["format"] == "json" and seen[0].params["q"] == "a b&c"
    assert seen[0].path == "/search"
