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

    def clamp(self, name: str, lx: int, ly: int) -> Tuple[int, int]:
        """Clamp a local point onto screen `name`."""
        screen = self.screens[name]
        return (
            min(max(lx, 0), screen.w - 1),
            min(max(ly, 0), screen.h - 1),
        )
