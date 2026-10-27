from conftest import plain
import os
import stat

import pytest

from privatechat.crypto import FAST_KDF, WrongPassword
from privatechat.vault import Vault, VaultLocked

PW = "a strong vault password"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "vault.bin"


def make(path, clock):
    return Vault(path, kdf=FAST_KDF, idle_seconds=600, clock=clock)


def all_bytes(root):
    out = b""
    for dirpath, _, files in os.walk(root):
        for f in files:
            with open(os.path.join(dirpath, f), "rb") as fh:
                out += fh.read()
    return out


def test_create_short_password_rejected(path, clock):
    with pytest.raises(ValueError):
        make(path, clock).create("x" * 11)
    assert not path.exists()


def test_create_twice_rejected(path, clock):
    v = make(path, clock)
    v.create(PW)
    with pytest.raises(ValueError):
        v.create(PW)


def test_create_leaves_unlocked(path, clock):
    v = make(path, clock)
    v.create(PW)
    assert v.exists() and v.is_unlocked and v.list_chats() == []


def test_create_unlock_roundtrip(path, clock):
    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("t")
    v.append_message(chat["id"], "user", "hello there")
    v.lock()
    v2 = make(path, clock)
    v2.unlock(PW)
    got = v2.get_chat(chat["id"])
    assert got["title"] == "t"
    assert plain(got["messages"]) == [{"role": "user", "content": "hello there"}]


def test_locked_ops_raise(path, clock):
    v = make(path, clock)
    v.create(PW)
    v.lock()
    for op in (v.list_chats, lambda: v.get_chat("x"), lambda: v.create_chat("t")):
        with pytest.raises(VaultLocked):
            op()


def test_wrong_password(path, clock):
    v = make(path, clock)
    v.create(PW)
    v.lock()
    with pytest.raises(WrongPassword):
        v.unlock("x" * 12)
    assert not v.is_unlocked


def test_idle_autolock(path, clock):
    v = make(path, clock)
    v.create(PW)
    clock.t += 601
    assert v.is_unlocked is False


def test_activity_resets_idle(path, clock):
    v = make(path, clock)
    v.create(PW)
    clock.t += 500
    v.list_chats()
    clock.t += 500
    assert v.is_unlocked is True


def test_canary_not_on_disk(path, clock, tmp_path):
    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("TITLE-CANARY")
    v.append_message(chat["id"], "user", "CANARY-XYZ")
    v.lock()
    data = all_bytes(tmp_path)
    assert b"CANARY-XYZ" not in data
    assert b"TITLE-CANARY" not in data
    assert PW.encode() not in data


def test_atomic_write_survives_failed_save(path, clock, tmp_path, monkeypatch):
    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("t")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        v.append_message(chat["id"], "user", "lost")
    monkeypatch.setattr(os, "replace", real_replace)
    assert calls["n"] == 1
    assert not list(tmp_path.glob("*.tmp"))
    v2 = make(path, clock)
    v2.unlock(PW)
    assert plain(v2.get_chat(chat["id"])["messages"]) == []


def test_delete_chat(path, clock):
    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("t")
    assert v.delete_chat(chat["id"]) is True
    assert v.get_chat(chat["id"]) is None
    assert v.delete_chat(chat["id"]) is False


def test_list_newest_first(path, clock):
    v = make(path, clock)
    v.create(PW)
    a = v.create_chat("a")
    b = v.create_chat("b")
    v.append_message(a["id"], "user", "bump")
    assert [c["id"] for c in v.list_chats()][0] == a["id"]
    assert {c["id"] for c in v.list_chats()} == {a["id"], b["id"]}


def test_file_mode_600(path, clock):
    make(path, clock).create(PW)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_clock_going_backwards_locks(path, clock):
    v = make(path, clock)
    v.create(PW)
    clock.t -= 5
    assert v.is_unlocked is False


def test_default_clock_counts_suspend(path):
    import time as _time

    from privatechat.vault import boottime

    assert Vault(path).clock is boottime
    assert abs(boottime() - _time.clock_gettime(_time.CLOCK_BOOTTIME)) < 1


def test_lock_during_save_waits_and_keeps_vault_readable(path, clock, monkeypatch):
    import threading

    import privatechat.crypto as crypto

    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("t")
    real_urandom = crypto.os.urandom
    locker = threading.Thread(target=v.lock)

    def urandom_then_race(n):
        # a Lock click arrives while the save is mid-encryption
        if not locker.is_alive() and locker.ident is None:
            locker.start()
            locker.join(0.2)
        return real_urandom(n)

    monkeypatch.setattr(crypto.os, "urandom", urandom_then_race)
    v.append_message(chat["id"], "user", "TOP SECRET")
    monkeypatch.setattr(crypto.os, "urandom", real_urandom)
    locker.join(2)
    assert not v.is_unlocked
    v2 = make(path, clock)
    v2.unlock(PW)
    assert v2.get_chat(chat["id"])["messages"][-1]["content"] == "TOP SECRET"


def test_commit_refuses_without_key(path, clock):
    v = make(path, clock)
    v.create(PW)
    chats = v._data["chats"]
    v._key = None
    with pytest.raises(VaultLocked):
        v._commit(lambda: chats.append({}), lambda: chats.pop())


def test_attachments_encrypted_on_disk(path, clock, tmp_path):
    import base64

    v = make(path, clock)
    v.create(PW)
    chat = v.create_chat("t")
    img = base64.b64encode(b"\x89PNG\r\n\x1a\nIMAGE-CANARY").decode()
    atts = [
        {"kind": "image", "name": "photo-canary.png", "mime": "image/png", "data": img},
        {"kind": "text", "name": "f.txt", "text": "FILE-CANARY", "truncated": False},
    ]
    v.append_message(chat["id"], "user", "look", atts)
    v.lock()
    data = all_bytes(tmp_path)
    for secret in (b"IMAGE-CANARY", img.encode(), b"FILE-CANARY", b"photo-canary"):
        assert secret not in data
    v2 = make(path, clock)
    v2.unlock(PW)
    assert v2.get_chat(chat["id"])["messages"][0]["attachments"] == atts


def test_keep_alive_refreshes_when_fresh(path, clock):
    v = make(path, clock)
    v.create(PW)
    clock.t += 550
    assert v.keep_alive() is True
    clock.t += 550  # 1100 s since creation, but only 550 since the refresh
    assert v.is_unlocked


def test_keep_alive_locks_when_already_idle(path, clock):
    v = make(path, clock)
    v.create(PW)
    clock.t += 700
    assert v.keep_alive() is False
    assert not v.is_unlocked
    assert v.keep_alive() is False  # never revives a locked vault


NEW_PW = "another strong password"


def test_change_password_keeps_chats(path, clock):
    v = make(path, clock)
    v.create(PW)
    cid = v.create_chat("keep")["id"]
    v.append_message(cid, "user", "secret text")
    old_blob = path.read_bytes()
    v.change_password(PW, NEW_PW)
    assert path.read_bytes()[:20] != old_blob[:20]  # fresh salt in the header
    v.append_message(cid, "assistant", "after change")  # still unlocked, saves under the new key
    v.lock()
    with pytest.raises(WrongPassword):
        v.unlock(PW)
    v.unlock(NEW_PW)
    assert [m["content"] for m in v.get_chat(cid)["messages"]] == ["secret text", "after change"]


def test_change_password_wrong_old_changes_nothing(path, clock):
    v = make(path, clock)
    v.create(PW)
    before = path.read_bytes()
    with pytest.raises(WrongPassword):
        v.change_password("not the password", NEW_PW)
    assert path.read_bytes() == before and v.is_unlocked


def test_change_password_rules(path, clock):
    v = make(path, clock)
    v.create(PW)
    with pytest.raises(ValueError):
        v.change_password(PW, "short")
    v.lock()
    with pytest.raises(VaultLocked):
        v.change_password(PW, NEW_PW)


def test_change_password_failed_write_keeps_old(path, clock, monkeypatch):
    v = make(path, clock)
    v.create(PW)
    cid = v.create_chat("x")["id"]
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        v.change_password(PW, NEW_PW)
    monkeypatch.undo()
    v.append_message(cid, "user", "still works")  # old key still in use
    v.lock()
    v.unlock(PW)
    assert v.get_chat(cid)["messages"][0]["content"] == "still works"


def test_destroy_removes_vault(path, clock, tmp_path):
    v = make(path, clock)
    v.create(PW)
    (tmp_path / ".vault-stray.tmp").write_bytes(b"leftover")
    v.destroy()
    assert not v.exists() and not v.is_unlocked
    assert list(tmp_path.iterdir()) == []
    v.create(NEW_PW)  # can start over
    assert v.list_chats() == []


def test_destroy_when_locked_or_missing(path, clock):
    v = make(path, clock)
    v.destroy()  # nothing there: no error
    v.create(PW)
    v.lock()
    v.destroy()
    assert not v.exists()


def test_change_password_derives_keys_outside_the_lock(path, clock, monkeypatch):
    # Argon2 takes ~1 s; holding the vault lock that long would freeze streaming replies
    import threading
    from privatechat import vault as vault_module

    v = make(path, clock)
    v.create(PW)
    real = vault_module.derive_key
    free = []

    def spy(*a):
        t = threading.Thread(target=lambda: free.append(v._mutex.acquire(timeout=0) and (v._mutex.release() or True)))
        t.start()
        t.join()
        return real(*a)

    monkeypatch.setattr(vault_module, "derive_key", spy)
    v.change_password(PW, NEW_PW)
    assert free == [True, True]
