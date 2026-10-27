"""Normal (unencrypted, by design) chat storage in SQLite."""

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .tree import annotate_path, latest_leaf, path_to

_SCHEMA = """
create table if not exists chats(id text primary key, title text not null, updated real not null);
create table if not exists messages(
    chat_id text not null references chats(id) on delete cascade,
    idx integer not null, role text not null, content text not null,
    primary key(chat_id, idx));
create table if not exists summaries(
    chat_id text not null references chats(id) on delete cascade,
    msg_id text not null, text text not null,
    primary key(chat_id, msg_id));
"""

HEAD = object()  # append after the chat's current head
WEB_MODES = ("off", "ask_each", "ask_once", "auto")


def check_web_mode(mode: str) -> str:
    if mode not in WEB_MODES:
        raise ValueError(f"web_mode must be one of {WEB_MODES}")
    return mode


def _row_to_message(
    id: str, parent: str | None, role: str, content: str, attachments: str | None, meta: str | None = None
) -> dict:
    msg = {"id": id, "parent": parent, "role": role, "content": content}
    if attachments:
        msg["attachments"] = json.loads(attachments)
    if meta:
        msg["meta"] = json.loads(meta)
    return msg


class NormalStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        cols = {row[1] for row in db.execute("pragma table_info(messages)")}
        if "attachments" not in cols:
            db.execute("alter table messages add column attachments text")
        if "meta" not in cols:
            db.execute("alter table messages add column meta text")
        if "id" not in cols:
            # older chats were a straight line: chain each message to the one before it
            db.execute("alter table messages add column id text")
            db.execute("alter table messages add column parent text")
            db.execute("alter table chats add column head text")
            for (chat_id,) in db.execute("select id from chats").fetchall():
                prev = None
                for (idx,) in db.execute(
                    "select idx from messages where chat_id = ? order by idx", (chat_id,)
                ).fetchall():
                    mid = secrets.token_hex(8)
                    db.execute(
                        "update messages set id = ?, parent = ? where chat_id = ? and idx = ?",
                        (mid, prev, chat_id, idx),
                    )
                    prev = mid
                db.execute("update chats set head = ? where id = ?", (prev, chat_id))
        if "web_mode" not in {row[1] for row in db.execute("pragma table_info(chats)")}:
            db.execute("alter table chats add column web_mode text not null default 'ask_once'")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path)
        try:
            db.execute("pragma foreign_keys = on")
            with db:
                yield db
        finally:
            db.close()

    def _messages(self, db: sqlite3.Connection, chat_id: str) -> list[dict]:
        rows = db.execute(
            "select id, parent, role, content, attachments, meta from messages where chat_id = ? order by idx",
            (chat_id,),
        ).fetchall()
        return [_row_to_message(*r) for r in rows]

    def _head(self, db: sqlite3.Connection, chat_id: str) -> str | None:
        row = db.execute("select head from chats where id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(chat_id)
        return row[0]

    def list_chats(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("select id, title, updated from chats order by updated desc").fetchall()
        return [{"id": i, "title": t, "updated": u} for i, t, u in rows]

    def get_chat(self, chat_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("select id, title, updated, head, web_mode from chats where id = ?", (chat_id,)).fetchone()
            if row is None:
                return None
            msgs = self._messages(db, chat_id)
        return {"id": row[0], "title": row[1], "updated": row[2], "head": row[3], "web_mode": row[4], "messages": annotate_path(msgs, row[3])}

    def get_message(self, chat_id: str, msg_id: str) -> dict | None:
        with self._db() as db:
            return next((m for m in self._messages(db, chat_id) if m["id"] == msg_id), None)

    def history(self, chat_id: str, msg_id: str) -> list[dict]:
        with self._db() as db:
            return path_to(self._messages(db, chat_id), msg_id)

    def create_chat(self, title: str, web_mode: str | None = None) -> dict:
        mode = check_web_mode(web_mode or "ask_once")
        chat = {"id": secrets.token_hex(8), "title": title, "updated": time.time(), "web_mode": mode}
        with self._db() as db:
            db.execute(
                "insert into chats (id, title, updated, web_mode) values (?, ?, ?, ?)",
                (chat["id"], title, chat["updated"], mode),
            )
        return chat

    def set_web_mode(self, chat_id: str, mode: str) -> None:
        check_web_mode(mode)
        with self._db() as db:
            if db.execute("update chats set web_mode = ? where id = ?", (mode, chat_id)).rowcount == 0:
                raise KeyError(chat_id)

    def append_message(
        self, chat_id: str, role: str, content: str, attachments: list | None = None, parent=HEAD,
        meta: dict | None = None,
    ) -> str:
        with self._db() as db:
            head = self._head(db, chat_id)
            parent = head if parent is HEAD else parent
            if parent is not None and db.execute(
                "select 1 from messages where chat_id = ? and id = ?", (chat_id, parent)
            ).fetchone() is None:
                raise KeyError(parent)
            (n,) = db.execute(
                "select coalesce(max(idx) + 1, 0) from messages where chat_id = ?", (chat_id,)
            ).fetchone()
            mid = secrets.token_hex(8)
            db.execute(
                "insert into messages (chat_id, idx, role, content, attachments, id, parent, meta)"
                " values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id, n, role, content, json.dumps(attachments) if attachments else None, mid, parent,
                    json.dumps(meta) if meta else None,
                ),
            )
            db.execute("update chats set head = ?, updated = ? where id = ?", (mid, time.time(), chat_id))
        return mid

    def switch(self, chat_id: str, msg_id: str) -> None:
        """Show the version `msg_id` belongs to, continuing to its newest reply."""
        with self._db() as db:
            msgs = self._messages(db, chat_id)
            if not any(m["id"] == msg_id for m in msgs):
                raise KeyError(msg_id)
            db.execute("update chats set head = ? where id = ?", (latest_leaf(msgs, msg_id), chat_id))

    def summaries(self, chat_id: str) -> dict[str, str]:
        """Stored summaries, keyed by the last message each one covers."""
        with self._db() as db:
            return dict(db.execute("select msg_id, text from summaries where chat_id = ?", (chat_id,)).fetchall())

    def save_summary(self, chat_id: str, msg_id: str, text: str) -> None:
        with self._db() as db:
            if db.execute("select 1 from messages where chat_id = ? and id = ?", (chat_id, msg_id)).fetchone() is None:
                raise KeyError(msg_id)
            db.execute("insert or replace into summaries values (?, ?, ?)", (chat_id, msg_id, text))

    def delete_chat(self, chat_id: str) -> bool:
        with self._db() as db:
            return db.execute("delete from chats where id = ?", (chat_id,)).rowcount > 0
