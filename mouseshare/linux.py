"""Linux runtime support for MouseShare input capture.

X11 is supported, including XWayland when MouseShare and its target clients
are X clients; WSLg therefore counts as X11. Wayland-native clients are
invisible to XRecord and XTest, so Wayland sessions are refused.
"""
import os
from pathlib import Path
from typing import Mapping, Optional, Tuple


def session_type(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the supported display-session kind visible in the environment."""
    values = os.environ if env is None else env
    declared = values.get("XDG_SESSION_TYPE", "").lower()
    if declared == "wayland":
        return "wayland"
    if values.get("DISPLAY"):
        return "x11"
    if values.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "none"


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mouseshare"


def log_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "mouseshare" / "debug.log"


class XGrab:
    """Own an idempotent X11 pointer-and-keyboard grab."""

    def __init__(self, display=None, x=None) -> None:
        if display is None or x is None:
            from Xlib import X
            from Xlib.display import Display

            display = Display()
            x = X
        self._display = display
        self._x = x
        self._root = display.screen().root
        self._grabbed = False

    def grab(self) -> None:
        if self._grabbed:
            return
        x = self._x
        pointer = self._root.grab_pointer(
            False,
            0,
            x.GrabModeAsync,
            x.GrabModeAsync,
            self._root,
            x.NONE,
            x.CurrentTime,
        )
        if pointer != x.GrabSuccess:
            raise RuntimeError("could not grab the X11 pointer")
        try:
            keyboard = self._root.grab_keyboard(
                False, x.GrabModeAsync, x.GrabModeAsync, x.CurrentTime
            )
        except Exception as exc:
            self._display.ungrab_pointer(x.CurrentTime)
            self._display.sync()
            raise RuntimeError("could not grab the X11 keyboard") from exc
        if keyboard != x.GrabSuccess:
            self._display.ungrab_pointer(x.CurrentTime)
            self._display.sync()
            raise RuntimeError("could not grab the X11 keyboard")
        self._display.sync()
        self._grabbed = True

    def ungrab(self) -> None:
        if not self._grabbed:
            return
        self._display.ungrab_pointer(self._x.CurrentTime)
        self._display.ungrab_keyboard(self._x.CurrentTime)
        self._display.sync()
        self._drain_events()
        self._grabbed = False

    def warp(self, point: Tuple[int, int]) -> None:
        self._root.warp_pointer(int(point[0]), int(point[1]))
        self._display.flush()
        self._drain_events()

    def _drain_events(self) -> None:
        while self._display.pending_events():
            self._display.next_event()


def warp_pointer(grab: XGrab, point: Tuple[int, int]) -> None:
    """Warp through an existing X connection so ordering is explicit."""
    grab.warp(point)
