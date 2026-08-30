"""Enumerating this machine's monitors.

The coordinates reported here are the ones `inject` uses to warp a cursor,
so they are passed through untouched -- including negative origins from a
monitor placed left of or above the primary. Normalising them would move
the cursor to the wrong screen.

Whether those numbers agree with `pynput`'s idea of a cursor position under
DPI scaling and Retina is a platform question that only a real machine can
answer; see the release checklist in the design spec.
"""
from typing import Any, Dict, List

from .layout import Monitor


def from_screens(device_id: str, screens: List[Any]) -> List[Monitor]:
    """Convert screeninfo-shaped objects into Monitors."""
    out: List[Monitor] = []
    used: Dict[str, int] = {}
    for index, s in enumerate(screens):
        base = getattr(s, "name", None) or str(index)
        count = used.get(base, 0)
        used[base] = count + 1
        out.append(
            Monitor(
                device_id=device_id,
                id=base if count == 0 else f"{base}#{count}",
                x=int(s.x),
                y=int(s.y),
                w=int(s.width),
                h=int(s.height),
                primary=bool(getattr(s, "is_primary", False)),
            )
        )
    return out


def enumerate_local(device_id: str) -> List[Monitor]:
    from screeninfo import get_monitors

    return from_screens(device_id, get_monitors())


def to_wire(monitors: List[Monitor]) -> List[dict]:
    return [
        {"id": m.id, "x": m.x, "y": m.y, "w": m.w, "h": m.h, "primary": m.primary}
        for m in monitors
    ]


def from_wire(device_id: str, raw: List[dict]) -> List[Monitor]:
    return [
        Monitor(
            device_id=device_id,
            id=m["id"],
            x=int(m["x"]),
            y=int(m["y"]),
            w=int(m["w"]),
            h=int(m["h"]),
            primary=bool(m.get("primary", False)),
        )
        for m in raw
    ]
