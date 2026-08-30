"""Wire protocol: newline-delimited JSON, one message per line.

Every encoded message carries a version. A stream that is the wrong
version, malformed, or over the line cap raises `ProtocolError`, which the
session treats as a disconnect.

v0.1 skipped garbage lines silently. That was fine for a prototype with no
authentication; it is not fine now, because a desynchronised stream is one
that can no longer be trusted to say when to stop suppressing input.
"""
import json
from typing import Dict, Iterator

VERSION = 2
MAX_LINE = 64 * 1024


class ProtocolError(Exception):
    """The peer sent something unusable. Always fatal to the connection."""


def encode(msg: dict) -> bytes:
    return json.dumps({**msg, "v": VERSION}, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict:
    return json.loads(line)


# -- pairing -----------------------------------------------------------------


def pair_request(device_id: str, name: str) -> dict:
    return {"t": "pair_request", "device_id": device_id, "name": name}


def pair_challenge(nonce: str, device_id: str) -> dict:
    """Carries the responder's own id: the proof transcript binds both
    device ids, and a connector reaching a typed-in address does not know
    the other machine's id until it is told."""
    return {"t": "pair_challenge", "nonce": nonce, "device_id": device_id}


def pair_proof(mac: str) -> dict:
    return {"t": "pair_proof", "hmac": mac}


def auth(device_id: str, mac: str) -> dict:
    return {"t": "auth", "device_id": device_id, "hmac": mac}


def pair_ok(name: str, monitors: list, token: str = "") -> dict:
    msg = {"t": "pair_ok", "name": name, "monitors": monitors}
    if token:
        msg["token"] = token
    return msg


def pair_err(reason: str) -> dict:
    return {"t": "pair_err", "reason": reason}


# -- input -------------------------------------------------------------------


def layout(monitors: list) -> dict:
    return {"t": "layout", "monitors": monitors}


def enter(x: int, y: int) -> dict:
    return {"t": "enter", "x": x, "y": y}


def pos(x: int, y: int) -> dict:
    return {"t": "pos", "x": x, "y": y}


def click(button: str, pressed: bool) -> dict:
    return {"t": "click", "button": button, "pressed": pressed}


def scroll(dx: int, dy: int) -> dict:
    return {"t": "scroll", "dx": dx, "dy": dy}


def key_special(value: str, pressed: bool) -> dict:
    """A named key -- resolved by the client through `pynput.keyboard.Key`."""
    return {"t": "key", "kind": "special", "value": value, "pressed": pressed}


def key_char(value: str, pressed: bool) -> dict:
    """A printable key -- resolved through `KeyCode.from_char`."""
    return {"t": "key", "kind": "char", "value": value, "pressed": pressed}


def leave() -> dict:
    return {"t": "leave"}


class LineBuffer:
    """Reassembles a byte stream into decoded messages."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> Iterator[Dict]:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if len(line) > MAX_LINE:
                raise ProtocolError(f"line of {len(line)} bytes exceeds cap")
            if not line.strip():
                continue
            yield self._parse(line)
        if len(self._buf) > MAX_LINE:
            raise ProtocolError(f"unterminated line exceeds {MAX_LINE} bytes")

    @staticmethod
    def _parse(line: bytes) -> dict:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"malformed JSON: {exc}") from exc
        if not isinstance(msg, dict):
            raise ProtocolError("message is not an object")
        if msg.get("v") != VERSION:
            raise ProtocolError(f"protocol version {msg.get('v')!r}, expected {VERSION}")
        return msg
