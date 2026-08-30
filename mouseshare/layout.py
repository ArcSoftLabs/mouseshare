"""The shared plane: every monitor of both machines, laid out side by side.

Each machine reports its monitors in its own OS coordinates. Those are kept
verbatim, because they are exactly what `inject` needs to warp a cursor.
The plane is a second coordinate space that exists only to answer one
question: when the cursor leaves this machine, where does it arrive?

Only whole devices move on the plane. Rearranging the monitors *inside* a
machine is the operating system's job, and letting the editor do it too
would let the UI show an arrangement the OS contradicts.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Rect = Tuple[int, int, int, int]


@dataclass(frozen=True)
class Monitor:
    device_id: str
    id: str
    x: int
    y: int
    w: int
    h: int
    primary: bool = False


def _contains(rect: Rect, px: int, py: int) -> bool:
    x, y, w, h = rect
    return x <= px < x + w and y <= py < y + h


def _overlap(a: Rect, b: Rect) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class Layout:
    def __init__(self, monitors: List[Monitor], offsets: Dict[str, Tuple[int, int]]):
        self.monitors = list(monitors)
        self.offsets = dict(offsets)

    # -- devices -------------------------------------------------------------

    def device_ids(self) -> List[str]:
        seen = []
        for m in self.monitors:
            if m.device_id not in seen:
                seen.append(m.device_id)
        return seen

    def _monitors_of(self, device_id: str) -> List[Monitor]:
        return [m for m in self.monitors if m.device_id == device_id]

    def _origin(self, device_id: str) -> Tuple[int, int]:
        """The device's own top-left in its OS space, which the plane
        offset is measured from."""
        mons = self._monitors_of(device_id)
        return min(m.x for m in mons), min(m.y for m in mons)

    def set_offset(self, device_id: str, offset: Tuple[int, int]) -> None:
        self.offsets[device_id] = offset

    # -- coordinate conversion -----------------------------------------------

    def to_plane(self, device_id: str, os_x: int, os_y: int) -> Tuple[int, int]:
        ox, oy = self.offsets[device_id]
        mx, my = self._origin(device_id)
        return ox + (os_x - mx), oy + (os_y - my)

    def from_plane(self, device_id: str, vx: int, vy: int) -> Tuple[int, int]:
        ox, oy = self.offsets[device_id]
        mx, my = self._origin(device_id)
        return mx + (vx - ox), my + (vy - oy)

    def plane_rect(self, device_id: str, monitor_id: str) -> Rect:
        m = next(
            mon for mon in self.monitors
            if mon.device_id == device_id and mon.id == monitor_id
        )
        x, y = self.to_plane(device_id, m.x, m.y)
        return x, y, m.w, m.h

    def _plane_rects(self, device_id: str, offset: Optional[Tuple[int, int]] = None) -> List[Rect]:
        ox, oy = self.offsets[device_id] if offset is None else offset
        mx, my = self._origin(device_id)
        return [
            (ox + (m.x - mx), oy + (m.y - my), m.w, m.h)
            for m in self._monitors_of(device_id)
        ]

    # -- crossing ------------------------------------------------------------

    def map_exit(self, device_id: str, os_x: int, os_y: int) -> Optional[Tuple[str, int, int]]:
        """Where does this machine's cursor position land on another machine?

        Returns `(target_device_id, target_os_x, target_os_y)` in the target
        machine's own coordinates, or None when the point is still on this
        machine or in empty plane space.
        """
        vx, vy = self.to_plane(device_id, os_x, os_y)
        # Sorted so that a violated no-overlap invariant produces a
        # repeatable answer rather than a coin flip.
        for m in sorted(self.monitors, key=lambda m: (m.device_id, m.id)):
            if m.device_id == device_id:
                continue  # the OS handles this machine's own monitors
            if _contains(self.plane_rect(m.device_id, m.id), vx, vy):
                return (m.device_id, *self.from_plane(m.device_id, vx, vy))
        return None

    def clamp(self, device_id: str, os_x: int, os_y: int) -> Tuple[int, int]:
        """Pin a point onto whichever of that device's monitors is nearest."""
        mons = self._monitors_of(device_id)
        if any(_contains((m.x, m.y, m.w, m.h), os_x, os_y) for m in mons):
            return os_x, os_y

        def pinned(m: Monitor) -> Tuple[int, int]:
            return (
                min(max(os_x, m.x), m.x + m.w - 1),
                min(max(os_y, m.y), m.y + m.h - 1),
            )

        def distance(m: Monitor) -> int:
            px, py = pinned(m)
            return (px - os_x) ** 2 + (py - os_y) ** 2

        return pinned(min(mons, key=distance))

    # -- placement -----------------------------------------------------------

    def can_place(self, device_id: str, offset: Tuple[int, int]) -> bool:
        """Would this offset overlap another device's actual screens?

        Compared against the union of monitor rectangles, not the bounding
        box: an L-shaped arrangement has a box far larger than the screens
        in it, and rejecting placements into that empty corner would refuse
        perfectly good layouts.
        """
        mine = self._plane_rects(device_id, offset)
        for other in self.device_ids():
            if other == device_id:
                continue
            for a in mine:
                for b in self._plane_rects(other):
                    if _overlap(a, b):
                        return False
        return True

    def _extent(self, device_id: str) -> Rect:
        rects = self._plane_rects(device_id)
        x = min(r[0] for r in rects)
        y = min(r[1] for r in rects)
        return x, y, max(r[0] + r[2] for r in rects) - x, max(r[1] + r[3] for r in rects) - y

    def snap_device(self, mobile: str, anchor: str) -> None:
        """Move `mobile` flush against the nearest edge of `anchor`,
        closing whichever gap or overlap is smallest."""
        ax, ay, aw, ah = self._extent(anchor)
        mx, my, mw, mh = self._extent(mobile)
        gaps = {
            "right": abs(mx - (ax + aw)),
            "left": abs((mx + mw) - ax),
            "below": abs(my - (ay + ah)),
            "above": abs((my + mh) - ay),
        }
        side = min(gaps, key=gaps.get)
        ox, oy = self.offsets[mobile]
        if side == "right":
            ox += (ax + aw) - mx
        elif side == "left":
            ox += ax - (mx + mw)
        elif side == "below":
            oy += (ay + ah) - my
        else:
            oy += ay - (my + mh)
        self.offsets[mobile] = (ox, oy)
