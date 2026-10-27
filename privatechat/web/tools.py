"""Web tool runner: executes Gemma's tool calls under the link rule and limits."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .net import WebError
from .pages import fetch_image, read_page
from .search import search

REFUSED = "refused: you can only open links from search results, pages you read, or the user's message"
INVALID = "invalid tool call"


def _spec(name, desc, prop, pdesc):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {prop: {"type": "string", "description": pdesc}},
        "required": [prop]}}}


TOOL_SPECS = [
    _spec("web_search", "Search the web. Cite the pages you use in your answer.", "query", "Search query"),
    _spec("read_page", "Read a web page as text. Only URLs from search results, pages already read, "
          "or the user's message can be opened. Cite pages you use.", "url", "Exact URL to open"),
    _spec("view_image", "Look at an image. Only image URLs listed on a page you read or given by the "
          "user can be opened.", "url", "Exact image URL"),
]


def normalize_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except ValueError:
        return ""


_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)


def _trim_url(u: str) -> str:
    """Strip trailing punctuation; a trailing ')' only when unbalanced (Wikipedia links keep theirs)."""
    u = u.rstrip(".,;:!?]}")
    while u.endswith(")") and u.count(")") > u.count("("):
        u = u[:-1].rstrip(".,;:!?]}")
    return u


def urls_in_text(text: str) -> list[str]:
    urls = [_trim_url(u) for u in _URL.findall(text)]
    return [u for u in urls if normalize_url(u)]


@dataclass
class ToolOutcome:
    text: str
    image: dict | None
    step: dict
    source: dict | None


class WebSession:
    MAX_IMAGES = 3

    def __init__(self, allowed: set[str], *, client: httpx.AsyncClient, searcher=search,
                 page_reader=read_page, image_fetcher=fetch_image):
        self.allowed = {n for n in map(normalize_url, allowed) if n}
        self.client = client
        self.searcher, self.page_reader, self.image_fetcher = searcher, page_reader, image_fetcher
        self.images_used = 0
        self.new_urls: set[str] = set()

    def _allow(self, url: str):
        n = normalize_url(url)
        if n and n not in self.allowed:
            self.allowed.add(n)
            self.new_urls.add(n)

    @staticmethod
    def _out(text, kind, key, val, status, detail="", image=None, source=None):
        return ToolOutcome(text, image, {"kind": kind, key: val, "status": status, "detail": detail}, source)

    async def run(self, name: str, args: dict) -> ToolOutcome:
        kinds = {"web_search": ("search", "query"), "read_page": ("page", "url"),
                 "view_image": ("image", "url")}
        if name not in kinds:
            return self._out(INVALID, "search", "query", "", "error")
        kind, key = kinds[name]
        val = args.get(key) if isinstance(args, dict) else None
        if not isinstance(val, str) or not val.strip():
            return self._out(INVALID, kind, key, "", "error")
        val = val.strip()
        try:
            if kind == "search":
                return await self._search(val)
            nv = normalize_url(val)
            if not nv or nv not in self.allowed:
                return self._out(REFUSED, kind, key, val, "refused", "link not allowed")
            if kind == "image":
                if self.images_used >= self.MAX_IMAGES:
                    return self._out("picture limit reached for this answer", kind, key, val,
                                     "refused", "picture limit")
                self.images_used += 1  # reserve before awaiting (concurrent calls); failures count
                img = await self.image_fetcher(self.client, val)
                return self._out("image loaded", kind, key, val, "done", image=img)
            return await self._page(val)
        except WebError as e:
            return self._out(f"error: {e}", kind, key, val, "error", str(e))
        except Exception:
            return self._out("error: the request failed", kind, key, val, "error", "request failed")

    async def _search(self, query):
        results = await self.searcher(query)
        lines = []
        for i, r in enumerate(results, 1):
            self._allow(r["url"])
            lines.append(f"{i}. {r['title']} — {r['url']}\n   {r['snippet']}")
        return self._out("\n".join(lines) or "no results", "search", "query", query, "done",
                         f"{len(results)} results")

    async def _page(self, url):
        p = await self.page_reader(self.client, url)
        for u in [p.url] + [i["url"] for i in p.images] + [l["url"] for l in p.links]:
            self._allow(u)
        parts = [p.title, p.url, "", p.text]
        if p.images:
            parts += ["", "Images:"] + [f"{n}. {i['url']} — {i['alt']}" for n, i in enumerate(p.images, 1)]
        if p.links:
            parts += ["", "Links:"] + [f"{n}. {l['url']} — {l['text']}" for n, l in enumerate(p.links, 1)]
        return self._out("\n".join(parts), "page", "url", url, "done",
                         "truncated" if p.truncated else "",
                         source={"url": p.url, "title": p.title, "text": p.text})
