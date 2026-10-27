"""Password-based encryption for the secret-chats vault.

Blob layout:
    b"PCV1" | u32 time_cost | u32 memory_cost | u8 parallelism | salt(16) | nonce(12) | ciphertext+tag
Everything before the nonce is authenticated as AAD, so tampering with the
KDF params or salt fails decryption just like a wrong password.
"""

import os
import struct
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"PCV1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
_PARAMS = struct.Struct(">IIB")
_HEADER_LEN = len(MAGIC) + _PARAMS.size + SALT_LEN


@dataclass(frozen=True)
class KdfParams:
    time_cost: int
    memory_cost: int  # KiB
    parallelism: int


DEFAULT_KDF = KdfParams(3, 65536, 4)
FAST_KDF = KdfParams(1, 8, 1)  # tests only


class WrongPassword(Exception):
    pass


def _derive(password: str, salt: bytes, kdf: KdfParams) -> bytearray:
    return bytearray(
        hash_secret_raw(
            password.encode(),
            salt,
            time_cost=kdf.time_cost,
            memory_cost=kdf.memory_cost,
            parallelism=kdf.parallelism,
            hash_len=KEY_LEN,
            type=Type.ID,
        )
    )


derive_key = _derive  # public name for callers that check a password against a held key


def _header(salt: bytes, kdf: KdfParams) -> bytes:
    return MAGIC + _PARAMS.pack(kdf.time_cost, kdf.memory_cost, kdf.parallelism) + salt


def seal_with_key(key: bytearray, salt: bytes, kdf: KdfParams, plaintext: bytes) -> bytes:
    header = _header(salt, kdf)
    nonce = os.urandom(NONCE_LEN)
    return header + nonce + AESGCM(bytes(key)).encrypt(nonce, plaintext, header)


def seal(
    password: str, plaintext: bytes, kdf: KdfParams = DEFAULT_KDF, salt: bytes | None = None
) -> tuple[bytes, bytearray]:
    salt = salt if salt is not None else os.urandom(SALT_LEN)
    key = _derive(password, salt, kdf)
    return seal_with_key(key, salt, kdf, plaintext), key


def open_blob(password: str, blob: bytes) -> tuple[bytes, bytearray, bytes, KdfParams]:
    if len(blob) < _HEADER_LEN + NONCE_LEN or blob[:4] != MAGIC:
        raise WrongPassword()
    kdf = KdfParams(*_PARAMS.unpack(blob[4 : 4 + _PARAMS.size]))
    salt = blob[4 + _PARAMS.size : _HEADER_LEN]
    nonce = blob[_HEADER_LEN : _HEADER_LEN + NONCE_LEN]
    key = _derive(password, salt, kdf)
    try:
        plaintext = AESGCM(bytes(key)).decrypt(nonce, blob[_HEADER_LEN + NONCE_LEN :], blob[:_HEADER_LEN])
    except InvalidTag:
        wipe(key)
        raise WrongPassword() from None
    return plaintext, key, salt, kdf


def wipe(key: bytearray) -> None:
    for i in range(len(key)):
        key[i] = 0
