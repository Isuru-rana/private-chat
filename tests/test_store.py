from conftest import plain
import sqlite3

from privatechat.store import NormalStore


def test_crud(tmp_path):
    s = NormalStore(tmp_path / "chats.db")
    a = s.create_chat("first")
    b = s.create_chat("second")
    s.append_message(a["id"], "user", "hi")
    s.append_message(a["id"], "assistant", "hello")
    got = s.get_chat(a["id"])
    assert got["title"] == "first"
    assert plain(got["messages"]) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert [c["id"] for c in s.list_chats()] == [a["id"], b["id"]]
    assert s.delete_chat(a["id"]) is True
    assert s.get_chat(a["id"]) is None
    assert s.delete_chat(a["id"]) is False
    with sqlite3.connect(tmp_path / "chats.db") as db:
        assert db.execute("select count(*) from messages").fetchone()[0] == 0


def test_persists_across_instances(tmp_path):
    s = NormalStore(tmp_path / "chats.db")
    c = s.create_chat("t")
    s.append_message(c["id"], "user", "x")
    assert NormalStore(tmp_path / "chats.db").get_chat(c["id"])["messages"][0]["content"] == "x"


ATT = [{"kind": "text", "name": "a.txt", "text": "hi", "truncated": False}]


def test_attachments_roundtrip(tmp_path):
    s = NormalStore(tmp_path / "chats.db")
    c = s.create_chat("t")
    s.append_message(c["id"], "user", "see file", ATT)
    s.append_message(c["id"], "assistant", "ok")
    assert plain(s.get_chat(c["id"])["messages"]) == [
        {"role": "user", "content": "see file", "attachments": ATT},
        {"role": "assistant", "content": "ok"},
    ]


def test_migrates_old_db_without_attachments_column(tmp_path):
    path = tmp_path / "chats.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "create table chats(id text primary key, title text not null, updated real not null);"
            "create table messages(chat_id text not null references chats(id) on delete cascade,"
            " idx integer not null, role text not null, content text not null, primary key(chat_id, idx));"
            "insert into chats values ('c1', 'old', 1.0);"
            "insert into messages values ('c1', 0, 'user', 'old msg');"
        )
    s = NormalStore(path)
    assert plain(s.get_chat("c1")["messages"]) == [{"role": "user", "content": "old msg"}]
    s.append_message("c1", "user", "new", ATT)
    assert s.get_chat("c1")["messages"][1]["attachments"] == ATT
