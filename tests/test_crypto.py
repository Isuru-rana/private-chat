import struct

import pytest

from privatechat.crypto import (
    DEFAULT_KDF,
    FAST_KDF,
    KdfParams,
    WrongPassword,
    open_blob,
    seal,
    seal_with_key,
    wipe,
)

PW = "correct horse battery"


def test_roundtrip():
    blob, _ = seal(PW, b"hello", FAST_KDF)
    assert open_blob(PW, blob)[0] == b"hello"


def test_wrong_password_raises():
    blob, _ = seal(PW, b"hello", FAST_KDF)
    with pytest.raises(WrongPassword):
        open_blob("correct horse batterz", blob)


def test_plaintext_not_in_blob():
    blob, _ = seal(PW, b"xx CANARY-123 xx", FAST_KDF)
    assert b"CANARY-123" not in blob
    assert PW.encode() not in blob


def test_tamper_header_fails():
    blob, _ = seal(PW, b"hello", FAST_KDF)
    salt_offset = 4 + 4 + 4 + 1
    tampered = bytearray(blob)
    tampered[salt_offset] ^= 0x01
    with pytest.raises(WrongPassword):
        open_blob(PW, bytes(tampered))


def test_bad_magic_fails():
    with pytest.raises(WrongPassword):
        open_blob(PW, b"XXXX" + b"\0" * 64)


def test_nonce_fresh():
    _, key = seal(PW, b"x", FAST_KDF, salt=b"s" * 16)
    a = seal_with_key(key, b"s" * 16, FAST_KDF, b"same")
    b = seal_with_key(key, b"s" * 16, FAST_KDF, b"same")
    assert a != b
    assert open_blob(PW, a)[0] == b"same"


def test_default_params_in_header():
    blob, _ = seal(PW, b"x", DEFAULT_KDF)
    assert open_blob(PW, blob)[3] == KdfParams(3, 65536, 4)
    assert struct.unpack(">II", blob[4:12]) == (3, 65536)


def test_open_returns_key_and_salt():
    blob, key = seal(PW, b"x", FAST_KDF)
    _, key2, salt, _ = open_blob(PW, blob)
    assert key2 == key and len(key) == 32 and len(salt) == 16


def test_wipe():
    _, key = seal(PW, b"x", FAST_KDF)
    wipe(key)
    assert all(b == 0 for b in key)
