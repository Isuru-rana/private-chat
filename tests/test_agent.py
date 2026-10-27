import asyncio
import base64
import json

from privatechat.agent import TRIMMED, _trim, run_turn
from privatechat.context import IMAGE_TOKENS
from privatechat.web.tools import TOOL_SPECS, ToolOutcome


def call(cid, name, **args):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


class FakeLLM:
    """Scripted per round: each round is a list of events. Records what it received."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.seen = []  # (messages snapshot, tools)

    def __call__(self, messages, tools=None):
        self.seen.append(([dict(m) for m in messages], tools))
        events = self.rounds.pop(0)

        async def gen():
            for e in events:
                yield e
        return gen()


class FakeSession:
    def __init__(self, outcomes=None, new_urls=()):
        self.calls = []
        self.outcomes = outcomes or {}
        self.new_urls = set(new_urls)
        self.images_used = 0

    async def run(self, name, args):
        self.calls.append((name, args))
        kind, key = {"web_search": ("search", "query"), "read_page": ("page", "url"),
                     "view_image": ("image", "url")}[name]
        val = args[key]
        if val in self.outcomes:
            return self.outcomes[val]
        return ToolOutcome(f"result for {val}", None,
                           {"kind": kind, key: val, "status": "done", "detail": ""}, None)


class Approver:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    async def __call__(self, req):
        self.requests.append(req)
        return self.answers.pop(0) if self.answers else {"allow": True, "query": None}


async def count_chars(text):
    return len(text)


def drive(msgs, llm, session, mode, approve=None, budget=10**9, max_rounds=6):
    async def go():
        return [e async for e in run_turn(msgs, llm=llm, session=session, mode=mode,
                                          approve=approve or Approver(), count=count_chars,
                                          budget=budget, max_rounds=max_rounds)]
    return asyncio.run(go())


USER = [{"role": "user", "content": "question?"}]
SEARCH_ROUND = [("reasoning", "think"), ("content", "let me look"),
                ("tool_calls", [call("c1", "web_search", query="q1")])]
ANSWER_ROUND = [("content", "the "), ("content", "answer")]


def final(events):
    assert events[-1][0] == "final"
    return events[-1][1]


def test_no_tools_when_off():
    for mode, session in [("off", FakeSession()), ("auto", None)]:
        llm = FakeLLM(ANSWER_ROUND)
        ev = drive(USER, llm, session, mode)
        assert len(llm.seen) == 1 and llm.seen[0][1] is None
        assert ev == [("content", "the "), ("content", "answer"),
                      ("final", {"content": "the answer", "steps": [], "sources": [], "new_urls": []})]


def test_search_then_answer():
    llm, s = FakeLLM(SEARCH_ROUND, ANSWER_ROUND), FakeSession()
    ev = drive(USER, llm, s, "auto")
    assert s.calls == [("web_search", {"query": "q1"})]
    assert llm.seen[0][1] == TOOL_SPECS
    assert ev[:2] == [("reasoning", "think"), ("content", "let me look")]
    webs = [e[1] for e in ev if e[0] == "web"]
    assert webs == [{"kind": "search", "query": "q1", "status": "running"},
                    {"kind": "search", "query": "q1", "status": "done", "detail": ""}]
    msgs2 = llm.seen[1][0]
    assert msgs2[0] == USER[0]
    assert msgs2[1] == {"role": "assistant", "content": "let me look", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "q1"}'}}]}
    assert msgs2[2] == {"role": "tool", "tool_call_id": "c1", "content": "result for q1"}
    assert final(ev)["content"] == "the answer"
    assert USER == [{"role": "user", "content": "question?"}]  # caller's list untouched


def test_ask_once_single_approval():
    r1 = [("tool_calls", [call("a", "web_search", query="q1"), call("b", "read_page", url="https://x/")])]
    r2 = [("tool_calls", [call("c", "web_search", query="q2")])]
    llm, s, ap = FakeLLM(r1, r2, ANSWER_ROUND), FakeSession(), Approver({"allow": True, "query": None})
    drive(USER, llm, s, "ask_once", ap)
    assert ap.requests == [{"kind": "answer"}]
    assert [c[0] for c in s.calls] == ["web_search", "read_page", "web_search"]


def test_ask_once_denied_refuses_all():
    r1 = [("tool_calls", [call("a", "web_search", query="q1"), call("b", "read_page", url="https://x/")])]
    llm, s, ap = FakeLLM(r1, ANSWER_ROUND), FakeSession(), Approver({"allow": False, "query": None})
    ev = drive(USER, llm, s, "ask_once", ap)
    assert ap.requests == [{"kind": "answer"}] and s.calls == []
    tool_msgs = [m for m in llm.seen[1][0] if m["role"] == "tool"]
    assert [m["content"] for m in tool_msgs] == ["refused by the user"] * 2
    assert llm.seen[1][1] is None  # later rounds send no tools
    steps = final(ev)["steps"]
    assert [(st["kind"], st["status"]) for st in steps] == [("search", "denied"), ("page", "denied")]
    assert steps[1]["url"] == "https://x/"


def test_ask_each_edited_query_used():
    r1 = [("tool_calls", [call("a", "web_search", query="q1"), call("b", "read_page", url="https://x/"),
                          call("c", "view_image", url="https://i/1.png")])]
    ap = Approver({"allow": True, "query": "edited"}, {"allow": False, "query": None},
                  {"allow": True, "query": None})
    llm, s = FakeLLM(r1, ANSWER_ROUND), FakeSession()
    ev = drive(USER, llm, s, "ask_each", ap)
    assert ap.requests == [{"kind": "search", "query": "q1"}, {"kind": "page", "url": "https://x/"},
                           {"kind": "image", "url": "https://i/1.png"}]
    assert s.calls == [("web_search", {"query": "edited"}), ("view_image", {"url": "https://i/1.png"})]
    webs = [e[1] for e in ev if e[0] == "web"]
    assert webs[0] == {"kind": "search", "query": "edited", "status": "running"}
    tool_msgs = [m for m in llm.seen[1][0] if m["role"] == "tool"]
    assert tool_msgs[1]["content"] == "refused by the user"
    assert llm.seen[1][1] == TOOL_SPECS  # loop continues with tools
    assert [st["status"] for st in final(ev)["steps"]] == ["done", "denied", "done"]


def test_auto_never_asks():
    r1 = [("tool_calls", [call("a", "web_search", query="q1")])]
    ap = Approver()
    llm, s = FakeLLM(r1, r1, ANSWER_ROUND), FakeSession()
    drive(USER, llm, s, "auto", ap)
    assert ap.requests == [] and len(s.calls) == 2


def test_last_round_has_no_tools():
    r = [("content", "x"), ("tool_calls", [call("a", "web_search", query="q")])]
    llm, s = FakeLLM(r, r, r), FakeSession()
    ev = drive(USER, llm, s, "auto", max_rounds=3)
    assert [t for _, t in llm.seen] == [TOOL_SPECS, TOOL_SPECS, None]
    assert len(s.calls) == 2  # stray tool calls in the no-tools round are ignored
    assert final(ev)["content"] == "x"


def test_bad_tool_call_reported_to_model():
    r1 = [("tool_calls", [{"id": "a", "name": "web_search", "arguments": "{not json"},
                          {"id": "b", "name": "read_page", "arguments": "[1]"}])]
    llm, s = FakeLLM(r1, ANSWER_ROUND), FakeSession()
    ev = drive(USER, llm, s, "auto")
    assert s.calls == []
    tool_msgs = [m for m in llm.seen[1][0] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_msgs] == [("a", "invalid tool call"),
                                                                        ("b", "invalid tool call")]
    steps = final(ev)["steps"]
    assert [(st["kind"], st["status"]) for st in steps] == [("search", "error"), ("page", "error")]


def test_image_delivered_to_model():
    img = {"kind": "image", "name": "n.png", "mime": "image/png", "data": b"\x89PNG"}
    out = ToolOutcome("image loaded", img, {"kind": "image", "url": "https://i/1.png", "status": "done",
                                            "detail": ""}, None)
    r1 = [("tool_calls", [call("a", "view_image", url="https://i/1.png")])]
    llm, s = FakeLLM(r1, ANSWER_ROUND), FakeSession({"https://i/1.png": out})
    drive(USER, llm, s, "auto")
    msgs = llm.seen[1][0]
    assert len(msgs) == 3 and msgs[-1] == {"role": "tool", "tool_call_id": "a", "content": [
        {"type": "text", "text": "image loaded"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()}}]}


def test_final_collects_steps_sources_urls():
    src = {"url": "https://p/1", "title": "P", "text": "body"}
    page = ToolOutcome("page text", None, {"kind": "page", "url": "https://p/1", "status": "done", "detail": ""}, src)
    r1 = [("content", "first"), ("tool_calls", [call("a", "web_search", query="q"),
                                                call("b", "read_page", url="https://p/1")])]
    r2 = [("tool_calls", [call("c", "read_page", url="https://p/1")])]
    llm = FakeLLM(r1, r2, [("content", "done")])
    s = FakeSession({"https://p/1": page}, new_urls={"https://z/", "https://a/"})
    ev = drive(USER, llm, s, "auto")
    f = final(ev)
    assert f["content"] == "done"
    assert [st.get("query") or st.get("url") for st in f["steps"]] == ["q", "https://p/1", "https://p/1"]
    assert all(st["status"] != "running" for st in f["steps"])
    assert f["sources"] == [src]
    assert f["new_urls"] == ["https://a/", "https://z/"]


def test_old_tool_results_trimmed_over_budget():
    img = {"kind": "image", "name": "n.png", "mime": "image/png", "data": b"x"}
    big = ToolOutcome("A" * 100, img, {"kind": "image", "url": "u1", "status": "done", "detail": ""}, None)
    r1 = [("tool_calls", [call("a", "view_image", url="u1")])]
    r2 = [("tool_calls", [call("b", "web_search", query="q2")])]
    llm = FakeLLM(r1, r2, ANSWER_ROUND)
    s = FakeSession({"u1": big, "q2": ToolOutcome("B" * 100, None, {"kind": "search", "query": "q2",
                                                                    "status": "done", "detail": ""}, None)})
    # budget fits user + both assistant msgs + one 100-char result, not two
    base = len("question?") + 2 * len('{"query": "q2"}')
    drive(USER, llm, s, "auto", budget=base + 150 + IMAGE_TOKENS)
    round2 = llm.seen[1][0]
    assert round2[2]["content"][0]["text"] == "A" * 100  # fits in round 2: untouched
    round3 = llm.seen[2][0]
    assert round3[2] == {"role": "tool", "tool_call_id": "a", "content": TRIMMED}
    assert round3[4]["content"] == "B" * 100  # newest kept
    assert round3[0] == USER[0] and round3[1]["role"] == "assistant"


def test_trim_counts_image_tokens():
    img_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
    msgs = [{"role": "user", "content": "q"},
            {"role": "tool", "tool_call_id": "a", "content": [{"type": "text", "text": "img"}, img_part]}]
    asyncio.run(_trim(msgs, count_chars, budget=100))  # 4 chars of text, but one image (~300 tokens)
    assert msgs[1]["content"] == TRIMMED
    keep = [dict(m) for m in msgs[:1]] + [{"role": "tool", "tool_call_id": "b", "content": "short"}]
    asyncio.run(_trim(keep, count_chars, budget=100))
    assert keep[1]["content"] == "short"


def test_null_tool_arguments_stored_as_empty_string():
    class S(FakeSession):
        async def run(self, name, args):
            self.calls.append((name, args))
            return ToolOutcome("invalid tool call", None,
                               {"kind": "search", "query": "", "status": "error", "detail": ""}, None)

    r1 = [("tool_calls", [{"id": "a", "name": "web_search", "arguments": None}])]
    llm = FakeLLM(r1, ANSWER_ROUND)
    ev = drive(USER, llm, S(), "auto")
    assert llm.seen[1][0][1]["tool_calls"][0]["function"]["arguments"] == ""
    assert final(ev)["content"] == "the answer"


def test_image_data_already_base64_string():
    # web.pages.fetch_image returns base64 text, not bytes
    img = {"kind": "image", "name": "n.png", "mime": "image/png", "data": "iVBORw=="}
    out = ToolOutcome("image loaded", img, {"kind": "image", "url": "https://i/1.png", "status": "done",
                                            "detail": ""}, None)
    llm = FakeLLM([("tool_calls", [call("a", "view_image", url="https://i/1.png")])], ANSWER_ROUND)
    drive(USER, llm, FakeSession({"https://i/1.png": out}), "auto")
    part = llm.seen[1][0][2]["content"][1]
    assert part == {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw=="}}
