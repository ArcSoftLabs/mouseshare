"""Pairing: a code shown on one machine, typed into the other.

The code proves a human can see both screens. Once that is established the
target hands out a *random* token, which authenticates every later
connection. The token is random rather than derived from the code, because
a derived token would be computable by anyone who watched the six-digit
exchange go past on the wire.

Neither the code nor the token ever crosses the network. Both sides prove
knowledge of the secret with an HMAC over a transcript containing a fresh
nonce and both device ids -- so a captured proof cannot be replayed, nor
aimed at a different pair of machines.

This authenticates the peer. It does not encrypt the session; see the
security note in the design spec.
"""
import hmac
import secrets
import time
from hashlib import sha256
from typing import Callable

TIMEOUT = 120.0
MAX_ATTEMPTS = 3


class PairingFailed(Exception):
    """The pairing cannot continue. Always fatal to the connection."""


def format_code(value: int) -> str:
    """Zero-padded to six digits, because the HMAC is computed over the
    string a human reads off the screen -- '000042', never '42'."""
    return f"{value:06d}"


def make_code() -> str:
    return format_code(secrets.randbelow(10**6))


def make_nonce() -> str:
    return secrets.token_hex(16)


def make_token() -> str:
    return secrets.token_bytes(32).hex()


def _transcript(nonce: str, initiator_id: str, target_id: str) -> bytes:
    return f"{nonce}|{initiator_id}|{target_id}".encode("ascii")


def proof(secret: bytes, nonce: str, initiator_id: str, target_id: str) -> str:
    """HMAC keyed by the code (during pairing) or the token bytes (later)."""
    return hmac.new(secret, _transcript(nonce, initiator_id, target_id), sha256).hexdigest()


def verify(secret: bytes, mac: str, nonce: str, initiator_id: str, target_id: str) -> bool:
    return hmac.compare_digest(proof(secret, nonce, initiator_id, target_id), mac)


def ok_proof(secret: bytes, nonce: str, target_id: str, initiator_id: str) -> str:
    """Prove the target accepted this specific pairing transcript."""
    transcript = f"{nonce}|{target_id}|{initiator_id}|ok".encode("ascii")
    return hmac.new(secret, transcript, sha256).hexdigest()


def verify_ok(
    secret: bytes, mac: str, nonce: str, target_id: str, initiator_id: str
) -> bool:
    return hmac.compare_digest(
        ok_proof(secret, nonce, target_id, initiator_id), mac
    )


class PendingPairing:
    """Target-side state while a code is on screen waiting to be typed."""

    def __init__(
        self,
        local_id: str,
        peer_id: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._local_id = local_id
        self._peer_id = peer_id
        self._clock = clock
        self._started = clock()
        self._attempts = 0
        self.code = make_code()
        self.nonce = make_nonce()

    def remaining(self) -> float:
        return max(0.0, TIMEOUT - (self._clock() - self._started))

    def check(self, mac: str) -> bool:
        if self.remaining() <= 0:
            raise PairingFailed("pairing code expired")
        if self._attempts >= MAX_ATTEMPTS:
            raise PairingFailed("too many attempts")
        if verify(self.code.encode("ascii"), mac, self.nonce, self._peer_id, self._local_id):
            return True
        self._attempts += 1
        if self._attempts >= MAX_ATTEMPTS:
            # The third wrong code is the third attempt, not a free one
            # before the limit takes effect.
            raise PairingFailed("too many attempts")
        return False
