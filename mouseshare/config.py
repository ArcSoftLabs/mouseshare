"""Load/save MouseShare configuration (~/.mouseshare/config.json)."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from .layout import Screen

DEFAULT_PORT = 39471
DEFAULT_PATH = Path.home() / ".mouseshare" / "config.json"


def _default_screens() -> Dict[str, Screen]:
    return {
        "host": Screen(0, 0, 1920, 1080),
        "client": Screen(1920, 0, 1920, 1080),
    }


@dataclass
class Config:
    peer_host: str = ""
    port: int = DEFAULT_PORT
    screens: Dict[str, Screen] = field(default_factory=_default_screens)


def load(path: Path = DEFAULT_PATH) -> Config:
    if not path.exists():
        return Config()
    raw = json.loads(path.read_text())
    screens = {
        name: Screen(**rect) for name, rect in raw.get("screens", {}).items()
    } or _default_screens()
    return Config(
        peer_host=raw.get("peer_host", ""),
        port=raw.get("port", DEFAULT_PORT),
        screens=screens,
    )


def save(cfg: Config, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "peer_host": cfg.peer_host,
        "port": cfg.port,
        "screens": {
            name: {"x": s.x, "y": s.y, "w": s.w, "h": s.h}
            for name, s in cfg.screens.items()
        },
    }
    path.write_text(json.dumps(raw, indent=2))
