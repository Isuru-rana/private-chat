"""Fetch and reduce web pages / images over a Tor client."""
import asyncio
import base64
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from ..attachments import MAX_IMAGE_BYTES, _image_mime
from .net import WebError

MAX_BODY = 3 * 1024 * 1024
MAX_TEXT = 6000
MAX_IMAGES = 10
MAX_LINKS = 20
MAX_REDIRECTS = 5
DEADLINE = 60.0  # total seconds per fetch (request + body)
SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "svg"}
BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main", "blockquote",
         "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dt", "dd", "figure", "figcaption"}


@dataclass
class Page:
    url: str
    title: str
    text: str
    images: list[dict]
    links: list[dict]
    truncated: bool


def _http(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


class _Reader(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip = 0
        self.in_title = False
        self.title = ""
        self.parts: list[str] = []
        self.images: list[dict] = []
        self.links: list[dict] = []
        self._link: dict | None = None
        self._seen_img: set[str] = set()
        self._seen_link: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        if self.skip:
            return
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "img" and a.get("src"):
            u = urljoin(self.base, a["src"].strip())
            if _http(u) and len(self.images) < MAX_IMAGES and u not in self._seen_img:
                self._seen_img.add(u)
                self.images.append({"url": u, "alt": " ".join((a.get("alt") or "").split())})
        elif tag == "a" and a.get("href"):
            u = urljoin(self.base, a["href"].strip())
            ok = _http(u) and len(self.links) < MAX_LINKS and u not in self._seen_link
            self._link = {"url": u, "text": ""} if ok else None

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
        if self.skip:
            return
        if tag in BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._link is not None:
            self._link["text"] = " ".join(self._link["text"].split())
            self._seen_link.add(self._link["url"])
            self.links.append(self._link)
            self._link = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip:
            return
        self.parts.append(data)
        if self._link is not None:
            self._link["text"] += data

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", ln).strip() for ln in raw.split("\n"))
        return "\n".join(ln for ln in lines if ln)


async def _one(b: bytes):
    yield b


async def _get(client: httpx.AsyncClient, url: str, limit: int, accept):
    """Stream a GET through the client. accept(content_type) -> None or error text.
    Returns (final_url, body, truncated, content_type)."""
    if not _http(url):
        raise WebError("only http(s) URLs can be fetched")
    try:
        async with asyncio.timeout(DEADLINE):
            # Redirects are followed by hand: httpx would read (and decode) every redirect body unbounded.
            for _ in range(MAX_REDIRECTS + 1):
                nxt = None
                async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r:
                    loc = r.headers.get("location")
                    if r.status_code in (301, 302, 303, 307, 308) and loc:
                        nxt = urljoin(str(r.url), loc.strip())  # body is never read, just closed
                        if not _http(nxt):
                            raise WebError("refused: redirect to a non-http(s) address")
                    else:
                        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                        if r.status_code >= 400:
                            raise WebError(f"the site answered HTTP {r.status_code}")
                        if r.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                            raise WebError("refused: compressed response")
                        bad = accept(ctype)
                        if bad:
                            raise WebError(bad)
                        buf = bytearray()
                        truncated = False
                        # raw bytes: the cap can't be bypassed by inflation. (Pre-read mock responses are in memory.)
                        async for chunk in (_one(r.content) if r.is_stream_consumed else r.aiter_raw()):
                            buf += chunk
                            if len(buf) > limit:
                                del buf[limit:]
                                truncated = True
                                break
                        return str(r.url), bytes(buf), truncated, r.headers.get("content-type", "")
                url = nxt
            raise WebError("too many redirects")
    except WebError:
        raise
    except TimeoutError:
        raise WebError("timed out") from None
    except httpx.TooManyRedirects:
        raise WebError("too many redirects") from None
    except httpx.UnsupportedProtocol:
        raise WebError("refused: redirect to a non-http(s) address") from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise WebError(f"could not fetch the page ({type(e).__name__})") from None


async def read_page(client: httpx.AsyncClient, url: str) -> Page:
    def accept(ct):
        if ct not in ("text/html", "text/plain"):
            return f"not a web page ({ct or 'unknown type'})"

    final, body, truncated, raw_ct = await _get(client, url, MAX_BODY, accept)
    m = re.search(r"charset=([\w-]+)", raw_ct, re.I)
    try:
        html = body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    if raw_ct.split(";")[0].strip().lower() == "text/plain":
        text = "\n".join(ln.strip() for ln in html.splitlines() if ln.strip())
        return Page(final, "", text[:MAX_TEXT], [], [], truncated)
    p = _Reader(final)
    p.feed(html)
    p.close()
    return Page(
        final, " ".join(p.title.split()), p.text()[:MAX_TEXT], p.images[:MAX_IMAGES], p.links[:MAX_LINKS], truncated
    )


async def fetch_image(client: httpx.AsyncClient, url: str) -> dict:
    final, body, truncated, _ = await _get(client, url, MAX_IMAGE_BYTES, lambda ct: None)
    if truncated:
        raise WebError("image is larger than 10 MB")
    mime = _image_mime(body)
    if not mime:
        raise WebError("not an image (png, jpeg, gif or webp)")
    name = unquote(urlsplit(final).path.rsplit("/", 1)[-1]) or "image"
    return {"kind": "image", "name": name[:120], "mime": mime, "data": base64.b64encode(body).decode()}
