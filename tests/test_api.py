from conftest import plain
import json
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient

from privatechat.app import create_app
from privatechat.crypto import FAST_KDF
from privatechat.llm import LLMError

PW = "a strong vault password"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


async def fake_llm(messages):
    yield "reasoning", "hm"
    yield "content", "Hi"


async def broken_llm(messages):
    yield "content", "partial"
    raise LLMError("cut")


async def slots_ok(*a, **k):
    return True


@pytest.fixture
def clock():
    return Clock()


async def _offline_counter(text):
    return len(text) // 3 + 1


def make_client(tmp_path, clock, llm=fake_llm, **kw):
    kw.setdefault("allowed_hosts", ["testserver"])
    async def _no_tor():
        return False

    kw.setdefault("tor_check", _no_tor)  # tests never depend on a running Tor
    kw.setdefault("counter", _offline_counter)  # never call a real llama-server from tests
    app = create_app(tmp_path, kdf=FAST_KDF, llm=llm, clock=clock, slots_check=slots_ok, **kw)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        yield c


def events(resp):
    out = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def all_bytes(root):
    out = b""
    for dirpath, _, files in os.walk(root):
        for f in files:
            with open(os.path.join(dirpath, f), "rb") as fh:
                out += fh.read()
    return out


def unlocked(client):
    return client.post("/api/vault/create", json={"password": PW}).json()["token"]


def test_normal_chat_stream_and_save(client):
    chat = client.post("/api/chats", json={"title": "hello"}).json()
    r = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "hey"})
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r)
    assert ("reasoning", "hm") in ev and ("content", "Hi") in ev and ev[-1][0] == "done"
    got = client.get(f"/api/chats/{chat['id']}").json()
    assert plain(got["messages"]) == [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "Hi"}]
    assert [c["id"] for c in client.get("/api/chats").json()] == [chat["id"]]
    assert client.delete(f"/api/chats/{chat['id']}").status_code == 200
    assert client.get(f"/api/chats/{chat['id']}").status_code == 404


def test_vault_create_unlock_lock_flow(client):
    assert client.get("/api/vault").json() == {"exists": False, "unlocked": False}
    assert client.post("/api/vault/unlock", json={"password": PW}).status_code == 404
    assert client.post("/api/vault/create", json={"password": "short"}).status_code == 400
    token = unlocked(client)
    assert client.get("/api/vault").json() == {"exists": True, "unlocked": True}
    assert client.post("/api/vault/create", json={"password": PW}).status_code == 400
    assert client.post("/api/vault/lock").status_code == 200
    assert client.get("/api/vault").json() == {"exists": True, "unlocked": False}
    token2 = client.post("/api/vault/unlock", json={"password": PW}).json()["token"]
    assert token2 and token2 != token


def test_secret_chat_flow(client):
    h = {"X-Vault-Token": unlocked(client)}
    chat = client.post("/api/secret/chats", json={"title": "s"}, headers=h).json()
    ev = events(client.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x"}, headers=h))
    assert ev[-1][0] == "done"
    assert client.get(f"/api/secret/chats/{chat['id']}", headers=h).json()["messages"][1]["content"] == "Hi"
    assert client.get("/api/chats").json() == []
    assert client.delete(f"/api/secret/chats/{chat['id']}", headers=h).status_code == 200
    assert client.get("/api/secret/chats", headers=h).json() == []


def test_secret_requires_token(client):
    token = unlocked(client)
    assert client.get("/api/secret/chats").status_code == 401
    assert client.get("/api/secret/chats", headers={"X-Vault-Token": "nope"}).status_code == 401
    client.post("/api/vault/lock")
    assert client.get("/api/secret/chats", headers={"X-Vault-Token": token}).status_code == 401


def test_wrong_password_saves_nothing(client, tmp_path):
    unlocked(client)
    client.post("/api/vault/lock")
    r = client.post("/api/vault/unlock", json={"password": "almost the password"})
    assert r.status_code == 401 and r.json() == {"error": "wrong_password"}
    assert client.get("/api/chats").json() == []
    assert b"almost the password" not in all_bytes(tmp_path)


def test_throttle_429(client):
    unlocked(client)
    client.post("/api/vault/lock")
    codes = [client.post("/api/vault/unlock", json={"password": "w" * 12}).status_code for _ in range(7)]
    assert codes == [401] * 6 + [429]
    r = client.post("/api/vault/unlock", json={"password": PW})
    assert r.status_code == 429 and r.json()["retry_after"] > 0


def test_idle_lock_via_api(client, clock):
    h = {"X-Vault-Token": unlocked(client)}
    clock.t += 601
    assert client.get("/api/secret/chats", headers=h).status_code == 401
    assert client.get("/api/vault").json()["unlocked"] is False


def test_background_idle_lock(tmp_path, clock):
    with make_client(tmp_path, clock, idle_poll=0.02) as c:
        unlocked(c)
        vault = c.app.state.vault
        clock.t += 601
        deadline = time.time() + 2
        while vault._key is not None and time.time() < deadline:
            time.sleep(0.02)
        assert vault._key is None


def test_llm_error_not_saved(tmp_path, clock):
    with make_client(tmp_path, clock, llm=broken_llm) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        ev = events(c.post(f"/api/chats/{chat['id']}/messages", json={"content": "q"}))
        assert ev[-1][0] == "error"
        assert plain(c.get(f"/api/chats/{chat['id']}").json()["messages"]) == [{"role": "user", "content": "q"}]


def test_no_store_header(client):
    assert client.get("/api/vault").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"


def test_leak_canary_e2e(client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = {"X-Vault-Token": unlocked(client)}
    chat = client.post("/api/secret/chats", json={"title": "TITLE-CANARY-E2E"}, headers=h).json()
    client.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "CANARY-E2E"}, headers=h)
    client.post("/api/vault/lock")
    data = all_bytes(tmp_path) + caplog.text.encode()
    for secret in (b"CANARY-E2E", b"TITLE-CANARY-E2E", PW.encode()):
        assert secret not in data


def test_foreign_host_rejected(tmp_path, clock):
    with make_client(tmp_path, clock, allowed_hosts=["127.0.0.1", "localhost"]) as c:
        assert c.get("/api/chats", headers={"Host": "evil.example"}).status_code == 400
        assert c.get("/api/chats", headers={"Host": "127.0.0.1:3050"}).status_code == 200


def test_cross_site_write_rejected(client):
    r = client.post("/api/chats", json={"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/api/chats", json={"title": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/chats").json() == []
    ok = client.post("/api/chats", json={"title": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_parallel_unlocks_respect_throttle(tmp_path, clock):
    import asyncio

    import httpx

    app = create_app(tmp_path, kdf=FAST_KDF, llm=fake_llm, clock=clock, slots_check=slots_ok,
                     allowed_hosts=["testserver"])

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            await c.post("/api/vault/create", json={"password": PW})
            await c.post("/api/vault/lock")
            rs = await asyncio.gather(*[c.post("/api/vault/unlock", json={"password": "w" * 12}) for _ in range(20)])
            return sorted(r.status_code for r in rs)

    codes = asyncio.run(go())
    assert codes.count(401) == 6 and codes.count(429) == 14


def test_history_alternates_after_failed_reply(tmp_path, clock):
    seen = []
    calls = {"n": 0}

    async def flaky_llm(messages):
        seen.append(messages)
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("down")
        yield "content", "ok"

    with make_client(tmp_path, clock, llm=flaky_llm) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": "first"})
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": "second"})
    roles = [m["role"] for m in seen[1]]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert seen[1][-1] == {"role": "user", "content": "first\n\nsecond"}


PNG_B64 = __import__("base64").b64encode(b"\x89PNG\r\n\x1a\nATT-IMG-CANARY").decode()
TXT_B64 = __import__("base64").b64encode(b"ATT-FILE-CANARY contents").decode()
ATTS = [
    {"name": "pic.png", "mime": "image/png", "data": PNG_B64},
    {"name": "notes.txt", "mime": "text/plain", "data": TXT_B64},
]


def recording_llm(seen):
    async def llm(messages):
        seen.append(messages)
        yield "content", "seen it"

    return llm


def test_send_with_attachments_reaches_model_and_saves(tmp_path, clock):
    seen = []
    with make_client(tmp_path, clock, llm=recording_llm(seen)) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        r = c.post(f"/api/chats/{chat['id']}/messages", json={"content": "what is this", "attachments": ATTS})
        assert events(r)[-1][0] == "done"
        stored = c.get(f"/api/chats/{chat['id']}").json()["messages"][0]
    assert [a["kind"] for a in stored["attachments"]] == ["image", "text"]
    parts = seen[0][-1]["content"]
    assert parts[0]["type"] == "text" and "ATT-FILE-CANARY" in parts[0]["text"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + PNG_B64


def test_attachment_only_message_allowed(client):
    chat = client.post("/api/chats", json={"title": "t"}).json()
    r = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "", "attachments": ATTS[:1]})
    assert r.status_code == 200


def test_empty_message_rejected(client):
    chat = client.post("/api/chats", json={"title": "t"}).json()
    assert client.post(f"/api/chats/{chat['id']}/messages", json={"content": "  "}).status_code == 400


def test_bad_attachment_rejected_nothing_saved(client):
    chat = client.post("/api/chats", json={"title": "t"}).json()
    bad = [{"name": "x.bin", "mime": "application/octet-stream", "data": "AAEC//4="}]
    r = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "hi", "attachments": bad})
    assert r.status_code == 400 and "supported" in r.json()["detail"]
    assert plain(client.get(f"/api/chats/{chat['id']}").json()["messages"]) == []


def test_secret_attachments_not_on_disk(client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h = {"X-Vault-Token": unlocked(client)}
    chat = client.post("/api/secret/chats", json={"title": "s"}, headers=h).json()
    client.post(f"/api/secret/chats/{chat['id']}/messages", json={"content": "x", "attachments": ATTS}, headers=h)
    client.post("/api/vault/lock")
    data = all_bytes(tmp_path) + caplog.text.encode()
    for secret in (b"ATT-IMG-CANARY", PNG_B64.encode(), b"ATT-FILE-CANARY", TXT_B64.encode()):
        assert secret not in data


def test_failed_reply_merges_attachments(tmp_path, clock):
    seen = []
    calls = {"n": 0}

    async def flaky(messages):
        seen.append(messages)
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("down")
        yield "content", "ok"

    with make_client(tmp_path, clock, llm=flaky) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": "first", "attachments": ATTS[:1]})
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": "second"})
    last = seen[1][-1]
    assert last["role"] == "user" and len(seen[1]) == 1
    assert "first" in last["content"][0]["text"] and "second" in last["content"][0]["text"]
    assert last["content"][1]["type"] == "image_url"


def counting_llm(seen):
    async def llm(messages):
        seen.append(messages)
        yield "content", f"reply {len(seen)}"

    return llm


@pytest.mark.parametrize("secret", [False, True])
def test_edit_regenerate_switch(tmp_path, clock, secret):
    seen = []
    with make_client(tmp_path, clock, llm=counting_llm(seen)) as c:
        h = {"X-Vault-Token": unlocked(c)} if secret else {}
        base = "/api/secret/chats" if secret else "/api/chats"
        chat = c.post(base, json={"title": "t"}, headers=h).json()
        url = f"{base}/{chat['id']}"
        c.post(f"{url}/messages", json={"content": "q1", "attachments": ATTS[1:]}, headers=h)
        c.post(f"{url}/messages", json={"content": "q2"}, headers=h)
        msgs = c.get(url, headers=h).json()["messages"]
        q1, a1, q2, a2 = msgs

        # edit q1 -> conversation restarts from there, keeping q1's attachment
        r = c.post(f"{url}/messages", json={"content": "q1 edited", "edit_of": q1["id"]}, headers=h)
        assert events(r)[-1][0] == "done"
        assert seen[-1] == [to_llm_expect("q1 edited")]
        msgs = c.get(url, headers=h).json()["messages"]
        assert [m["content"] for m in msgs] == ["q1 edited", "reply 3"]
        assert msgs[0]["versions"] == 2 and msgs[0]["attachments"][0]["name"] == "notes.txt"

        # go back to the original version
        back = c.post(f"{url}/switch", json={"message_id": q1["id"]}, headers=h).json()
        assert [m["content"] for m in back["messages"]] == ["q1", "reply 1", "q2", "reply 2"]

        # regenerate the first reply -> new version of a1, history = just q1
        r = c.post(f"{url}/regenerate", json={"parent": q1["id"]}, headers=h)
        assert events(r)[-1][0] == "done"
        assert [m["role"] for m in seen[-1]] == ["user"]
        msgs = c.get(url, headers=h).json()["messages"]
        assert [m["content"] for m in msgs] == ["q1", "reply 4"]
        assert msgs[1]["versions"] == 2


def to_llm_expect(text):
    return {"role": "user", "content": [{"type": "text", "text": text + "\n\nAttached file `notes.txt`:\n```\nATT-FILE-CANARY contents\n```"}]}


def test_regenerate_needs_user_message(client):
    chat = client.post("/api/chats", json={"title": "t"}).json()
    client.post(f"/api/chats/{chat['id']}/messages", json={"content": "q"})
    a = client.get(f"/api/chats/{chat['id']}").json()["messages"][1]
    assert client.post(f"/api/chats/{chat['id']}/regenerate", json={"parent": a["id"]}).status_code == 400
    assert client.post(f"/api/chats/{chat['id']}/regenerate", json={"parent": "nope"}).status_code == 404


def test_edit_unknown_message_404(client):
    chat = client.post("/api/chats", json={"title": "t"}).json()
    r = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "x", "edit_of": "nope"})
    assert r.status_code == 404
    assert client.post(f"/api/chats/{chat['id']}/switch", json={"message_id": "nope"}).status_code == 404


def test_secret_edit_versions_stay_encrypted(client, tmp_path):
    h = {"X-Vault-Token": unlocked(client)}
    chat = client.post("/api/secret/chats", json={"title": "s"}, headers=h).json()
    url = f"/api/secret/chats/{chat['id']}"
    client.post(f"{url}/messages", json={"content": "VERSION-ONE-CANARY"}, headers=h)
    q = client.get(url, headers=h).json()["messages"][0]
    client.post(f"{url}/messages", json={"content": "VERSION-TWO-CANARY", "edit_of": q["id"]}, headers=h)
    client.post("/api/vault/lock")
    data = all_bytes(tmp_path)
    assert b"VERSION-ONE-CANARY" not in data and b"VERSION-TWO-CANARY" not in data


def test_vault_locking_mid_request_is_401(client):
    h = {"X-Vault-Token": unlocked(client)}
    vault = client.app.state.vault
    real = vault.list_chats

    def lock_then_list():
        vault.lock()
        return real()

    vault.list_chats = lock_then_list
    assert client.get("/api/secret/chats", headers=h).status_code == 401


async def char_counter(text):
    return len(text)


def context_client(tmp_path, clock, seen, summaries_made, fail_summary=False):
    async def summarizer(messages, *, max_tokens):
        summaries_made.append(messages)
        if fail_summary:
            raise LLMError("down")
        return f"SUMMARY #{len(summaries_made)}"

    return make_client(tmp_path, clock, llm=counting_llm(seen), counter=char_counter,
                       summarizer=summarizer, context_budget=1500)


def long_chat(c, n=8):
    chat = c.post("/api/chats", json={"title": "t"}).json()
    for i in range(n):
        c.post(f"/api/chats/{chat['id']}/messages", json={"content": f"question {i} " + "z" * 300})
    return chat


def test_long_chat_trimmed_with_summary(tmp_path, clock):
    seen, made = [], []
    with context_client(tmp_path, clock, seen, made) as c:
        chat = long_chat(c)
        sent = seen[-1]
        assert sent[0]["role"] == "system" and "SUMMARY" in sent[0]["content"]
        assert sent[-1]["content"].startswith("question 7")
        assert "question 0" not in str(sent[1:])
        # the next turn reuses or extends the stored summary instead of starting over
        before = len(made)
        r = c.post(f"/api/chats/{chat['id']}/regenerate",
                   json={"parent": c.get(f"/api/chats/{chat['id']}").json()["messages"][-2]["id"]})
        ev = events(r)
        assert len(made) == before
        ctx = [d for e, d in ev if e == "context"][0]
        assert ctx["dropped"] > 0 and ctx["summarized"] is True and ctx["first_kept_id"]


def test_status_event_while_summarizing(tmp_path, clock):
    seen, made = [], []
    with context_client(tmp_path, clock, seen, made) as c:
        chat = long_chat(c, n=4)
        r = c.post(f"/api/chats/{chat['id']}/messages", json={"content": "more " + "z" * 300})
        kinds = [e for e, _ in events(r)]
        if "status" in kinds:
            assert kinds.index("status") < kinds.index("content")


def test_summary_failure_still_answers(tmp_path, clock):
    seen, made = [], []
    with context_client(tmp_path, clock, seen, made, fail_summary=True) as c:
        chat = long_chat(c)
        assert seen[-1][0]["role"] == "user"  # no summary, just the newest messages
        assert c.get(f"/api/chats/{chat['id']}").json()["messages"][-1]["content"].startswith("reply")


def test_short_chat_untouched(tmp_path, clock):
    seen, made = [], []
    with context_client(tmp_path, clock, seen, made) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        r = c.post(f"/api/chats/{chat['id']}/messages", json={"content": "hi"})
        assert "context" not in [e for e, _ in events(r)] and made == []


NEW_PW = "another strong password"


def test_change_password_api(client):
    h = {"X-Vault-Token": unlocked(client)}
    chat = client.post("/api/secret/chats", json={"title": "keep"}, headers=h).json()
    body = {"old": PW, "new": NEW_PW}
    assert client.post("/api/vault/password", json=body).status_code == 401  # needs the vault token
    assert client.post("/api/vault/password", json={"old": PW, "new": "short"}, headers=h).status_code == 400
    r = client.post("/api/vault/password", json={"old": "wrong one entirely", "new": NEW_PW}, headers=h)
    assert r.status_code == 403 and r.json() == {"error": "wrong_password"}  # not 401: the page reads 401 as "locked"
    assert client.post("/api/vault/password", json=body, headers=h).status_code == 200
    assert client.get("/api/secret/chats", headers=h).status_code == 200  # still unlocked, same session
    client.post("/api/vault/lock")
    assert client.post("/api/vault/unlock", json={"password": PW}).status_code == 401
    h = {"X-Vault-Token": client.post("/api/vault/unlock", json={"password": NEW_PW}).json()["token"]}
    assert [c["id"] for c in client.get("/api/secret/chats", headers=h).json()] == [chat["id"]]


def test_change_password_wrong_old_is_throttled(client):
    h = {"X-Vault-Token": unlocked(client)}
    for _ in range(6):
        client.post("/api/vault/password", json={"old": "wrong one entirely", "new": NEW_PW}, headers=h)
    r = client.post("/api/vault/password", json={"old": PW, "new": NEW_PW}, headers=h)
    assert r.status_code == 429


def test_wipe_api(client, tmp_path):
    h = {"X-Vault-Token": unlocked(client)}
    client.post("/api/secret/chats", json={"title": "gone"}, headers=h)
    normal = client.post("/api/chats", json={"title": "stays"}).json()
    assert client.post("/api/vault/wipe", json={"confirm": "wipe"}).status_code == 400
    assert (tmp_path / "vault.bin").exists()
    assert client.post("/api/vault/wipe", json={"confirm": "WIPE"}).status_code == 200  # no password needed
    assert client.get("/api/vault").json() == {"exists": False, "unlocked": False}
    assert not (tmp_path / "vault.bin").exists()
    assert client.get("/api/secret/chats", headers=h).status_code == 401
    assert [c["id"] for c in client.get("/api/chats").json()] == [normal["id"]]
    assert client.post("/api/vault/create", json={"password": NEW_PW}).status_code == 200  # start over


def test_wipe_resets_throttle(client):
    unlocked(client)
    client.post("/api/vault/lock")
    for _ in range(7):
        client.post("/api/vault/unlock", json={"password": "wrong one entirely"})
    client.post("/api/vault/wipe", json={"confirm": "WIPE"})
    client.post("/api/vault/create", json={"password": NEW_PW})
    client.post("/api/vault/lock")
    assert client.post("/api/vault/unlock", json={"password": NEW_PW}).status_code == 200
