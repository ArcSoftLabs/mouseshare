"""Configuration and identity (~/.mouseshare/config.json).

This file now holds pairing tokens, so it is written atomically and kept
private to the user. A corrupt file is replaced rather than propagated as
a crash -- losing a pairing is a minor annoyance, refusing to start is not.
"""
import json
import os
import socket
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple

DEFAULT_PORT = 39471
DEFAULT_PATH = Path.home() / ".mouseshare" / "config.json"
ESCAPE_KEYS = frozenset({"ctrl", "cmd", "alt", "shift"})


def default_escape_key() -> str:
    return "cmd" if sys.platform == "darwin" else "ctrl"


@dataclass
class Peer:
    name: str
    token: str  # hex; proves us to this peer without ever being sent
    # Where it answered last time. Kept because discovery is the first
    # thing a firewall stops, and a machine you have already paired with
    # should not become unreachable just because multicast went quiet.
    # The port is its own, not ours: one already taken here is free there.
    last_address: str = ""
    last_port: int = 0


@dataclass
class Config:
    device_id: str = ""
    name: str = ""
    port: int = DEFAULT_PORT
    peers: Dict[str, Peer] = field(default_factory=dict)
    offsets: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    escape_key: str = field(default_factory=default_escape_key)


def _defaults() -> Config:
    return Config(device_id=uuid.uuid4().hex, name=socket.gethostname())


def load(path: Path = DEFAULT_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        return _defaults()
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
    except (OSError, ValueError):
        return _defaults()

    cfg = _defaults()
    cfg.device_id = raw.get("device_id") or cfg.device_id
    cfg.name = raw.get("name") or cfg.name
    cfg.port = raw.get("port", DEFAULT_PORT)
    escape_key = raw.get("escape_key", cfg.escape_key)
    if escape_key in ESCAPE_KEYS:
        cfg.escape_key = escape_key
    for device_id, peer in (raw.get("peers") or {}).items():
        cfg.peers[device_id] = Peer(
            name=peer.get("name", ""),
            token=peer.get("token", ""),
            last_address=peer.get("last_address", ""),
            last_port=peer.get("last_port", 0),
        )
    for device_id, offset in (raw.get("offsets") or {}).items():
        cfg.offsets[device_id] = (int(offset[0]), int(offset[1]))
    return cfg


def load_or_create(path: Path = DEFAULT_PATH) -> Config:
    """Load, and write the result straight back.

    A fresh install and a corrupt file both mint a new `device_id`. That id
    keys every stored token and is baked into the pairing transcript, so if
    it is not persisted immediately it changes on the next launch and
    silently breaks every pairing the user had made.
    """
    cfg = load(path)
    try:
        save(cfg, path)
    except OSError as exc:  # read-only home, full disk -- run anyway
        import logging

        logging.getLogger("mouseshare").warning("could not write config: %s", exc)
    return cfg


def save(cfg: Config, path: Path = DEFAULT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "device_id": cfg.device_id,
        "name": cfg.name,
        "port": cfg.port,
        "escape_key": cfg.escape_key,
        "peers": {
            device_id: {
                "name": p.name,
                "token": p.token,
                "last_address": p.last_address,
                "last_port": p.last_port,
            }
            for device_id, p in cfg.peers.items()
        },
        "offsets": {d: list(o) for d, o in cfg.offsets.items()},
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    if sys.platform != "win32":
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- it holds tokens
    os.replace(tmp, path)  # atomic: readers see the old file or the new one
