"""Agent tool loop: model rounds -> web tool calls -> results back -> final answer."""
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from .context import IMAGE_TOKENS
from .web.tools import INVALID, TOOL_SPECS, WebSession

TRIMMED = "[older web result trimmed to save space]"
DENIED = "refused by the user"
KINDS = {"web_search": ("search", "query"), "read_page": ("page", "url"), "view_image": ("image", "url")}


def text_of(m: dict) -> str:
    c = m.get("content")
    parts = [c] if isinstance(c, str) else [p.get("text", "") for p in c or [] if p.get("type") == "text"]
    parts += [tc["function"]["arguments"] for tc in m.get("tool_calls") or []]
    return "\n".join(parts)


def _images(m: dict) -> int:
    c = m.get("content")
    return 0 if isinstance(c, str) else sum(p.get("type") == "image_url" for p in c or [])


async def _trim(msgs: list[dict], count, budget: int) -> None:
    sizes = [await count(text_of(m)) + _images(m) * IMAGE_TOKENS for m in msgs]
    total = sum(sizes)
    for i, m in enumerate(msgs):
        if total <= budget:
            return
        if m.get("role") == "tool" and m.get("content") != TRIMMED:
            msgs[i] = {**m, "content": TRIMMED}
            new = await count(TRIMMED)
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
                   budget: int, max_rounds: int = 6) -> AsyncIterator[tuple[str, object]]:
    msgs = list(messages)
    use_tools = mode != "off" and session is not None
    answer_ok: bool | None = None  # ask_once verdict for this answer
    steps, sources, seen_src = [], [], set()
    content = ""
    for rnd in range(1, max_rounds + 1):
        await _trim(msgs, count, budget)
        tools = TOOL_SPECS if use_tools and rnd < max_rounds else None
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
            if not isinstance(args, dict):
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
            use_tools = False
    yield "final", {"content": content, "steps": steps, "sources": sources,
                    "new_urls": sorted(session.new_urls) if session is not None else []}
