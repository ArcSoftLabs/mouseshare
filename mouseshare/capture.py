"""Global mouse and keyboard capture with conditional suppression (host).

Thin adapter over pynput; imports are lazy so the tested core loads on any
platform. Two modes:

- Normal: events reach local apps untouched; `on_move(x, y)` reports the
  absolute cursor position and nothing else is forwarded.
- Remote (between start_remote/stop_remote): events are suppressed
  system-wide and reported as callbacks instead.

Platform mechanics, verified against the pynput 1.8.2 sources:

- **Windows.** Only `suppress_event()` actually stops an event reaching
  other applications: it raises `SuppressException`, which the hook
  handler turns into a non-zero return. Returning `False` from the filter
  merely skips pynput's own callback dispatch -- the hook still calls
  `CallNextHookEx` and the keystroke lands in whatever app has focus. So
  while suppressing we decode the raw event ourselves in the filter and
  then raise.

  Keys are decoded with the listener's own `_event_to_key()` rather than a
  hand-written virtual-key table. That reuses pynput's `_SPECIAL_KEYS` map
  and its `KeyTranslator`, so layouts, printables and left/right modifier
  identity behave exactly as they do when not suppressing. `_PRESS_MESSAGES`
  and `_RELEASE_MESSAGES` include the `WM_SYSKEY*` variants, so Alt
  combinations are not lost.

- **macOS.** `_handler` dispatches callbacks *before* consulting
  `darwin_intercept`, so the normal handlers keep firing while suppressing.

Either way movement is measured from the previous event's position, and
the cursor is only warped back to the anchor once it has wandered near
the screen edge. See `_moved`.
"""
import ctypes
import logging
import sys
import threading
import time
from typing import Callable, Optional, Tuple

WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_CLICKS = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
# LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
MOUSE_INJECTED_MASK = 0x01 | 0x02
# LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
KEY_INJECTED_MASK = 0x10 | 0x02
ESCAPE_WINDOW = 0.5
log = logging.getLogger("mouseshare")


class EscapeDetector:
    """Recognise two clean modifier taps without depending on pynput."""

    def __init__(self, key: str, window: float, clock=time.monotonic):
        self.key = key
        self.window = window
        self._clock = clock
        self._down = False
        self._repeated = False
        self._first_tap = None

    def event(self, key: str, pressed: bool) -> bool:
        matches = key == self.key or key in (f"{self.key}_l", f"{self.key}_r")
        if not matches:
            self._first_tap = None
            self._down = False
            self._repeated = False
            return False
        if pressed:
            if self._down:
                self._repeated = True
            else:
                self._down = True
            return False
        if not self._down:
            self._first_tap = None
            return False
        self._down = False
        if self._repeated:
            self._repeated = False
            self._first_tap = None
            return False
        now = self._clock()
        if self._first_tap is not None and now - self._first_tap <= self.window:
            self._first_tap = None
            return True
        self._first_tap = now
        return False


def key_to_wire(key, key_enum) -> Optional[Tuple[str, str]]:
    """Turn a pynput key into the tagged wire form, or None to drop it.

    A key with no character -- a dead key mid-composition, or one pynput
    could not translate -- is dropped rather than sent as junk the peer
    could never resolve, and so could never release.
    """
    if key is None:
        return None
    name = getattr(key, "name", None)
    if isinstance(key, key_enum) or (name and not hasattr(key, "char")):
        return ("special", name)
    char = getattr(key, "char", None)
    return ("char", char) if char else None


class InputCapture:
    def __init__(
        self,
        on_move: Callable[[int, int], None],
        on_delta: Callable[[int, int], None],
        on_click: Callable[[str, bool], None],
        on_scroll: Callable[[int, int], None],
        on_key: Callable[[str, str, bool], None],
        on_escape: Callable[[], None] = lambda: None,
        on_capture_lost: Callable[[], None] = lambda: None,
        escape_key: str = "ctrl",
    ):
        self._on_move = on_move
        self._on_delta = on_delta
        self._on_click = on_click
        self._on_scroll = on_scroll
        self._on_key = on_key
        self._on_escape = on_escape
        self._on_capture_lost = on_capture_lost
        self._escape_key = escape_key
        self._escape = EscapeDetector(escape_key, ESCAPE_WINDOW)
        self.suppressing = False
        self._anchor = (0, 0)
        self._limit = 1
        self._mouse_listener = None
        self._key_listener = None
        self._controller = None
        self._key_enum = None
        self._consume_current = False
        self._watchdog_stop = threading.Event()
        self._watchdog = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        from pynput import keyboard, mouse

        self._controller = mouse.Controller()
        self._key_enum = keyboard.Key
        self._start_mouse(mouse)
        self._start_keyboard(keyboard)
        self._start_watchdog()

    def set_escape_key(self, key: str) -> None:
        self._escape_key = key
        self._escape = EscapeDetector(key, ESCAPE_WINDOW)

    def start_remote(self, anchor: Tuple[int, int], limit: int) -> None:
        """Suppress local input and report movement as deltas instead.

        The caller chooses the anchor -- the middle of the screen the
        cursor left -- and the real cursor is parked there. Left on the
        edge it crossed, the OS clamp would eat every further move in that
        direction and fabricate sideways jumps where the desktop steps.
        `limit` is the largest offset from the anchor that can be real
        movement; anything beyond it is the cursor being moved for us.
        """
        self._anchor = (int(anchor[0]), int(anchor[1]))
        self._limit = int(limit)
        self._escape = EscapeDetector(self._escape_key, ESCAPE_WINDOW)
        self._controller.position = self._anchor
        self.suppressing = True

    def stop_remote(self) -> None:
        self.suppressing = False

    def _moved(self, x: int, y: int) -> None:
        """Report one event's movement, measured from the anchor.

        A suppressed event never reaches the desktop, so the real cursor
        stays parked and each event carries the anchor plus its own
        movement. Nothing needs putting back afterwards -- and it must
        not be, because a warp is a syscall inside the hook callback that
        returns as another hook event, and Windows stops calling a hook
        that cannot keep up at a thousand reports a second.
        """
        dx, dy = x - self._anchor[0], y - self._anchor[1]
        if max(abs(dx), abs(dy)) > self._limit:
            # Not the user. The event already in the hook pipeline when
            # suppression began still carries the pre-park position, and
            # Windows does not mark our park as injected, so size is what
            # tells them apart. Put the cursor back instead of reporting.
            self._controller.position = self._anchor
        elif (dx, dy) != (0, 0):
            self._on_delta(dx, dy)

    def stop(self) -> None:
        self.suppressing = False
        self._watchdog_stop.set()
        for listener in (self._mouse_listener, self._key_listener):
            if listener is not None:
                listener.stop()
        self._mouse_listener = self._key_listener = None

    def _start_watchdog(self, interval: float = 0.5) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()

        def watch():
            while not self._watchdog_stop.wait(interval):
                listeners = (self._mouse_listener, self._key_listener)
                dead = False
                for listener in listeners:
                    if listener is not None:
                        try:
                            listener.join(timeout=0)
                        except Exception:  # noqa: BLE001 - listener reports its failure
                            log.exception("input listener failed")
                            dead = True
                if dead or any(
                    listener is not None and not listener.running
                    for listener in listeners
                ):
                    was_suppressing = self.suppressing
                    self.suppressing = False
                    if was_suppressing:
                        try:
                            self._on_capture_lost()
                        except Exception:  # noqa: BLE001 - watchdog must survive callback
                            log.exception("capture-lost handler failed")
                    return

        self._watchdog = threading.Thread(
            target=watch, name="mouseshare-capture-watchdog", daemon=True
        )
        self._watchdog.start()

    # -- mouse ---------------------------------------------------------------

    def _start_mouse(self, mouse) -> None:
        def handle_move(x, y, *_):
            if self.suppressing:
                # macOS path: callbacks fire even for suppressed events.
                # Untested as a host. This assumes the intercept leaves
                # the real cursor parked, as suppression does on Windows;
                # if it does not, the offsets accumulate and the peer
                # cursor runs away in whatever direction it was going.
                self._moved(int(x), int(y))
            else:
                self._on_move(int(x), int(y))

        def handle_click(x, y, button, pressed, *_):
            if self.suppressing:
                self._on_click(button.name, pressed)

        def handle_scroll(x, y, dx, dy, *_):
            if self.suppressing:
                self._on_scroll(int(dx), int(dy))

        kwargs = {}
        if sys.platform == "win32":
            def win32_event_filter(msg, data):
                if not self.suppressing:
                    return True
                if data.flags & MOUSE_INJECTED_MASK:
                    # Any synthetic event passes, not just ours. Windows
                    # gives no reliable way to tell our warp from another
                    # tool's, and swallowing all injected input would break
                    # accessibility software outright. Physical input --
                    # the thing the user is holding -- is still suppressed.
                    return True
                if msg == WM_MOUSEMOVE:
                    self._moved(data.pt.x, data.pt.y)
                elif msg in WM_CLICKS:
                    self._on_click(*WM_CLICKS[msg])
                elif msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    step = ctypes.c_int16(data.mouseData >> 16).value // 120
                    if msg == WM_MOUSEWHEEL:
                        self._on_scroll(0, step)
                    else:
                        self._on_scroll(step, 0)
                self._mouse_listener.suppress_event()  # raises; stops propagation

            kwargs["win32_event_filter"] = win32_event_filter
        elif sys.platform == "darwin":
            kwargs["darwin_intercept"] = self._darwin_intercept

        self._mouse_listener = mouse.Listener(
            on_move=handle_move,
            on_click=handle_click,
            on_scroll=handle_scroll,
            **kwargs,
        )
        self._mouse_listener.start()

    # -- keyboard ------------------------------------------------------------

    def _start_keyboard(self, keyboard) -> None:
        def emit(key, pressed):
            wire = key_to_wire(key, self._key_enum)
            value = (
                wire[1] if wire is not None
                else getattr(key, "name", None) or getattr(key, "char", None) or ""
            )
            escaped = self._escape.event(value, pressed)
            if escaped:
                if sys.platform == "darwin":
                    self._consume_current = True
                self._on_escape()
                return
            if wire is not None:
                self._on_key(wire[0], wire[1], pressed)

        def handle_press(key, *_):
            if self.suppressing:
                emit(key, True)  # macOS: callbacks fire before the intercept

        def handle_release(key, *_):
            if self.suppressing:
                emit(key, False)

        kwargs = {}
        if sys.platform == "win32":
            def win32_event_filter(msg, data):
                if not self.suppressing:
                    return True
                if data.flags & KEY_INJECTED_MASK:
                    return True
                listener = self._key_listener
                if msg in listener._PRESS_MESSAGES or msg in listener._RELEASE_MESSAGES:
                    pressed = msg in listener._PRESS_MESSAGES
                    try:
                        # pynput's own translation: special-key table plus
                        # KeyTranslator for printables, so layouts and
                        # left/right modifiers behave as they normally do.
                        key = listener._event_to_key(msg, data.vkCode)
                    except OSError:
                        key = None
                    emit(key, pressed)
                listener.suppress_event()  # raises; stops propagation

            kwargs["win32_event_filter"] = win32_event_filter
        elif sys.platform == "darwin":
            kwargs["darwin_intercept"] = self._darwin_keyboard_intercept

        self._key_listener = keyboard.Listener(
            on_press=handle_press, on_release=handle_release, **kwargs
        )
        self._key_listener.start()

    # -- shared --------------------------------------------------------------

    def _darwin_keyboard_intercept(self, event_type, event):
        if self._consume_current:
            self._consume_current = False
            return None
        return self._darwin_intercept(event_type, event)

    def _darwin_intercept(self, event_type, event):
        if not self.suppressing:
            return event
        import Quartz

        if Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGEventSourceUnixProcessID
        ) != 0:
            return event  # injected by us; let it through
        return None  # swallow the real event
