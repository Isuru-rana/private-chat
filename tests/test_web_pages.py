import asyncio
import base64

import httpx
import pytest

from privatechat.web import net
from privatechat.web.net import WebError, isolation_for_chat, proxy_url, tor_client
from privatechat.web.pages import fetch_image, read_page

HTML = b"""<html><head><title> My  Page </title><style>.x{}</style><script>evil()</script></head>
<body><nav>NAVNOISE <a href="/nav">n</a></nav><header>HEADNOISE</header>
<h1>Hello</h1><p>First   para</p><p>Second <b>para</b></p>
<img src="/a.png" alt="A"><img src="pic/b.jpg"><img src="data:image/png;base64,xx"><img src="/a.png" alt="A">
<a href="../up">Up link</a><a href="javascript:x()">bad</a><a href="mailto:a@b.c">m</a>
<noscript>NOSCRIPT</noscript><form><input type="text" value="FORMNOISE"><select><option>x</option></select></form><footer>FOOTNOISE</footer></body></html>"""


def client(handler):
    return tor_client("t", transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def reply(body, ctype="text/html; charset=utf-8", status=200, headers=None):
    return lambda req: httpx.Response(status, content=body, headers={"content-type": ctype, **(headers or {})})


def test_read_page_extracts_text_title_images_links():
    async def go():
        async with client(reply(HTML)) as c:
            return await read_page(c, "https://ex.test/dir/page")

    p = run(go())
    assert p.title == "My Page"
    for noise in ("NAVNOISE", "HEADNOISE", "evil", "NOSCRIPT", "FORMNOISE", "FOOTNOISE", ".x{"):
        assert noise not in p.text
    assert "Hello\nFirst para\nSecond para" in p.text
    assert p.images == [{"url": "https://ex.test/a.png", "alt": "A"}, {"url": "https://ex.test/dir/pic/b.jpg", "alt": ""}]
    assert p.links == [{"url": "https://ex.test/up", "text": "Up link"}]
    assert not p.truncated


def test_text_capped_at_6000():
    body = b"<p>" + b"x" * 20000 + b"</p>"
    p = run(_read(body))
    assert len(p.text) == 6000


def test_images_and_links_capped():
    body = "".join(f'<img src="/i{i}.png"><a href="/l{i}">l{i}</a>' for i in range(50)).encode()
    p = run(_read(body))
    assert len(p.images) == 10 and len(p.links) == 20


async def _read(body):
    async with client(reply(body)) as c:
        return await read_page(c, "https://ex.test/")


def test_non_html_refused():
    async def go():
        async with client(reply(b"%PDF", "application/pdf")) as c:
            await read_page(c, "https://ex.test/a.pdf")

    with pytest.raises(WebError, match="not a web page"):
        run(go())


def test_non_http_url_refused():
    async def go():
        async with client(reply(HTML)) as c:
            await read_page(c, "file:///etc/passwd")

    with pytest.raises(WebError):
        run(go())


def test_body_cap():
    sent = 0

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal sent
            for _ in range(4 * 16):
                sent += 65536
                yield b"a" * 65536

    def h(req):
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=Body())

    async def go():
        async with client(h) as c:
            return await read_page(c, "https://ex.test/big")

    p = run(go())
    assert p.truncated
    assert sent <= 3 * 1024 * 1024 + 65536


def test_redirect_scheme_refused():
    def h(req):
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    async def go():
        async with client(h) as c:
            await read_page(c, "https://ex.test/")

    with pytest.raises(WebError):
        run(go())


def test_redirect_final_url_used_for_relative():
    def h(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.test/x/y"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b'<img src="z.png">')

    async def go():
        async with client(h) as c:
            return await read_page(c, "https://ex.test/start")

    assert run(go()).images[0]["url"] == "https://other.test/x/z.png"


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def test_fetch_image_validates_magic():
    async def ok():
        async with client(reply(PNG, "image/png")) as c:
            return await fetch_image(c, "https://ex.test/p/cat.png")

    r = run(ok())
    assert r == {"kind": "image", "name": "cat.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}

    async def bad():
        async with client(reply(b"<html>no</html>", "image/png")) as c:
            await fetch_image(c, "https://ex.test/p/cat.png")

    with pytest.raises(WebError):
        run(bad())


def test_fetch_image_too_big_and_default_name():
    big = PNG + b"0" * (10 * 1024 * 1024)

    async def go(body, url):
        async with client(reply(body, "image/png")) as c:
            return await fetch_image(c, url)

    with pytest.raises(WebError):
        run(go(big, "https://ex.test/a.png"))
    assert run(go(PNG, "https://ex.test/"))["name"] == "image"


def test_tor_client_uses_socks5h_with_isolation_user():
    assert proxy_url("chat-abc") == f"socks5h://chat-abc:x@{net.TOR_PROXY}"
    c = tor_client("chat-abc")
    assert not c.follow_redirects  # redirects are followed manually in pages._get
    assert c.headers["User-Agent"].startswith("Mozilla/5.0 (Windows NT 10.0")
    assert c.headers["Accept-Language"] == "en-US,en;q=0.5"
    assert "Referer" not in c.headers
    assert len(c._mounts) >= 1


def test_isolation_for_chat_stable_and_distinct():
    a = isolation_for_chat("one")
    assert a == isolation_for_chat("one") and a != isolation_for_chat("two")
    assert a.startswith("chat-") and len(a) == 5 + 16


# ---- fix round 1 ----
def test_no_cookies_stored_or_sent():
    seen = []

    def h(req):
        seen.append(req.headers.get("cookie"))
        if req.url.path == "/r":
            return httpx.Response(302, headers={"location": "/p", "set-cookie": "c=3"})
        return httpx.Response(200, headers={"content-type": "text/plain", "set-cookie": "a=1"}, content=b"x")

    async def go():
        async with client(h) as c:
            await read_page(c, "https://ex.test/p")
            await read_page(c, "https://ex.test/p")
            await read_page(c, "https://ex.test/r")

    run(go())
    assert seen == [None] * 4


def test_parser_fast_with_many_tags():
    import time


    body = b"".join(b'<img src="/i%d.png"><a href="/l%d">x</a>' % (i, i) for i in range(50000))
    t = time.perf_counter()
    p = run(_read(body))
    assert time.perf_counter() - t < 1.0
    assert len(p.images) == 10 and len(p.links) == 20


def test_compressed_bomb_refused():
    import gzip
    import time

    bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
    assert len(bomb) < 200_000
    t = time.perf_counter()

    async def go():
        async with client(reply(bomb, "text/html", headers={"content-encoding": "gzip"})) as c:
            return await read_page(c, "https://ex.test/")

    try:
        p = run(go())
        assert len(p.text) <= 3 * 1024 * 1024
    except WebError:
        pass
    assert time.perf_counter() - t < 2.0


def test_accept_encoding_identity_sent():
    got = []

    def h(req):
        got.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x")

    async def go():
        async with client(h) as c:
            await read_page(c, "https://ex.test/")

    run(go())
    assert got == ["identity"]


def test_total_deadline_cuts_slow_stream(monkeypatch):
    from privatechat.web import pages

    monkeypatch.setattr(pages, "DEADLINE", 0.2)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.05)
                yield b"a"

    def h(req):
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=Slow())

    async def go():
        async with client(h) as c:
            await read_page(c, "https://ex.test/")

    with pytest.raises(WebError, match="timed out"):
        run(go())


# ---- fix round 2 ----
def test_redirect_body_never_read():
    pulled = 0

    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal pulled
            for _ in range(1000):
                pulled += 65536
                yield b"\0" * 65536

    def h(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/end", "content-encoding": "gzip"}, stream=Bomb())
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"done")

    async def go():
        async with client(h) as c:
            return await read_page(c, "https://ex.test/start")

    p = run(go())
    assert p.text == "done" and p.url == "https://ex.test/end"
    assert pulled <= 65536


def test_too_many_redirects():
    def h(req):
        n = int(req.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    async def go():
        async with client(h) as c:
            await read_page(c, "https://ex.test/")

    with pytest.raises(WebError, match="redirect"):
        run(go())


def test_five_redirects_ok():
    def h(req):
        n = int(req.url.path.strip("/") or 0)
        if n < 5:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    async def go():
        async with client(h) as c:
            return await read_page(c, "https://ex.test/")

    assert run(go()).text == "ok"


def test_form_wrapped_page_keeps_its_text():
    body = b"<html><body><form runat='server'><h1>Ministry</h1><p>Opening hours are 9-5.</p><script>evil()</script></form></body></html>"
    p = run(_read(body))
    assert "Ministry" in p.text and "Opening hours are 9-5." in p.text and "evil" not in p.text
