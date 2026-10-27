import asyncio
import json

import httpx
import pytest

from privatechat.llm import LLMError, slots_disabled, stream_chat

BASE = "http://llm.test/v1"


def sse(*deltas, done=True):
    lines = [f"data: {json.dumps({'choices': [{'delta': d}]})}\n\n" for d in deltas]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def collect(client):
    async def go():
        return [x async for x in stream_chat([{"role": "user", "content": "hi"}], base_url=BASE, client=client)]

    return asyncio.run(go())


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_parses_reasoning_and_content():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        body = sse({"reasoning_content": "think"}, {"content": "Hel"}, {"content": "lo"}, {})
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    out = collect(client_for(handler))
    assert out == [("reasoning", "think"), ("content", "Hel"), ("content", "lo")]
    assert seen["url"] == BASE + "/chat/completions"
    assert seen["body"]["model"] == "gemma4-26b"
    assert seen["body"]["stream"] is True
    assert seen["body"]["max_tokens"] >= 2000


def test_stream_cut_without_done_raises():
    def handler(req):
        return httpx.Response(200, content=sse({"content": "x"}, done=False))

    with pytest.raises(LLMError):
        collect(client_for(handler))


def test_connection_error_raises_llmerror():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError):
        collect(client_for(handler))


def test_500_raises_llmerror():
    def handler(req):
        return httpx.Response(500, content=b"boom")

    with pytest.raises(LLMError):
        collect(client_for(handler))


def test_slots_disabled_true_on_501():
    def handler(req):
        assert str(req.url) == "http://llm.test/slots"
        return httpx.Response(501)

    assert asyncio.run(slots_disabled(BASE, client=client_for(handler))) is True


def test_slots_disabled_false_on_200():
    assert asyncio.run(slots_disabled(BASE, client=client_for(lambda r: httpx.Response(200, json=[])))) is False


def test_malformed_line_raises_llmerror():
    def handler(req):
        return httpx.Response(200, content=b"data: {not json\n\ndata: [DONE]\n\n")

    with pytest.raises(LLMError):
        collect(client_for(handler))


def test_error_frame_raises_llmerror():
    def handler(req):
        body = sse({"content": "part"}).replace(
            b"data: [DONE]", b'data: {"error": {"message": "context full"}}\n\ndata: [DONE]'
        )
        return httpx.Response(200, content=body)

    with pytest.raises(LLMError, match="context full"):
        collect(client_for(handler))


def test_count_tokens_uses_tokenize():
    from privatechat.llm import count_tokens

    def handler(req):
        assert str(req.url) == "http://llm.test/tokenize"
        assert json.loads(req.content) == {"content": "hello there"}
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    assert asyncio.run(count_tokens("hello there", BASE, client=client_for(handler))) == 3


def test_count_tokens_falls_back_to_estimate():
    from privatechat.llm import count_tokens

    def handler(req):
        raise httpx.ConnectError("down")

    assert asyncio.run(count_tokens("x" * 30, BASE, client=client_for(handler))) == 10


def test_complete_turns_thinking_off():
    from privatechat.llm import complete

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": " summary text "}}]})

    out = asyncio.run(complete([{"role": "user", "content": "x"}], max_tokens=500, base_url=BASE, client=client_for(handler)))
    assert out == "summary text"
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["body"]["stream"] is False and seen["body"]["max_tokens"] == 500


def test_complete_error_raises():
    from privatechat.llm import complete

    with pytest.raises(LLMError):
        asyncio.run(complete([], max_tokens=10, base_url=BASE, client=client_for(lambda r: httpx.Response(400, json={}))))


def _tc_stream(*deltas):
    def handler(req):
        handler.body = json.loads(req.content)
        return httpx.Response(200, content=sse(*deltas), headers={"content-type": "text/event-stream"})

    return handler


def test_tool_call_deltas_assembled():
    h = _tc_stream(
        {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "web_search", "arguments": '{"qu'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": 'ery":"x"}'}}]},
        {},
    )
    out = collect(client_for(h))
    assert out[-1] == ("tool_calls", [{"id": "c1", "name": "web_search", "arguments": '{"query":"x"}'}])
    assert sum(1 for k, _ in out if k == "tool_calls") == 1


def test_two_parallel_tool_calls():
    h = _tc_stream(
        {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "read_page", "arguments": "{}"}}]},
        {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "web_search", "arguments": "{}"}}]},
    )
    out = collect(client_for(h))
    assert out[-1] == (
        "tool_calls",
        [
            {"id": "a", "name": "web_search", "arguments": "{}"},
            {"id": "b", "name": "read_page", "arguments": "{}"},
        ],
    )


def test_tools_sent_only_when_given():
    h = _tc_stream({"content": "hi"})
    collect(client_for(h))
    assert "tools" not in h.body

    async def go():
        tools = [{"type": "function", "function": {"name": "t"}}]
        return [x async for x in stream_chat([], base_url=BASE, client=client_for(h), tools=tools)]

    asyncio.run(go())
    assert h.body["tools"] == [{"type": "function", "function": {"name": "t"}}]
