import asyncio

from privatechat.web.net import WebError
from privatechat.web.pages import Page
from privatechat.web.tools import TOOL_SPECS, WebSession, normalize_url, urls_in_text

REFUSE = "refused: you can only open links from search results, pages you read, or the user's message"


def run(coro):
    return asyncio.run(coro)


async def fake_search(q):
    return [{"title": "T1", "url": "https://a.example/x", "snippet": "s1"},
            {"title": "T2", "url": "https://B.example/y#frag", "snippet": "s2"}]


async def fake_page(client, url):
    return Page(url="https://final.example/p", title="PT", text="BODY TEXT",
                images=[{"url": "https://img.example/1.png", "alt": "one"}],
                links=[{"url": "https://l.example/z", "text": "lk"}], truncated=False)


async def fake_img(client, url):
    return {"kind": "image", "name": "n.png", "mime": "image/png", "data": b"x"}


def sess(allowed=(), **kw):
    kw.setdefault("searcher", fake_search)
    kw.setdefault("page_reader", fake_page)
    kw.setdefault("image_fetcher", fake_img)
    return WebSession(set(allowed), client=object(), **kw)


def test_specs():
    names = [s["function"]["name"] for s in TOOL_SPECS]
    assert names == ["web_search", "read_page", "view_image"]


def test_helpers():
    assert normalize_url("HTTPS://Ex.COM/A#f") == "https://ex.com/A"
    assert urls_in_text("see https://a.com/x#y and http://b.org.") == ["https://a.com/x#y", "http://b.org"]


def test_search_makes_results_openable():
    s = sess()
    o = run(s.run("web_search", {"query": "q"}))
    assert o.step["kind"] == "search" and o.step["status"] == "done" and o.source is None
    assert "1. T1 — https://a.example/x" in o.text and "   s1" in o.text
    o = run(s.run("read_page", {"url": "https://b.example/y"}))
    assert o.step["status"] == "done"


def test_unlisted_url_refused():
    s = sess()
    for u in ["https://nope.example/", "http://attacker/?q=secret"]:
        o = run(s.run("read_page", {"url": u}))
        assert o.step["status"] == "refused" and o.text == REFUSE
        o = run(s.run("view_image", {"url": u}))
        assert o.step["status"] == "refused" and o.image is None


def test_url_from_user_message_allowed():
    s = sess({normalize_url(u) for u in urls_in_text("go https://U.example/a#x")})
    o = run(s.run("read_page", {"url": "https://u.example/a"}))
    assert o.step["status"] == "done"
    assert o.source == {"url": "https://final.example/p", "title": "PT", "text": "BODY TEXT"}
    assert "BODY TEXT" not in str(o.step)


def test_page_links_and_images_become_openable():
    s = sess({"https://u.example/a"})
    o = run(s.run("read_page", {"url": "https://u.example/a"}))
    assert "Images:" in o.text and "1. https://img.example/1.png" in o.text and "Links:" in o.text
    assert run(s.run("read_page", {"url": "https://l.example/z"})).step["status"] == "done"
    assert run(s.run("read_page", {"url": "https://final.example/p"})).step["status"] == "done"
    i = run(s.run("view_image", {"url": "https://img.example/1.png"}))
    assert i.step["status"] == "done" and i.image["kind"] == "image"
    assert "https://img.example/1.png" in s.new_urls


def test_fourth_image_refused():
    s = sess({"https://i/%d" % n for n in range(4)})
    for n in range(3):
        assert run(s.run("view_image", {"url": "https://i/%d" % n})).step["status"] == "done"
    o = run(s.run("view_image", {"url": "https://i/3"}))
    assert o.step["status"] == "refused" and o.text == "picture limit reached for this answer"
    assert s.images_used == 3


def test_weberror_reported():
    async def bad(q):
        raise WebError("tor down")
    async def boom(c, u):
        raise RuntimeError("secret https://x/?q=1")
    s = sess({"https://u/"}, searcher=bad, page_reader=boom)
    o = run(s.run("web_search", {"query": "q"}))
    assert o.step["status"] == "error" and o.text == "error: tor down"
    o = run(s.run("read_page", {"url": "https://u/"}))
    assert o.step["status"] == "error" and o.text == "error: the request failed"
    assert "secret" not in str(o.step) + o.text


def test_unknown_tool_and_bad_args():
    s = sess()
    for n, a in [("nope", {}), ("web_search", {}), ("web_search", {"query": 5}),
                 ("read_page", {"url": ""}), ("read_page", None), ("view_image", {"url": 3})]:
        o = run(s.run(n, a))
        assert o.step["status"] == "error" and o.text == "invalid tool call"


def test_fragment_ignored_in_link_rule():
    s = sess({"https://u.example/a#top"})
    assert run(s.run("read_page", {"url": "https://U.example/a#other"})).step["status"] == "done"


def test_concurrent_images_capped():
    calls = []

    async def slow(c, u):
        calls.append(u)
        await asyncio.sleep(0.01)
        return {"kind": "image", "name": "n", "mime": "image/png", "data": b"x"}
    s = sess({"https://i/%d" % n for n in range(4)}, image_fetcher=slow)

    async def go():
        return await asyncio.gather(*[s.run("view_image", {"url": "https://i/%d" % n}) for n in range(4)])
    outs = run(go())
    assert len(calls) == 3
    assert [o.step["status"] for o in outs].count("refused") == 1
    assert any(o.text == "picture limit reached for this answer" for o in outs)


def test_failed_image_fetch_counts():
    async def bad(c, u):
        raise WebError("x")
    s = sess({"https://i/%d" % n for n in range(4)}, image_fetcher=bad)
    for n in range(3):
        assert run(s.run("view_image", {"url": "https://i/%d" % n})).step["status"] == "error"
    assert run(s.run("view_image", {"url": "https://i/3"})).step["status"] == "refused"


def test_malformed_urls_do_not_crash():
    assert urls_in_text("see http://[::1 here") == []
    assert normalize_url("http://[::1") == ""
    s = sess({"http://[::1"})
    assert "" not in s.allowed
    for t in ("read_page", "view_image"):
        o = run(s.run(t, {"url": "http://[::1"}))
        assert o.step["status"] in ("refused", "error")


def test_urls_in_text_keeps_balanced_parens():
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert urls_in_text(wiki) == [wiki]
    assert urls_in_text(f"see {wiki}.") == [wiki]
    assert urls_in_text("(see https://example.com/a)") == ["https://example.com/a"]
    assert urls_in_text("(see https://example.com/a).") == ["https://example.com/a"]
