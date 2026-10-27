"""Streaming client for the local llama-server (OpenAI-compatible API)."""

import json
import os
from collections.abc import AsyncIterator

import httpx

LLAMA_URL = os.environ.get("PC_LLAMA", "http://127.0.0.1:8088/v1")
MODEL = "gemma4-26b"
MAX_TOKENS = 4000


class LLMError(Exception):
    pass


async def stream_chat(
    messages: list[dict],
    *,
    base_url: str = LLAMA_URL,
    client: httpx.AsyncClient | None = None,
    tools: list[dict] | None = None,
) -> AsyncIterator[tuple[str, object]]:
    body = {"model": MODEL, "messages": messages, "stream": True, "max_tokens": MAX_TOKENS}
    if tools:
        body["tools"] = tools
    calls: dict[int, dict] = {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0))
    try:
        async with client.stream("POST", f"{base_url}/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                raise LLMError(f"llama-server returned HTTP {resp.status_code}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    if calls:
                        yield "tool_calls", [calls[i] for i in sorted(calls)]
                    return
                try:
                    frame = json.loads(data)
                except ValueError:
                    raise LLMError("garbled reply from llama-server") from None
                if "error" in frame:
                    err = frame["error"]
                    raise LLMError(str(err.get("message", err) if isinstance(err, dict) else err))
                choices = frame.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    yield "reasoning", delta["reasoning_content"]
                if delta.get("content"):
                    yield "content", delta["content"]
                for tc in delta.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    c["id"] = tc.get("id") or c["id"]
                    c["name"] += fn.get("name") or ""
                    c["arguments"] += fn.get("arguments") or ""
        raise LLMError("reply stream ended early")
    except httpx.HTTPError as e:
        raise LLMError(f"cannot reach llama-server: {type(e).__name__}") from None
    finally:
        if own:
            await client.aclose()


async def slots_disabled(base_url: str = LLAMA_URL, *, client: httpx.AsyncClient | None = None) -> bool:
    root = base_url.removesuffix("/v1")
    own = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        return (await client.get(f"{root}/slots")).status_code != 200
    except httpx.HTTPError:
        return True
    finally:
        if own:
            await client.aclose()


async def count_tokens(text: str, base_url: str = LLAMA_URL, *, client: httpx.AsyncClient | None = None) -> int:
    """Exact token count from llama-server; a rough estimate if it can't be reached."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(f"{base_url.removesuffix('/v1')}/tokenize", json={"content": text})
        resp.raise_for_status()
        return len(resp.json()["tokens"])
    except (httpx.HTTPError, ValueError, KeyError):
        return -(-len(text) // 3)  # ~3 characters per token, rounded up
    finally:
        if own:
            await client.aclose()


async def complete(
    messages: list[dict],
    *,
    max_tokens: int,
    base_url: str = LLAMA_URL,
    client: httpx.AsyncClient | None = None,
) -> str:
    """One non-streamed answer with thinking switched off (used for summaries)."""
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0))
    try:
        resp = await client.post(f"{base_url}/chat/completions", json=body)
        if resp.status_code != 200:
            raise LLMError(f"llama-server returned HTTP {resp.status_code}")
        return (resp.json()["choices"][0]["message"].get("content") or "").strip()
    except httpx.HTTPError as e:
        raise LLMError(f"cannot reach llama-server: {type(e).__name__}") from None
    except (ValueError, KeyError, IndexError):
        raise LLMError("garbled reply from llama-server") from None
    finally:
        if own:
            await client.aclose()
