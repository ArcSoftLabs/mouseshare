"""Screen layout model: rectangles on a shared virtual plane."""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Screen:
    x: int
    y: int
    w: int
    h: int

    def contains(self, vx: int, vy: int) -> bool:
        return self.x <= vx < self.x + self.w and self.y <= vy < self.y + self.h


class Layout:
    def __init__(self, screens: Dict[str, Screen]):
        self.screens = screens

    def map_exit(self, name: str, lx: int, ly: int) -> Optional[Tuple[str, int, int]]:
        """If local point (lx, ly) has left screen `name` and lands on another
        screen, return (other_name, other_lx, other_ly). Otherwise None."""
        screen = self.screens[name]
        if 0 <= lx < screen.w and 0 <= ly < screen.h:
            return None
        vx, vy = screen.x + lx, screen.y + ly
        for other_name, other in self.screens.items():
            if other_name == name:
                continue
            if other.contains(vx, vy):
                return other_name, vx - other.x, vy - other.y
        return None

    def set_size(self, name: str, w: int, h: int) -> None:
        """Replace a screen's dimensions with its actual measured size."""
        s = self.screens[name]
        self.screens[name] = Screen(s.x, s.y, w, h)

    def snap(self, mobile: str, anchor: str) -> None:
        """Move `mobile` so it sits flush against the nearest edge of
        `anchor`, eliminating any gap or overlap between them."""
        a, m = self.screens[anchor], self.screens[mobile]
        gaps = {
            "right": abs(m.x - (a.x + a.w)),
            "left": abs((m.x + m.w) - a.x),
            "below": abs(m.y - (a.y + a.h)),
            "above": abs((m.y + m.h) - a.y),
        }
        side = min(gaps, key=gaps.get)
        x, y = m.x, m.y
        if side == "right":
            x = a.x + a.w
        elif side == "left":
            x = a.x - m.w
        elif side == "below":
            y = a.y + a.h
        else:
            y = a.y - m.h
        self.screens[mobile] = Screen(x, y, m.w, m.h)

    def clamp(self, name: str, lx: int, ly: int) -> Tuple[int, int]:
        """Clamp a local point onto screen `name`."""
        screen = self.screens[name]
        return (
            min(max(lx, 0), screen.w - 1),
            min(max(ly, 0), screen.h - 1),
        )
