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


@dataclass
class Peer:
    name: str
    token: str  # hex; proves us to this peer without ever being sent
    last_address: str = ""


@dataclass
class Config:
    device_id: str = ""
    name: str = ""
    port: int = DEFAULT_PORT
    peers: Dict[str, Peer] = field(default_factory=dict)
    offsets: Dict[str, Tuple[int, int]] = field(default_factory=dict)


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
    for device_id, peer in (raw.get("peers") or {}).items():
        cfg.peers[device_id] = Peer(
            name=peer.get("name", ""),
            token=peer.get("token", ""),
            last_address=peer.get("last_address", ""),
        )
    for device_id, offset in (raw.get("offsets") or {}).items():
        cfg.offsets[device_id] = (int(offset[0]), int(offset[1]))
    return cfg


def save(cfg: Config, path: Path = DEFAULT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "device_id": cfg.device_id,
        "name": cfg.name,
        "port": cfg.port,
        "peers": {
            device_id: {
                "name": p.name,
                "token": p.token,
                "last_address": p.last_address,
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
