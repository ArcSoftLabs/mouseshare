"""Wire protocol: newline-delimited JSON, one message per line.

Every encoded message carries a version. A stream that is the wrong
version, malformed, or over the line cap raises `ProtocolError`, which the
session treats as a disconnect.

v0.1 skipped garbage lines silently. That was fine for a prototype with no
authentication; it is not fine now, because a desynchronised stream is one
that can no longer be trusted to say when to stop suppressing input.
"""
import base64
import binascii
import json
from typing import Dict, Iterator

VERSION = 3
MIN_VERSION = 2
MAX_LINE = 64 * 1024
CAPABILITIES = ["heartbeat"]
OPTIONAL_TYPES = {"ping", "pong"}


class ProtocolError(Exception):
    """The peer sent something unusable. Always fatal to the connection."""


def encode(msg: dict, version: int = VERSION) -> bytes:
    return json.dumps({**msg, "v": version}, separators=(",", ":")).encode() + b"\n"


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


def pair_ok(name: str, monitors: list, token: str = "", caps=None) -> dict:
    msg = {
        "t": "pair_ok", "name": name, "monitors": monitors,
        "caps": CAPABILITIES if caps is None else list(caps),
    }
    if token:
        msg["token"] = token
    return msg


def pair_err(reason: str) -> dict:
    return {"t": "pair_err", "reason": reason}


# -- input -------------------------------------------------------------------


def layout(monitors: list, caps=None) -> dict:
    return {
        "t": "layout", "monitors": monitors,
        "caps": CAPABILITIES if caps is None else list(caps),
    }


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


def ping(seq: int) -> dict:
    return {"t": "ping", "seq": seq}


def pong(seq: int) -> dict:
    return {"t": "pong", "seq": seq}


def register_optional_type(kind: str) -> None:
    """Register a forward-compatible message type that may be ignored."""
    OPTIONAL_TYPES.add(kind)


def chunk_payload(kind: str, id: str, data: bytes, size=32 * 1024) -> Iterator[dict]:
    if not isinstance(kind, str) or not isinstance(id, str) or size <= 0:
        raise ValueError("kind and id must be strings and size must be positive")
    total = max(1, (len(data) + size - 1) // size)
    for index in range(total):
        part = data[index * size:(index + 1) * size]
        yield {
            "t": kind, "id": id, "i": index, "n": total,
            "data": base64.b64encode(part).decode("ascii"),
        }


class ChunkAssembler:
    def __init__(self, byte_cap: int):
        if byte_cap < 0:
            raise ValueError("byte cap must not be negative")
        self._byte_cap = byte_cap
        self._kind = self._id = None
        self._total = None
        self._parts = {}
        self._size = 0

    def add(self, msg: dict) -> bytes | None:
        kind, chunk_id = msg.get("t"), msg.get("id")
        index, total = msg.get("i"), msg.get("n")
        if (
            not isinstance(kind, str) or not isinstance(chunk_id, str)
            or not isinstance(index, int) or isinstance(index, bool)
            or not isinstance(total, int) or isinstance(total, bool)
            or total <= 0 or total > max(1, self._byte_cap)
            or index < 0 or index >= total
            or not isinstance(msg.get("data"), str)
        ):
            raise ProtocolError("malformed chunk")
        if self._total is None:
            self._kind, self._id, self._total = kind, chunk_id, total
        elif kind != self._kind or chunk_id != self._id:
            raise ProtocolError("mismatched chunk id")
        elif total != self._total:
            raise ProtocolError("mismatched chunk total")
        if index in self._parts:
            raise ProtocolError("duplicate chunk index")
        try:
            part = base64.b64decode(msg["data"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProtocolError("malformed chunk data") from exc
        if self._size + len(part) > self._byte_cap:
            raise ProtocolError("chunk payload exceeds cap")
        self._parts[index] = part
        self._size += len(part)
        if len(self._parts) == self._total:
            return b"".join(self._parts[i] for i in range(self._total))
        return None


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
        version = msg.get("v")
        if (
            not isinstance(version, int) or isinstance(version, bool)
            or not MIN_VERSION <= version <= VERSION
        ):
            raise ProtocolError(
                f"protocol version {version!r}, expected {MIN_VERSION}..{VERSION}"
            )
        return msg
