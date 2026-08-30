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
import sys
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
    ):
        self._on_move = on_move
        self._on_delta = on_delta
        self._on_click = on_click
        self._on_scroll = on_scroll
        self._on_key = on_key
        self.suppressing = False
        self._anchor = (0, 0)
        self._limit = 1
        self._mouse_listener = None
        self._key_listener = None
        self._controller = None
        self._key_enum = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        from pynput import keyboard, mouse

        self._controller = mouse.Controller()
        self._key_enum = keyboard.Key
        self._start_mouse(mouse)
        self._start_keyboard(keyboard)

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
        for listener in (self._mouse_listener, self._key_listener):
            if listener is not None:
                listener.stop()
        self._mouse_listener = self._key_listener = None

    # -- mouse ---------------------------------------------------------------

    def _start_mouse(self, mouse) -> None:
        def handle_move(x, y, *_):
            if self.suppressing:
                # macOS path: callbacks fire even for suppressed events.
                # Untested as a host: if the tap stops the move reaching
                # the window server the cursor never leaves the anchor,
                # every callback carries anchor+delta, and measuring from
                # `_last` would telescope movement to nothing.
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
            kwargs["darwin_intercept"] = self._darwin_intercept

        self._key_listener = keyboard.Listener(
            on_press=handle_press, on_release=handle_release, **kwargs
        )
        self._key_listener.start()

    # -- shared --------------------------------------------------------------

    def _darwin_intercept(self, event_type, event):
        if not self.suppressing:
            return event
        import Quartz

        if Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGEventSourceUnixProcessID
        ) != 0:
            return event  # injected by us; let it through
        return None  # swallow the real event
