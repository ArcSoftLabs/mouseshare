"""Global mouse capture with conditional suppression (host side).

Thin adapter over pynput; imports are lazy so the tested core loads on any
platform. Two modes:

- Normal: events reach local apps untouched; `on_move(x, y)` reports the
  absolute cursor position.
- Remote (between start_remote/stop_remote): events are suppressed
  system-wide and reported as callbacks instead.

Platform mechanics differ (verified against pynput 1.8 sources):

- Windows: `suppress_event()` raises inside the low-level hook *before*
  pynput dispatches callbacks, so while suppressing we decode the raw
  message in `win32_event_filter` ourselves and forward it. Suppressed
  events never move the real cursor, so it stays frozen at the anchor and
  every WM_MOUSEMOVE carries anchor+delta in `data.pt`.
- macOS: pynput dispatches callbacks *before* consulting
  `darwin_intercept`, so normal handlers keep firing while suppressing; we
  compute deltas against the anchor and warp the cursor back to it. Our
  warp is an injected event, which the intercept passes through.
"""
import ctypes
import sys
from typing import Callable

WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_CLICKS = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
LLMHF_INJECTED_MASK = 0x3


class MouseCapture:
    def __init__(
        self,
        on_move: Callable[[int, int], None],
        on_click: Callable[[str, bool], None],
        on_scroll: Callable[[int, int], None],
        on_delta: Callable[[int, int], None],
    ):
        self._on_move = on_move
        self._on_click = on_click
        self._on_scroll = on_scroll
        self._on_delta = on_delta
        self.suppressing = False
        self._anchor = (0, 0)
        self._listener = None
        self._controller = None

    def start(self) -> None:
        from pynput import mouse

        self._controller = mouse.Controller()

        def handle_move(x, y):
            if self.suppressing:
                # macOS path: callbacks fire even for suppressed events
                dx, dy = int(x) - self._anchor[0], int(y) - self._anchor[1]
                if (dx, dy) != (0, 0):
                    self._on_delta(dx, dy)
                    self._controller.position = self._anchor
            else:
                self._on_move(int(x), int(y))

        def handle_click(x, y, button, pressed):
            self._on_click(button.name, pressed)

        def handle_scroll(x, y, dx, dy):
            self._on_scroll(int(dx), int(dy))

        kwargs = {}
        if sys.platform == "win32":
            def win32_event_filter(msg, data):
                if not self.suppressing:
                    return True
                if data.flags & LLMHF_INJECTED_MASK:
                    return True  # synthetic event; not from the real mouse
                if msg == WM_MOUSEMOVE:
                    dx = data.pt.x - self._anchor[0]
                    dy = data.pt.y - self._anchor[1]
                    if (dx, dy) != (0, 0):
                        self._on_delta(dx, dy)
                elif msg in WM_CLICKS:
                    self._on_click(*WM_CLICKS[msg])
                elif msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    step = ctypes.c_int16(data.mouseData >> 16).value // 120
                    self._on_scroll(0, step) if msg == WM_MOUSEWHEEL \
                        else self._on_scroll(step, 0)
                self._listener.suppress_event()  # raises; skips normal dispatch

            kwargs["win32_event_filter"] = win32_event_filter
        elif sys.platform == "darwin":
            def darwin_intercept(event_type, event):
                if not self.suppressing:
                    return event
                import Quartz

                if Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGEventSourceUnixProcessID
                ) != 0:
                    return event  # our own warp; let it through
                return None  # swallow the real event

            kwargs["darwin_intercept"] = darwin_intercept

        self._listener = mouse.Listener(
            on_move=handle_move,
            on_click=handle_click,
            on_scroll=handle_scroll,
            **kwargs,
        )
        self._listener.start()

    def start_remote(self) -> None:
        """Suppress local events and report movement as deltas instead."""
        pos = self._controller.position
        self._anchor = (int(pos[0]), int(pos[1]))
        self.suppressing = True

    def stop_remote(self) -> None:
        self.suppressing = False

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
