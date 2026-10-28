"""Full file reading through the API: parts pass, pinned notes, open_file (fakes only)."""
import base64
import json

from privatechat.llm import LLMError
from privatechat.store import NormalStore
from test_api import ATTS, PW, Clock, all_bytes, events, make_client, unlocked
from test_api_web import Factory, ScriptLLM, tor_down

CANARY = "zebra-canary-7731"


def upload(name, text):
    return {"name": name, "mime": "text/plain", "data": base64.b64encode(text.encode()).decode()}


def text_of_tokens(tokens, tag):
    """Text the offline counter (len//3+1) rates at about `tokens`."""
    line = f"{tag} {CANARY} lorem ipsum dolor sit amet\n"
    return (line * (tokens * 3 // len(line) + 1))[: tokens * 3]


class Notes:
    """summarizer fake: records calls, returns 'note i'; optional hook per call."""

    def __init__(self, fail_on=None, hook=None, filler=""):
        self.calls, self.fail_on, self.hook, self.filler = [], fail_on, hook, filler

    async def __call__(self, messages, max_tokens=None):
        self.calls.append((messages, max_tokens))
        i = len(self.calls)
        if self.hook:
            self.hook(i)
        if i == self.fail_on:
            raise LLMError("notes failed")
        return f"note {i}{self.filler}"


def big_files(n=3, tokens=8300):
    return [upload(f"f{i}.py", text_of_tokens(tokens, f"file{i}")) for i in range(1, n + 1)]


def client(tmp_path, llm=None, notes=None, clock=None, **kw):
    return make_client(tmp_path, clock or Clock(), llm=llm or ScriptLLM(), summarizer=notes or Notes(), **kw)


def off_chat(c, prefix="/api/chats", h=None):
    return c.post(prefix, json={"title": "f", "web_mode": "off"}, headers=h).json()["id"]


def send(c, cid, content="read these", files=(), prefix="/api/chats", h=None, **extra):
    return c.post(f"{prefix}/{cid}/messages", json={"content": content, "attachments": list(files), **extra},
                  headers=h)


def statuses(ev):
    return [d for k, d in ev if k == "status"]


def all_text(msgs):
    out = []
    for m in msgs:
        c = m.get("content")
        out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or []))
    return "\n".join(out)


def test_small_files_inline_no_pass(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        ev = events(send(c, cid, files=[upload("small.py", text_of_tokens(1000, "small"))]))
        assert ev[-1][0] == "done" and statuses(ev) == []
        assert notes.calls == []
        assert CANARY in all_text(llm.seen[0][0])


def test_big_files_read_in_parts(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        ev = events(send(c, cid, files=big_files()))
        assert ev[-1][0] == "done"
        assert statuses(ev) == [f"Reading part {k} of 3…" for k in (1, 2, 3)]
        assert [mt for _, mt in notes.calls] == [500, 500, 500]
        msgs = llm.seen[0][0]
        assert [m["role"] for m in msgs].count("system") == 1 and msgs[0]["role"] == "system"
        assert all(f"note {i}" in msgs[0]["content"] for i in (1, 2, 3))
        assert CANARY not in all_text(msgs)
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        assert NormalStore(tmp_path / "chats.db").notes(cid) == {uid: ["note 1", "note 2", "note 3"]}


def test_too_big_rejected_400_nothing_stored(tmp_path):
    notes = Notes()
    with client(tmp_path, notes=notes) as c:
        cid = off_chat(c)
        r = send(c, cid, files=big_files(7, 8500))
        assert r.status_code == 400
        assert r.json()["detail"] == "These files need 7 parts to read; the limit is 5. Remove some files."
        assert c.get(f"/api/chats/{cid}").json()["messages"] == []
        assert notes.calls == []


def test_followup_reuses_notes(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        ev = events(send(c, cid, content="and then?"))
        assert ev[-1][0] == "done" and statuses(ev) == []
        assert len(notes.calls) == 3
        msgs = llm.seen[-1][0]
        assert msgs[0]["role"] == "system" and "note 3" in msgs[0]["content"]
        assert CANARY not in all_text(msgs)


def test_regenerate_reuses_notes(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        ev = events(c.post(f"/api/chats/{cid}/regenerate", json={"parent": uid}))
        assert ev[-1][0] == "done" and len(notes.calls) == 3
        assert "note 1" in llm.seen[-1][0][0]["content"]


def test_interrupted_pass_rereads(tmp_path):
    llm, notes = ScriptLLM(), Notes(fail_on=2)
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        ev = events(send(c, cid, files=big_files()))
        assert ev[-1] == ("error", "notes failed") and len(notes.calls) == 2
        assert NormalStore(tmp_path / "chats.db").notes(cid) == {}
        msgs = c.get(f"/api/chats/{cid}").json()["messages"]
        assert [m["role"] for m in msgs] == ["user"] and llm.seen == []
        ev = events(c.post(f"/api/chats/{cid}/regenerate", json={"parent": msgs[0]["id"]}))
        assert ev[-1][0] == "done" and len(notes.calls) == 5
        assert "part 1 of 3" in notes.calls[2][0][0]["content"]
        assert NormalStore(tmp_path / "chats.db").notes(cid) == {msgs[0]["id"]: ["note 3", "note 4", "note 5"]}


def test_edit_text_only_keeps_notes(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        ev = events(send(c, cid, content="reworded", edit_of=uid))
        assert ev[-1][0] == "done" and statuses(ev) == [] and len(notes.calls) == 3
        new = c.get(f"/api/chats/{cid}").json()["messages"][0]
        assert new["id"] != uid and new["content"] == "reworded"
        assert NormalStore(tmp_path / "chats.db").notes(cid)[new["id"]] == ["note 1", "note 2", "note 3"]
        assert "note 2" in llm.seen[-1][0][0]["content"]


def test_edit_with_new_file_rereads(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        ev = events(send(c, cid, content="one more", edit_of=uid, files=[upload("extra.py", "x = 1\n")]))
        assert ev[-1][0] == "done" and len(statuses(ev)) == 3 and len(notes.calls) == 6
        new = c.get(f"/api/chats/{cid}").json()["messages"][0]
        assert new["id"] != uid and len(new["attachments"]) == 4
        assert all(a["read"] == "parts" for a in new["attachments"])
        assert "extra.py" in notes.calls[-1][0][0]["content"]


def test_secret_vault_locks_mid_pass(tmp_path):
    clock = Clock()

    def lock_after_first(i):
        if i == 1:
            clock.t += 601

    notes = Notes(hook=lock_after_first)
    with client(tmp_path, notes=notes, clock=clock) as c:
        h = {"X-Vault-Token": unlocked(c)}
        cid = off_chat(c, "/api/secret/chats", h)
        ev = events(send(c, cid, files=big_files(), prefix="/api/secret/chats", h=h))
        assert ev[-1] == ("error", "vault locked") and len(notes.calls) == 1
        h = {"X-Vault-Token": c.post("/api/vault/unlock", json={"password": PW}).json()["token"]}
        assert c.app.state.vault.notes(cid) == {}
        assert c.get(f"/api/secret/chats/{cid}", headers=h).status_code == 200
        assert CANARY.encode() not in all_bytes(tmp_path)


def test_secret_pass_saves_in_vault_only(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        h = {"X-Vault-Token": unlocked(c)}
        cid = off_chat(c, "/api/secret/chats", h)
        ev = events(send(c, cid, files=big_files(), prefix="/api/secret/chats", h=h))
        assert ev[-1][0] == "done"
        assert list(c.app.state.vault.notes(cid).values()) == [["note 1", "note 2", "note 3"]]
        raw = all_bytes(tmp_path)
        assert CANARY.encode() not in raw and b"note 1" not in raw


def test_open_file_with_web_off(tmp_path):
    llm = ScriptLLM([("tool_calls", [{"id": "c1", "name": "open_file", "arguments": json.dumps({"name": "small.py"})}])], [("content", "it says hi")])
    factory = Factory()
    with client(tmp_path, llm, web_session_factory=factory) as c:
        cid = off_chat(c)
        ev = events(send(c, cid, files=[upload("small.py", f"print('{CANARY}')\n")]))
        assert ev[-1][0] == "done"
        tool_msg = llm.seen[1][0][-1]
        assert tool_msg["role"] == "tool" and CANARY in tool_msg["content"]
        assert [t["function"]["name"] for t in llm.seen[0][1]] == ["open_file"]
        msg = c.get(f"/api/chats/{cid}").json()["messages"][1]
        assert msg["content"] == "it says hi" and msg["meta"]["web"]["steps"][0]["kind"] == "file"
        assert factory.made == []


def test_tor_down_still_has_file_tools(tmp_path):
    llm, factory = ScriptLLM(), Factory()
    with client(tmp_path, llm, web_session_factory=factory, tor_check=tor_down) as c:
        cid = c.post("/api/chats", json={"title": "a", "web_mode": "auto"}).json()["id"]
        ev = events(send(c, cid, files=[upload("small.py", "x = 1\n")]))
        assert ev[-1][0] == "done" and ("web", app_tor_down()) in ev
        assert [t["function"]["name"] for t in llm.seen[0][1]] == ["open_file"]
        assert factory.made == []
        msg = c.get(f"/api/chats/{cid}").json()["messages"][1]
        assert msg["meta"]["web"]["steps"] == [app_tor_down()]


def app_tor_down():
    from privatechat.app import TOR_DOWN
    return TOR_DOWN


def test_send_after_failed_pass_rereads(tmp_path):
    llm, notes = ScriptLLM(), Notes(fail_on=2)
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        assert events(send(c, cid, files=big_files()))[-1][0] == "error"
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        ev = events(send(c, cid, content="and then?"))
        assert ev[-1][0] == "done" and statuses(ev) == [f"Reading part {k} of 3…" for k in (1, 2, 3)]
        assert len(notes.calls) == 5
        assert NormalStore(tmp_path / "chats.db").notes(cid) == {uid: ["note 3", "note 4", "note 5"]}
        msgs = llm.seen[-1][0]
        assert msgs[0]["role"] == "system" and "note 5" in msgs[0]["content"]
        assert CANARY not in all_text(msgs)
        # the next turn still pins them
        events(send(c, cid, content="more?"))
        assert len(notes.calls) == 5 and "note 4" in llm.seen[-1][0][0]["content"]


def test_edit_image_only_keeps_notes(tmp_path):
    notes = Notes()
    with client(tmp_path, notes=notes) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        uid = c.get(f"/api/chats/{cid}").json()["messages"][0]["id"]
        ev = events(send(c, cid, content="with a picture", edit_of=uid, files=ATTS[:1]))
        assert ev[-1][0] == "done" and statuses(ev) == [] and len(notes.calls) == 3
        new = c.get(f"/api/chats/{cid}").json()["messages"][0]
        assert NormalStore(tmp_path / "chats.db").notes(cid)[new["id"]] == ["note 1", "note 2", "note 3"]


def test_unknown_chat_404_before_counting(tmp_path):
    seen = []

    async def counter(text):
        seen.append(1)
        return len(text) // 3 + 1

    with client(tmp_path, counter=counter) as c:
        assert send(c, "nope", files=big_files(7, 8500)).status_code == 404
        assert seen == []


def test_pinned_notes_reduce_context_budget(tmp_path):
    llm, notes = ScriptLLM(), Notes(filler=" x" * 1500)
    with client(tmp_path, llm, notes, context_budget=1500) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        ev = events(send(c, cid, content="and then?"))
        ctx = [d for k, d in ev if k == "context"]
        assert ctx and ctx[0]["dropped"] == 2  # without the pin's cost, 3 short messages fit easily
        assert [m["role"] for m in llm.seen[-1][0]][-1] == "user"


def test_summary_and_pin_share_one_system_message(tmp_path):
    llm, notes = ScriptLLM(), Notes(filler=" x" * 1500)
    with client(tmp_path, llm, notes, context_budget=1500) as c:
        cid = off_chat(c)
        send(c, cid, files=big_files())
        ev = events(send(c, cid, content="and then?"))
        assert ("status", "Summarizing earlier messages…") in ev
        msgs = llm.seen[-1][0]
        assert [m["role"] for m in msgs].count("system") == 1 and msgs[0]["role"] == "system"
        assert "Notes from reading" in msgs[0]["content"] and "Summary of the earlier part" in msgs[0]["content"]


def test_open_file_text_reaches_model_in_long_summarized_chat(tmp_path):
    llm, notes = ScriptLLM(), Notes()
    with client(tmp_path, llm, notes) as c:
        cid = off_chat(c)
        send(c, cid, files=[upload("big.py", text_of_tokens(8300, "big"))])
        for i in range(4):  # ~4000 tokens each: older turns drop out and get summarized
            send(c, cid, content=f"chatter {i} " + "lorem ipsum " * 1000)
        assert NormalStore(tmp_path / "chats.db").summaries(cid)
        llm.rounds = [[("tool_calls", [{"id": "o1", "name": "open_file",
                                        "arguments": json.dumps({"name": "big.py"})}])],
                      [("content", "found it")]]
        ev = events(send(c, cid, content="show me the start of big.py"))
        assert ev[-1][0] == "done"
        tool = [m for m in llm.seen[-1][0] if m["role"] == "tool"][-1]
        assert CANARY in tool["content"] and "trimmed" not in tool["content"]
        # history was planned with room kept for the excerpt: the whole round fits the budget
        from privatechat.agent import text_of
        assert sum(len(text_of(m)) // 3 + 1 for m in llm.seen[-1][0]) <= 16384 - 4000 - 300
