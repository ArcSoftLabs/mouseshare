"""Enumerating this machine's monitors.

The coordinates reported here are the ones `inject` uses to warp a cursor,
so they are passed through untouched -- including negative origins from a
monitor placed left of or above the primary. Normalising them would move
the cursor to the wrong screen.

Whether those numbers agree with `pynput`'s idea of a cursor position under
DPI scaling and Retina is a platform question that only a real machine can
answer; see the release checklist in the design spec.
"""
import sys
from types import SimpleNamespace
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


FALLBACK = Monitor("", "0", 0, 0, 1920, 1080, primary=True)


def appkit_to_quartz(screens: List[Any]) -> List[Any]:
    """Convert AppKit's bottom-left frames to Quartz's top-left space."""
    if not screens:
        return []
    primary_h = int(screens[0].height)
    return [
        SimpleNamespace(
            x=s.x,
            y=primary_h - (s.y + s.height),
            width=s.width,
            height=s.height,
            is_primary=getattr(s, "is_primary", False),
            name=getattr(s, "name", None),
        )
        for s in screens
    ]


def enumerate_local(device_id: str) -> List[Monitor]:
    """This machine's monitors, or one assumed screen.

    Enumeration fails on a headless session and on display setups the
    backend does not recognise. Refusing to start would be the wrong
    answer -- the layout editor exists precisely so a wrong guess can be
    corrected by hand.
    """
    try:
        from screeninfo import get_monitors

        screens = get_monitors()
        if sys.platform == "darwin":
            screens = appkit_to_quartz(screens)
        found = from_screens(device_id, screens)
    except Exception:  # noqa: BLE001 - any backend failure means "unknown"
        found = []
    if found:
        return found
    return [
        Monitor(device_id, FALLBACK.id, FALLBACK.x, FALLBACK.y,
                FALLBACK.w, FALLBACK.h, primary=True)
    ]


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
