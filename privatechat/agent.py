"""Agent tool loop: model rounds -> web tool calls -> results back -> final answer."""
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from .context import IMAGE_TOKENS
from .files import FileTools
from .web.tools import INVALID, TOOL_SPECS, WebSession

TRIMMED = "[older web result trimmed to save space]"
TRIMMED_FILE = "[older file excerpt trimmed to save space; call open_file again if needed]"
DENIED = "refused by the user"
KINDS = {"web_search": ("search", "query"), "read_page": ("page", "url"), "view_image": ("image", "url"),
         "open_file": ("file", "name")}


def text_of(m: dict) -> str:
    c = m.get("content")
    parts = [c] if isinstance(c, str) else [p.get("text", "") for p in c or [] if p.get("type") == "text"]
    parts += [tc["function"]["arguments"] for tc in m.get("tool_calls") or []]
    return "\n".join(parts)


def _images(m: dict) -> int:
    c = m.get("content")
    return 0 if isinstance(c, str) else sum(p.get("type") == "image_url" for p in c or [])


async def _trim(msgs: list[dict], count, budget: int) -> None:
    """Oldest tool results first; the newest round's results are never trimmed."""
    sizes = [await count(text_of(m)) + _images(m) * IMAGE_TOKENS for m in msgs]
    total = sum(sizes)
    names = {tc.get("id"): tc["function"].get("name") for m in msgs for tc in m.get("tool_calls") or []}
    newest = max((i for i, m in enumerate(msgs) if m.get("tool_calls")), default=len(msgs))
    for i, m in enumerate(msgs[:newest]):
        if total <= budget:
            return
        if m.get("role") == "tool" and m.get("content") not in (TRIMMED, TRIMMED_FILE):
            mark = TRIMMED_FILE if names.get(m.get("tool_call_id")) == "open_file" else TRIMMED
            msgs[i] = {**m, "content": mark}
            new = await count(mark)
            total += new - sizes[i]
            sizes[i] = new


def _tool_content(outcome):
    img = outcome.image
    if not img:
        return outcome.text
    data = img["data"]  # web.pages.fetch_image gives base64 text; accept raw bytes too
    if isinstance(data, (bytes, bytearray)):
        data = base64.b64encode(data).decode()
    return [{"type": "text", "text": outcome.text},
            {"type": "image_url", "image_url": {"url": f"data:{img['mime']};base64,{data}"}}]


async def run_turn(messages: list[dict], *, llm, session: WebSession | None, mode: str,
                   approve: Callable[[dict], Awaitable[dict]], count: Callable[[str], Awaitable[int]],
                   budget: int, max_rounds: int = 6, files: FileTools | None = None) -> AsyncIterator[tuple[str, object]]:
    msgs = list(messages)
    web_on = mode != "off" and session is not None
    answer_ok: bool | None = None  # ask_once verdict for this answer
    steps, sources, seen_src = [], [], set()
    content = ""
    for rnd in range(1, max_rounds + 1):
        await _trim(msgs, count, budget)
        tools = ((TOOL_SPECS if web_on else []) + ([files.spec] if files else [])) or None
        if rnd >= max_rounds:
            tools = None
        content, calls = "", []
        async for kind, val in llm(msgs, tools=tools):
            if kind == "tool_calls":
                calls = val
                continue
            if kind == "content":
                content += val
            yield kind, val
        if not tools or not calls:
            break
        msgs.append({"role": "assistant", "content": content, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or ""}}
            for c in calls]})
        for c in calls:
            kind, key = KINDS.get(c["name"], ("search", "query"))
            try:
                args = json.loads(c["arguments"] or "{}")
            except ValueError:
                args = None
            if c["name"] == "open_file" and files is not None:
                name = args.get("name") if isinstance(args, dict) else ""
                yield "web", {"kind": "file", "name": name, "status": "running"}
                outcome = await files.run(args)
                step, text = outcome.step, outcome.text
            elif not isinstance(args, dict) or c["name"] not in KINDS or not web_on or c["name"] == "open_file":
                step = {"kind": kind, key: "", "status": "error", "detail": INVALID}
                text = INVALID
            else:
                allow = True
                if mode == "ask_once":
                    if answer_ok is None:
                        answer_ok = bool((await approve({"kind": "answer"})).get("allow"))
                    allow = answer_ok
                elif mode == "ask_each" and c["name"] in KINDS:
                    verdict = await approve({"kind": kind, key: args.get(key)})
                    allow = bool(verdict.get("allow"))
                    if allow and kind == "search" and verdict.get("query"):
                        args = {**args, "query": verdict["query"]}
                if not allow:
                    step = {"kind": kind, key: args.get(key), "status": "denied", "detail": ""}
                    text = DENIED
                else:
                    yield "web", {"kind": kind, key: args.get(key), "status": "running"}
                    outcome = await session.run(c["name"], args)
                    step, text = outcome.step, _tool_content(outcome)
                    src = outcome.source
                    if src and src["url"] not in seen_src:
                        seen_src.add(src["url"])
                        sources.append(src)
            steps.append(step)
            yield "web", step
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": text})
        if answer_ok is False:
            web_on = False
    yield "final", {"content": content, "steps": steps, "sources": sources,
                    "new_urls": sorted(session.new_urls) if session is not None else []}
