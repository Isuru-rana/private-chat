"""Web access through the API: per-chat modes, web turns, approvals (fakes only, no network)."""
import json
import logging
import threading
import time

import pytest

from privatechat import app as app_module
from privatechat.web.tools import TOOL_SPECS, ToolOutcome
from test_api import PW, Clock, all_bytes, events, make_client, unlocked


def call(cid, name, **args):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


class ScriptLLM:
    """llm(messages, tools=None) scripted per round; records (messages, tools)."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.seen = []

    def __call__(self, messages, tools=None):
        self.seen.append((messages, tools))
        evs = self.rounds.pop(0) if self.rounds else [("content", "ok")]

        async def gen():
            for e in evs:
                yield e
        return gen()


class FakeClient:
    closed = False

    async def aclose(self):
        self.closed = True


class FakeWeb:
    def __init__(self, chat_id, allowed, outcomes, new_urls):
        self.chat_id, self.allowed = chat_id, set(allowed)
        self.outcomes, self.new_urls = outcomes, set(new_urls)
        self.client = FakeClient()
        self.calls = []

    async def run(self, name, args):
        self.calls.append((name, args))
        kind, key = {"web_search": ("search", "query"), "read_page": ("page", "url"),
                     "view_image": ("image", "url")}[name]
        val = args[key]
        if val in self.outcomes:
            return self.outcomes[val]
        return ToolOutcome(f"result for {val}", None, {"kind": kind, key: val, "status": "done", "detail": ""}, None)


class Factory:
    def __init__(self, outcomes=None, new_urls=()):
        self.outcomes, self.new_urls = outcomes or {}, new_urls
        self.made = []

    def __call__(self, chat_id, allowed):
        s = FakeWeb(chat_id, allowed, self.outcomes, self.new_urls)
        self.made.append(s)
        return s


async def tor_up():
    return True


async def tor_down():
    return False


def web_client(tmp_path, llm, factory, tor=tor_up):
    return make_client(tmp_path, Clock(), llm=llm, web_session_factory=factory, tor_check=tor)


def in_thread(c, url, body, headers=None):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", c.post(url, json=body, headers=headers)))
    t.start()
    return t, out


def wait_pending(c, n=1):
    end = time.time() + 5
    while time.time() < end:
        if len(c.app.state.approvals) >= n:
            return list(c.app.state.approvals)[-1]
        time.sleep(0.01)
    raise AssertionError("no approval appeared")


SEARCH = [("content", "looking"), ("tool_calls", [call("c1", "web_search", query="q1")])]
ANSWER = [("content", "the answer")]


def test_web_mode_set_and_defaults(tmp_path):
    with web_client(tmp_path, ScriptLLM(), Factory()) as c:
        chat = c.post("/api/chats", json={"title": "n"}).json()
        assert chat["web_mode"] == "ask_once"
        assert c.get(f"/api/chats/{chat['id']}").json()["web_mode"] == "ask_once"
        assert c.post("/api/chats", json={"title": "a", "web_mode": "auto"}).json()["web_mode"] == "auto"
        assert c.post("/api/chats", json={"title": "b", "web_mode": "nope"}).status_code == 400
        r = c.put(f"/api/chats/{chat['id']}/web", json={"mode": "ask_each"})
        assert r.status_code == 200 and r.json()["web_mode"] == "ask_each" and r.json()["id"] == chat["id"]
        assert c.get(f"/api/chats/{chat['id']}").json()["web_mode"] == "ask_each"
        assert c.put(f"/api/chats/{chat['id']}/web", json={"mode": "bad"}).status_code == 400
        assert c.put("/api/chats/missing/web", json={"mode": "auto"}).status_code == 404
        h = {"X-Vault-Token": unlocked(c)}
        sc = c.post("/api/secret/chats", json={"title": "s"}, headers=h).json()
        assert sc["web_mode"] == "off"
        assert c.put(f"/api/secret/chats/{sc['id']}/web", json={"mode": "auto"}).status_code == 401
        r = c.put(f"/api/secret/chats/{sc['id']}/web", json={"mode": "auto"}, headers=h)
        assert r.json()["web_mode"] == "auto"
        assert c.get(f"/api/secret/chats/{sc['id']}", headers=h).json()["web_mode"] == "auto"
        assert c.put("/api/secret/chats/missing/web", json={"mode": "auto"}, headers=h).status_code == 404


def test_off_mode_never_touches_web(tmp_path):
    llm, f = ScriptLLM(ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "off"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "hi"}))
        assert ev == [("content", "the answer"), ("done", True)]
        assert f.made == [] and llm.seen[0][1] is None
        assert "meta" not in c.get(f"/api/chats/{chat['id']}").json()["messages"][1]


def test_auto_web_turn_saves_meta(tmp_path):
    src = {"url": "https://p.example/1", "title": "P1", "text": "page text"}
    page = ToolOutcome("P1 text", None, {"kind": "page", "url": "https://p.example/1", "status": "done",
                                         "detail": ""}, src)
    llm = ScriptLLM(SEARCH, [("tool_calls", [call("c2", "read_page", url="https://p.example/1")])], ANSWER)
    f = Factory({"https://p.example/1": page}, new_urls={"https://p.example/1", "https://q.example/"})
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "auto"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "find it"}))
        webs = [d for e, d in ev if e == "web"]
        assert webs == [
            {"kind": "search", "query": "q1", "status": "running"},
            {"kind": "search", "query": "q1", "status": "done", "detail": ""},
            {"kind": "page", "url": "https://p.example/1", "status": "running"},
            {"kind": "page", "url": "https://p.example/1", "status": "done", "detail": ""},
        ]
        assert not [e for e, _ in ev if e.startswith("approval")]
        assert ("content", "the answer") in ev and ev[-1] == ("done", True)
        assert llm.seen[0][1] == TOOL_SPECS
        assert f.made[0].chat_id == chat["id"] and f.made[0].client.closed
        msg = c.get(f"/api/chats/{chat['id']}").json()["messages"][1]
        assert msg["content"] == "the answer"  # the final round, not the "looking" preamble
        assert msg["meta"] == {
            "web": {"steps": [webs[1], webs[3]], "sources": [src]},
            "urls": ["https://p.example/1", "https://q.example/"],
        }


def test_web_turn_empty_answer_fallback(tmp_path):
    llm = ScriptLLM(SEARCH, [])
    with web_client(tmp_path, llm, Factory()) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "auto"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "x"}))
        fallback = "I couldn't finish an answer — try again or rephrase."
        assert ("content", fallback) in ev
        assert c.get(f"/api/chats/{chat['id']}").json()["messages"][1]["content"] == fallback


def test_ask_once_approval_flow(tmp_path):
    llm, f = ScriptLLM(SEARCH, ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n"}).json()  # default ask_once
        assert c.post("/api/web/approve", json={"id": "nope", "allow": True}).status_code == 404
        t, out = in_thread(c, f"/api/chats/{chat['id']}/messages", {"content": "x"})
        aid = wait_pending(c)
        assert c.post("/api/web/approve", json={"id": aid, "allow": True}).status_code == 200
        t.join(5)
        ev = events(out["r"])
        names = [e for e, _ in ev]
        assert ("approval", {"id": aid, "kind": "answer"}) in ev
        assert names.index("approval") < names.index("approval_done") < names.index("web")
        assert ("approval_done", {"id": aid}) in ev
        assert f.made[0].calls == [("web_search", {"query": "q1"})]
        assert ev[-1] == ("done", True)
        assert c.app.state.approvals == {}
        assert c.post("/api/web/approve", json={"id": aid, "allow": True}).status_code == 404


def test_approval_timeout_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "APPROVAL_TIMEOUT", 0.1)
    llm, f = ScriptLLM(SEARCH, ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "ask_each"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "x"}))
        (aid,) = [d["id"] for e, d in ev if e == "approval"]
        assert ("approval", {"id": aid, "kind": "search", "query": "q1"}) in ev
        assert ("approval_done", {"id": aid}) in ev
        assert ("web", {"kind": "search", "query": "q1", "status": "denied", "detail": ""}) in ev
        assert f.made[0].calls == [] and c.app.state.approvals == {}
        assert ev[-1] == ("done", True)


def test_approval_timeout_ends_asking_for_this_answer(tmp_path, monkeypatch):
    # an abandoned laptop must not chain 300 s waits (each one keeping the vault alive)
    monkeypatch.setattr(app_module, "APPROVAL_TIMEOUT", 0.1)
    two = [("tool_calls", [call("c1", "web_search", query="q1"), call("c2", "web_search", query="q2")])]
    llm, f = ScriptLLM(two, ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "ask_each"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "x"}))
        assert [e for e, _ in ev].count("approval") == 1
        assert ("web", {"kind": "search", "query": "q2", "status": "denied", "detail": ""}) in ev
        assert f.made[0].calls == [] and ev[-1] == ("done", True)


def test_secret_approval_needs_token(tmp_path):
    llm, f = ScriptLLM(SEARCH, ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "ask_each"}, headers=h).json()
        t, out = in_thread(c, f"/api/secret/chats/{chat['id']}/messages", {"content": "x"}, h)
        aid = wait_pending(c)
        assert c.post("/api/web/approve", json={"id": aid, "allow": True}).status_code == 401
        bad = {"X-Vault-Token": "wrong"}
        assert c.post("/api/web/approve", json={"id": aid, "allow": True}, headers=bad).status_code == 401
        r = c.post("/api/web/approve", json={"id": aid, "allow": True, "query": "edited"}, headers=h)
        assert r.status_code == 200
        t.join(5)
        assert events(out["r"])[-1] == ("done", True)
        assert f.made[0].calls == [("web_search", {"query": "edited"})]


def test_secret_lock_during_web_turn(tmp_path):
    llm, f = ScriptLLM(SEARCH, ANSWER), Factory()
    with web_client(tmp_path, llm, f) as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "ask_once"}, headers=h).json()
        t, out = in_thread(c, f"/api/secret/chats/{chat['id']}/messages", {"content": "x"}, h)
        aid = wait_pending(c)
        c.post("/api/vault/lock")
        t.join(5)
        assert not t.is_alive()
        ev = events(out["r"])
        assert ("approval_done", {"id": aid}) in ev
        assert ev[-1] == ("error", "vault locked")
        assert f.made[0].calls == [] and f.made[0].client.closed
        assert c.app.state.approvals == {}
        h = {"X-Vault-Token": c.post("/api/vault/unlock", json={"password": PW}).json()["token"]}
        msgs = c.get(f"/api/secret/chats/{chat['id']}", headers=h).json()["messages"]
        assert [m["role"] for m in msgs] == ["user"]


def test_tor_down_answers_without_tools(tmp_path):
    llm, f = ScriptLLM(ANSWER), Factory()
    with web_client(tmp_path, llm, f, tor=tor_down) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "auto"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "x"}))
        step = {"kind": "search", "status": "error", "detail": "Tor isn't running"}
        assert ev == [("web", step), ("content", "the answer"), ("done", True)]
        assert f.made == [] and llm.seen[0][1] is None
        msg = c.get(f"/api/chats/{chat['id']}").json()["messages"][1]
        assert msg["content"] == "the answer"
        assert msg["meta"] == {"web": {"steps": [step], "sources": []}, "urls": []}


def test_allowed_urls_from_history_and_user_text(tmp_path):
    llm = ScriptLLM(SEARCH, ANSWER, ANSWER)
    f = Factory(new_urls={"https://found.example/a"})
    with web_client(tmp_path, llm, f) as c:
        chat = c.post("/api/chats", json={"title": "n", "web_mode": "auto"}).json()
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": "start at https://First.example/p."})
        assert f.made[0].allowed == {"https://first.example/p"}
        c.post(f"/api/chats/{chat['id']}/messages",
               json={"content": "and HTTPS://B.Example/x#frag, also http://[bad and https://[::1"})
        assert f.made[1].allowed == {"https://first.example/p", "https://found.example/a", "https://b.example/x"}


def test_secret_web_turn_no_leak(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    url = "https://canary.example/CANARYURL"
    src = {"url": url, "title": "CANARYTITLE", "text": "CANARYPAGETEXT"}
    page = ToolOutcome("CANARYPAGETEXT", None, {"kind": "page", "url": url, "status": "done", "detail": ""}, src)
    llm = ScriptLLM([("tool_calls", [call("a", "web_search", query="CANARYQUERY")])],
                    [("tool_calls", [call("b", "read_page", url=url)])], [("content", "CANARYANSWER")])
    f = Factory({url: page, "CANARYQUERY": ToolOutcome(
        "CANARYRESULT " + url, None, {"kind": "search", "query": "CANARYQUERY", "status": "done", "detail": ""},
        None)}, new_urls={url})
    with web_client(tmp_path, llm, f) as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "auto"}, headers=h).json()
        ev = events(c.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h))
        assert ev[-1] == ("done", True)
        msg = c.get(f"/api/secret/chats/{chat['id']}", headers=h).json()["messages"][1]
        assert msg["meta"]["web"]["sources"] == [src]
        c.post("/api/vault/lock")
    blob = all_bytes(tmp_path)
    for canary in ("CANARYQUERY", "CANARYURL", "CANARYPAGETEXT", "CANARYTITLE", "CANARYRESULT", "CANARYANSWER"):
        assert canary.encode() not in blob
        assert canary not in caplog.text


def clocked_client(tmp_path, llm, factory=None):
    clock = Clock()
    return clock, make_client(tmp_path, clock, llm=llm, web_session_factory=factory or Factory(), tor_check=tor_up)


def test_secret_reply_keeps_vault_alive_past_limit(tmp_path):
    clock = None

    def llm(messages, tools=None):
        async def gen():
            for part in ("a", "b", "c"):
                clock.t += 550  # 1650 s total, never 600 s between events
                yield "content", part
        return gen()

    clock, cm = clocked_client(tmp_path, llm)
    with cm as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "off"}, headers=h).json()
        ev = events(c.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h))
        assert ev[-1] == ("done", True)
        assert c.get("/api/vault").json()["unlocked"]
        assert c.get(f"/api/secret/chats/{chat['id']}", headers=h).json()["messages"][-1]["content"] == "abc"


def test_secret_web_turn_keeps_vault_alive_past_limit(tmp_path):
    clock = None
    rounds = [SEARCH, ANSWER]

    def llm(messages, tools=None):
        evs = rounds.pop(0)

        async def gen():
            for e in evs:
                clock.t += 550
                yield e
        return gen()

    clock, cm = clocked_client(tmp_path, llm)
    with cm as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "auto"}, headers=h).json()
        ev = events(c.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h))
        assert ev[-1] == ("done", True)
        assert c.get(f"/api/secret/chats/{chat['id']}", headers=h).json()["messages"][-1]["content"] == "the answer"


def test_secret_reply_gap_over_limit_locks(tmp_path):
    clock = None

    def llm(messages, tools=None):
        async def gen():
            yield "content", "a"
            clock.t += 700  # one gap past the idle limit
            yield "content", "b"
        return gen()

    clock, cm = clocked_client(tmp_path, llm)
    with cm as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "off"}, headers=h).json()
        ev = events(c.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h))
        assert ev[-1] == ("error", "vault locked")


def test_unexpected_error_sends_generic_event_and_no_log_text(tmp_path, caplog):
    def llm(messages, tools=None):
        async def gen():
            yield "content", "a"
            raise RuntimeError("https://secret.example/?q=hidden-query")
        return gen()

    clock, cm = clocked_client(tmp_path, llm)
    with caplog.at_level(logging.DEBUG), cm as c:
        h = {"X-Vault-Token": unlocked(c)}
        chat = c.post("/api/secret/chats", json={"title": "s", "web_mode": "off"}, headers=h).json()
        r = c.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h)
        assert events(r)[-1] == ("error", "something went wrong")
        assert "hidden-query" not in r.text
    assert "hidden-query" not in caplog.text
