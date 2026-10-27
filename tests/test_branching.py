"""Edit / regenerate / switch-version behaviour, identical for both stores."""

import sqlite3

import pytest

from conftest import plain
from privatechat.crypto import FAST_KDF
from privatechat.store import NormalStore
from privatechat.vault import Vault


@pytest.fixture(params=["normal", "vault"])
def store(request, tmp_path):
    if request.param == "normal":
        return NormalStore(tmp_path / "chats.db")
    v = Vault(tmp_path / "vault.bin", kdf=FAST_KDF)
    v.create("a strong vault password")
    return v


def convo(store, chat_id):
    return [m["content"] for m in store.get_chat(chat_id)["messages"]]


def test_append_returns_id_and_moves_head(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q1")
    a = store.append_message(c["id"], "assistant", "a1")
    chat = store.get_chat(c["id"])
    assert chat["head"] == a and [m["id"] for m in chat["messages"]] == [u, a]


def test_edit_creates_version_and_keeps_old(store):
    c = store.create_chat("t")
    u1 = store.append_message(c["id"], "user", "q1")
    store.append_message(c["id"], "assistant", "a1")
    store.append_message(c["id"], "user", "q2")
    store.append_message(c["id"], "assistant", "a2")
    # edit q1: new user message with the same parent as q1
    u1b = store.append_message(c["id"], "user", "q1 edited", parent=None)
    store.append_message(c["id"], "assistant", "a1 new")
    assert convo(store, c["id"]) == ["q1 edited", "a1 new"]
    first = store.get_chat(c["id"])["messages"][0]
    assert first["versions"] == 2 and first["version"] == 2 and first["sibling_ids"] == [u1, u1b]
    # go back to the original version: the whole old conversation returns
    store.switch(c["id"], u1)
    assert convo(store, c["id"]) == ["q1", "a1", "q2", "a2"]


def test_regenerate_adds_reply_version(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q")
    a1 = store.append_message(c["id"], "assistant", "first answer")
    a2 = store.append_message(c["id"], "assistant", "second answer", parent=u)
    last = store.get_chat(c["id"])["messages"][-1]
    assert last["content"] == "second answer" and last["sibling_ids"] == [a1, a2]


def test_history_up_to_message(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q", [{"kind": "text", "name": "f", "text": "x", "truncated": False}])
    store.append_message(c["id"], "assistant", "a")
    hist = store.history(c["id"], u)
    assert plain(hist) == [{"role": "user", "content": "q", "attachments": [{"kind": "text", "name": "f", "text": "x", "truncated": False}]}]


def test_get_message_and_unknown_ids(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q")
    assert store.get_message(c["id"], u)["content"] == "q"
    assert store.get_message(c["id"], "nope") is None
    with pytest.raises(KeyError):
        store.switch(c["id"], "nope")
    with pytest.raises(KeyError):
        store.append_message(c["id"], "user", "x", parent="nope")


def test_old_linear_db_is_migrated(tmp_path):
    path = tmp_path / "chats.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "create table chats(id text primary key, title text not null, updated real not null);"
            "create table messages(chat_id text not null references chats(id) on delete cascade,"
            " idx integer not null, role text not null, content text not null, attachments text,"
            " primary key(chat_id, idx));"
            "insert into chats values ('c1', 'old', 1.0);"
            "insert into messages values ('c1', 0, 'user', 'q', null);"
            "insert into messages values ('c1', 1, 'assistant', 'a', null);"
        )
    s = NormalStore(path)
    assert convo(s, "c1") == ["q", "a"]
    s.append_message("c1", "user", "q2")
    assert convo(s, "c1") == ["q", "a", "q2"]


def test_old_linear_vault_is_migrated(tmp_path):
    v = Vault(tmp_path / "vault.bin", kdf=FAST_KDF)
    v.create("a strong vault password")
    # simulate a vault saved before versions existed
    v._data["chats"].append({"id": "c1", "title": "old", "updated": 1.0, "messages": [
        {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]})
    v._commit(lambda: None, lambda: None)
    v.lock()
    v.unlock("a strong vault password")
    assert convo(v, "c1") == ["q", "a"]
    v.append_message("c1", "user", "q2")
    assert convo(v, "c1") == ["q", "a", "q2"]


def test_summaries_saved_per_message(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q")
    assert store.summaries(c["id"]) == {}
    store.save_summary(c["id"], u, "gist")
    assert store.summaries(c["id"]) == {u: "gist"}
    store.save_summary(c["id"], u, "newer gist")
    assert store.summaries(c["id"]) == {u: "newer gist"}
    with pytest.raises(KeyError):
        store.save_summary(c["id"], "nope", "x")


def test_vault_summary_encrypted(tmp_path):
    import os

    v = Vault(tmp_path / "vault.bin", kdf=FAST_KDF)
    v.create("a strong vault password")
    c = v.create_chat("t")
    u = v.append_message(c["id"], "user", "q")
    v.save_summary(c["id"], u, "SUMMARY-CANARY")
    v.lock()
    assert b"SUMMARY-CANARY" not in (tmp_path / "vault.bin").read_bytes()
    assert not [f for f in os.listdir(tmp_path) if f != "vault.bin"]


# --- message meta + per-chat web mode ---


def test_meta_roundtrip(store):
    c = store.create_chat("t")
    u = store.append_message(c["id"], "user", "q")
    meta = {"steps": [{"tool": "search", "q": "x"}], "sources": ["https://a.example/"]}
    a = store.append_message(c["id"], "assistant", "ans", meta=meta)
    msgs = store.get_chat(c["id"])["messages"]
    assert "meta" not in msgs[0] and msgs[1]["meta"] == meta
    assert store.get_message(c["id"], a)["meta"] == meta
    assert "meta" not in store.get_message(c["id"], u)
    assert store.history(c["id"], a)[-1]["meta"] == meta
    assert plain(msgs)[1] == {"role": "assistant", "content": "ans"}


def test_web_mode_defaults(store):
    expected = "ask_once" if isinstance(store, NormalStore) else "off"
    c = store.create_chat("t")
    assert store.get_chat(c["id"])["web_mode"] == expected
    c2 = store.create_chat("t2", web_mode="auto")
    assert store.get_chat(c2["id"])["web_mode"] == "auto"


def test_set_web_mode_validates(store):
    c = store.create_chat("t")
    store.set_web_mode(c["id"], "ask_each")
    assert store.get_chat(c["id"])["web_mode"] == "ask_each"
    with pytest.raises(ValueError):
        store.set_web_mode(c["id"], "bogus")
    with pytest.raises(ValueError):
        store.create_chat("x", web_mode="bogus")
    assert store.get_chat(c["id"])["web_mode"] == "ask_each"
    with pytest.raises(KeyError):
        store.set_web_mode("nope", "off")


def test_old_db_gets_web_mode_and_meta_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        create table chats(id text primary key, title text not null, updated real not null, head text);
        create table messages(chat_id text not null, idx integer not null, role text not null,
            content text not null, attachments text, id text, parent text, primary key(chat_id, idx));
        create table summaries(chat_id text not null, msg_id text not null, text text not null,
            primary key(chat_id, msg_id));
        insert into chats values ('c1', 'old', 1.0, 'm1');
        insert into messages values ('c1', 0, 'user', 'hi', null, 'm1', null);
        """
    )
    db.commit()
    db.close()
    s = NormalStore(path)
    chat = s.get_chat("c1")
    assert chat["web_mode"] == "ask_once" and "meta" not in chat["messages"][0]
    s.append_message("c1", "assistant", "yo", meta={"k": 1})
    assert s.get_chat("c1")["messages"][1]["meta"] == {"k": 1}


def test_vault_meta_encrypted(tmp_path):
    v = Vault(tmp_path / "vault.bin", kdf=FAST_KDF)
    v.create("a strong vault password")
    c = v.create_chat("t")
    v.append_message(c["id"], "assistant", "a", meta={"q": "CANARY-meta-7731"})
    assert b"CANARY-meta-7731" not in (tmp_path / "vault.bin").read_bytes()
    v.lock()
    v.unlock("a strong vault password")
    assert v.get_chat(c["id"])["messages"][0]["meta"] == {"q": "CANARY-meta-7731"}


def test_vault_upgrade_old_chat_gets_web_mode_off():
    from privatechat.vault import _upgrade

    chat = {"id": "x", "title": "t", "updated": 1.0, "head": None, "messages": []}
    _upgrade(chat)
    assert chat["web_mode"] == "off"


def test_append_does_not_alias_caller_data(store):
    c = store.create_chat("t")
    meta = {"urls": ["a"]}
    atts = [{"name": "orig"}]
    a = store.append_message(c["id"], "assistant", "x", attachments=atts, meta=meta)
    meta["urls"].append("x")
    atts[0]["name"] = "changed"
    for m in (store.get_message(c["id"], a), store.get_chat(c["id"])["messages"][0]):
        assert m["meta"] == {"urls": ["a"]} and m["attachments"] == [{"name": "orig"}]
