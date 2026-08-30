"""Wire protocol: newline-delimited JSON messages."""
import json
from typing import Dict, Iterator


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict:
    return json.loads(line)


def hello(role: str, w: int, h: int) -> dict:
    return {"t": "hello", "role": role, "w": w, "h": h}


def enter(x: int, y: int) -> dict:
    return {"t": "enter", "x": x, "y": y}


def pos(x: int, y: int) -> dict:
    return {"t": "pos", "x": x, "y": y}


def click(button: str, pressed: bool) -> dict:
    return {"t": "click", "button": button, "pressed": pressed}


def scroll(dx: int, dy: int) -> dict:
    return {"t": "scroll", "dx": dx, "dy": dy}


def leave() -> dict:
    return {"t": "leave"}


class LineBuffer:
    """Reassembles a byte stream into decoded JSON messages."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> Iterator[Dict]:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
