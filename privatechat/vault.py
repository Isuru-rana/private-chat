"""Secret-chats vault: one encrypted file, key held only in memory while unlocked."""

import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path

from .store import HEAD, check_web_mode
from .tree import annotate_path, latest_leaf, path_to
from .crypto import DEFAULT_KDF, SALT_LEN, KdfParams, WrongPassword, derive_key, open_blob, seal, seal_with_key, wipe

MIN_PASSWORD = 12


def boottime() -> float:
    """Clock that keeps counting while the laptop is suspended (unlike monotonic)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class VaultLocked(Exception):
    pass


def _serialized(method):
    """Run under the vault's lock so lock() can never wipe the key mid-save."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return wrapper


def _copy(obj):
    return json.loads(json.dumps(obj))


def _upgrade(chat: dict) -> None:
    """Chats saved before versions existed were a straight line: chain them."""
    chat.setdefault("web_mode", "off")
    if "head" in chat:
        return
    prev = None
    for m in chat["messages"]:
        m["id"] = secrets.token_hex(8)
        m["parent"] = prev
        prev = m["id"]
    chat["head"] = prev


class Vault:
    def __init__(
        self,
        path: Path,
        kdf: KdfParams = DEFAULT_KDF,
        idle_seconds: int = 600,
        clock: Callable[[], float] = boottime,
    ):
        self.path = Path(path)
        self.kdf = kdf
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._key: bytearray | None = None
        self._salt: bytes | None = None
        self._file_kdf: KdfParams | None = None
        self._data: dict | None = None
        self._last = 0.0
        self._mutex = threading.RLock()

    # --- state ---

    def exists(self) -> bool:
        return self.path.exists()

    @property
    @_serialized
    def is_unlocked(self) -> bool:
        self.check_idle()
        return self._key is not None

    @_serialized
    def check_idle(self) -> None:
        if self._key is None:
            return
        elapsed = self.clock() - self._last
        if elapsed > self.idle_seconds or elapsed < 0:
            self.lock()

    def touch(self) -> None:
        self._last = self.clock()

    @_serialized
    def keep_alive(self) -> bool:
        """An active secret reply counts as use. Idle check first (a suspend still locks), then refresh.
        Returns False if locked (or idle-expired); never revives a locked vault."""
        self.check_idle()
        if self._key is None:
            return False
        self.touch()
        return True

    @_serialized
    def create(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if self.exists():
            raise ValueError("vault already exists")
        data = {"chats": []}
        salt = os.urandom(SALT_LEN)
        blob, key = seal(password, self._encode(data), self.kdf, salt=salt)
        self._write(blob)
        self._set_open(key, salt, self.kdf, data)

    @_serialized
    def unlock(self, password: str) -> None:
        plaintext, key, salt, kdf = open_blob(password, self.path.read_bytes())
        self._set_open(key, salt, kdf, json.loads(plaintext))

    def change_password(self, old: str, new: str) -> None:
        """Re-encrypt everything under `new` with a fresh salt. The old file stays until the new one is written.
        Both Argon2 runs happen outside the vault lock so replies keep streaming meanwhile."""
        if len(new) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self._mutex:
            if not self.is_unlocked:
                raise VaultLocked()
            salt, kdf = self._salt, self._file_kdf
        check = derive_key(old, salt, kdf)
        new_salt = os.urandom(SALT_LEN)
        new_key = derive_key(new, new_salt, self.kdf)
        with self._mutex:
            try:
                if not self.is_unlocked:
                    raise VaultLocked()
                if not hmac.compare_digest(bytes(check), bytes(self._key)):
                    raise WrongPassword()
                self._write(seal_with_key(new_key, new_salt, self.kdf, self._encode(self._data)))
            except BaseException:
                wipe(new_key)
                raise
            finally:
                wipe(check)
            wipe(self._key)
            self._key, self._salt, self._file_kdf = new_key, new_salt, self.kdf
            self.touch()

    @_serialized
    def destroy(self) -> None:
        """Wipe: lock, overwrite the vault file with random bytes, delete it (and any leftover temp files).
        On an SSD old blocks may survive the overwrite; they stay encrypted."""
        self.lock()
        if self.path.exists():
            size = self.path.stat().st_size
            with open(self.path, "r+b") as fh:
                fh.write(os.urandom(size))
                fh.flush()
                os.fsync(fh.fileno())
            self.path.unlink()
        if self.path.parent.exists():
            for tmp in self.path.parent.glob(".vault-*.tmp"):
                tmp.unlink(missing_ok=True)

    @_serialized
    def lock(self) -> None:
        if self._key is not None:
            wipe(self._key)
        self._key = None
        self._data = None
        self._salt = None

    # --- chats ---

    @_serialized
    def list_chats(self) -> list[dict]:
        chats = self._chats()
        return [
            {"id": c["id"], "title": c["title"], "updated": c["updated"]}
            for c in sorted(chats, key=lambda c: c["updated"], reverse=True)
        ]

    @_serialized
    def get_chat(self, chat_id: str) -> dict | None:
        for c in self._chats():
            if c["id"] == chat_id:
                c = _copy(c)
                return {
                    "id": c["id"],
                    "title": c["title"],
                    "updated": c["updated"],
                    "head": c["head"],
                    "web_mode": c["web_mode"],
                    "messages": annotate_path(c["messages"], c["head"]),
                }
        return None

    @_serialized
    def get_message(self, chat_id: str, msg_id: str) -> dict | None:
        return _copy(next((m for m in self._find(chat_id)["messages"] if m["id"] == msg_id), None))

    @_serialized
    def history(self, chat_id: str, msg_id: str) -> list[dict]:
        return _copy(path_to(self._find(chat_id)["messages"], msg_id))

    @_serialized
    def switch(self, chat_id: str, msg_id: str) -> None:
        """Show the version `msg_id` belongs to, continuing to its newest reply."""
        chat = self._find(chat_id)
        if not any(m["id"] == msg_id for m in chat["messages"]):
            raise KeyError(msg_id)
        old = chat["head"]
        new = latest_leaf(chat["messages"], msg_id)
        self._commit(lambda: chat.__setitem__("head", new), lambda: chat.__setitem__("head", old))

    @_serialized
    def create_chat(self, title: str, web_mode: str | None = None) -> dict:
        mode = check_web_mode(web_mode or "off")
        chats = self._chats()
        chat = {
            "id": secrets.token_hex(8), "title": title, "updated": time.time(), "head": None,
            "web_mode": mode, "messages": [],
        }
        self._commit(lambda: chats.append(chat), lambda: chats.remove(chat))
        return {"id": chat["id"], "title": title, "updated": chat["updated"], "web_mode": mode}

    @_serialized
    def set_web_mode(self, chat_id: str, mode: str) -> None:
        check_web_mode(mode)
        chat = self._find(chat_id)
        old = chat["web_mode"]
        self._commit(lambda: chat.__setitem__("web_mode", mode), lambda: chat.__setitem__("web_mode", old))

    @_serialized
    def append_message(
        self, chat_id: str, role: str, content: str, attachments: list | None = None, parent=HEAD,
        meta: dict | None = None,
    ) -> str:
        chat = self._find(chat_id)
        parent = chat["head"] if parent is HEAD else parent
        if parent is not None and not any(m["id"] == parent for m in chat["messages"]):
            raise KeyError(parent)
        msg = {"id": secrets.token_hex(8), "parent": parent, "role": role, "content": content}
        if attachments:
            msg["attachments"] = _copy(attachments)
        if meta:
            msg["meta"] = _copy(meta)
        old_updated, old_head = chat["updated"], chat["head"]

        def do():
            chat["messages"].append(msg)
            chat["updated"] = time.time()
            chat["head"] = msg["id"]

        def undo():
            chat["messages"].pop()
            chat["updated"] = old_updated
            chat["head"] = old_head

        self._commit(do, undo)
        return msg["id"]

    @_serialized
    def summaries(self, chat_id: str) -> dict[str, str]:
        """Stored summaries, keyed by the last message each one covers."""
        return dict(self._find(chat_id).get("summaries", {}))

    @_serialized
    def save_summary(self, chat_id: str, msg_id: str, text: str) -> None:
        chat = self._find(chat_id)
        if not any(m["id"] == msg_id for m in chat["messages"]):
            raise KeyError(msg_id)
        sums = chat.setdefault("summaries", {})
        old = sums.get(msg_id)

        def undo():
            if old is None:
                sums.pop(msg_id, None)
            else:
                sums[msg_id] = old

        self._commit(lambda: sums.__setitem__(msg_id, text), undo)

    @_serialized
    def delete_chat(self, chat_id: str) -> bool:
        chats = self._chats()
        idx = next((i for i, c in enumerate(chats) if c["id"] == chat_id), None)
        if idx is None:
            return False
        chat = chats[idx]
        self._commit(lambda: chats.pop(idx), lambda: chats.insert(idx, chat))
        return True

    # --- internals ---

    def _set_open(self, key: bytearray, salt: bytes, kdf: KdfParams, data: dict) -> None:
        # keep the file's salt + params so re-encryption reuses the same key
        self._salt = salt
        self._file_kdf = kdf
        self._key = key
        self._data = data
        for chat in data["chats"]:
            _upgrade(chat)
        self.touch()

    def _chats(self) -> list:
        if not self.is_unlocked:
            raise VaultLocked()
        self.touch()
        return self._data["chats"]

    def _find(self, chat_id: str) -> dict:
        for c in self._chats():
            if c["id"] == chat_id:
                return c
        raise KeyError(chat_id)

    def _commit(self, do: Callable[[], object], undo: Callable[[], object]) -> None:
        if self._key is None:
            raise VaultLocked()
        do()
        try:
            self._write(seal_with_key(self._key, self._salt, self._file_kdf, self._encode(self._data)))
        except BaseException:
            undo()
            raise

    @staticmethod
    def _encode(data: dict) -> bytes:
        return json.dumps(data, separators=(",", ":")).encode()

    def _write(self, blob: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vault-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
