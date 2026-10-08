"""sealed — everything that crosses the public runner repo travels as Fernet ciphertext (AES-128-CBC + HMAC-SHA256).

The key lives in Nexus (~/.iphone/run.key, 0600) and once as the repo secret IPHONE_RUN_KEY. The dispatch input
(tunnel URLs, run token, target hosts, the spec and its credentials) and the result bundle (screenshots, results.json,
Appium log, Safari console) are both sealed, so a stranger reading the public repo's inputs, logs or artifacts reads
nothing. Standalone on purpose: the runner repo carries this file as-is. `guide iphone:tunnel`.
"""

import json

from cryptography.fernet import Fernet, InvalidToken


class SealBroken(Exception):
    pass


def new_key() -> str:
    return Fernet.generate_key().decode()


def seal_bytes(plain: bytes, *, key: str) -> bytes:
    return Fernet(key.encode()).encrypt(plain)


def unseal_bytes(ciphertext: bytes, *, key: str) -> bytes:
    try:
        return Fernet(key.encode()).decrypt(ciphertext)
    except InvalidToken as error:
        raise SealBroken("ciphertext does not open with this key — the repo secret IPHONE_RUN_KEY and "
                         "~/.iphone/run.key differ; `iphone setup` sets them together") from error


def seal(payload: dict, *, key: str) -> str:
    return seal_bytes(json.dumps(payload).encode(), key=key).decode()


def unseal(ciphertext: str, *, key: str) -> dict:
    return json.loads(unseal_bytes(ciphertext.encode(), key=key))
