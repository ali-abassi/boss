"""Deterministic Ed25519 fixture used only by the fake Herdr transport."""
import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
    "7db31227db4e14fe0b16810746c32ea61c565cb7a44f355c09b3d71ac92cf99a"
))
PUBLIC_KEY_B64 = base64.b64encode(
    _PRIVATE.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
).decode()


def signed_event(event: dict, event_sequence: int, previous_sha256: str) -> dict:
    unsigned = {"event_sequence": event_sequence, "previous_sha256": previous_sha256, **event}
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    signature = base64.b64encode(_PRIVATE.sign(bytes.fromhex(digest))).decode()
    return {**unsigned, "event_sha256": digest, "signature": signature}


def sign_chain(events: list[dict], previous_sha256: str = "0" * 64,
               start_sequence: int = 1) -> list[dict]:
    output = []
    for offset, event in enumerate(events):
        signed = signed_event(event, start_sequence + offset, previous_sha256)
        output.append(signed); previous_sha256 = signed["event_sha256"]
    return output
