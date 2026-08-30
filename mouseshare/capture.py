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
        self._radius = 1
        self._last = (0, 0)
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

    def start_remote(self, anchor: Tuple[int, int], radius: int) -> None:
        """Suppress local input and report movement as deltas instead.

        The caller chooses the anchor -- the middle of the screen the
        cursor left -- and the real cursor is parked there. Left on the
        edge it crossed, the OS clamp would eat every further move in that
        direction and fabricate sideways jumps where the desktop steps.
        `radius` is how far the cursor may wander from the anchor before
        it is put back, and must keep it clear of the screen edge.
        """
        self._anchor = (int(anchor[0]), int(anchor[1]))
        self._radius = int(radius)
        self._last = self._anchor
        self._controller.position = self._anchor
        self.suppressing = True

    def stop_remote(self) -> None:
        self.suppressing = False

    def _moved(self, x: int, y: int) -> None:
        """Report movement since the last known position, and keep the
        real cursor away from the screen edge.

        Measured from the previous event rather than from the anchor, so
        the cursor does not have to be warped back after every one. That
        warp was a syscall inside the hook callback which came straight
        back as another hook event, and Windows stops calling a hook that
        cannot keep up -- taking edge detection down with it.
        """
        dx, dy = x - self._last[0], y - self._last[1]
        self._last = (x, y)
        # A hand covers no quarter-screen between two hook events. A jump
        # that large is the cursor being moved for us: the park, one of
        # the warps below, or an event that was already in flight when
        # suppression began. Windows does not mark SetCursorPos as
        # injected, so size is the only thing that tells them apart --
        # and by construction nothing but a warp can exceed the radius,
        # because reaching it is what triggers one.
        if max(abs(dx), abs(dy)) <= self._radius and (dx, dy) != (0, 0):
            self._on_delta(dx, dy)
        if (
            abs(x - self._anchor[0]) > self._radius
            or abs(y - self._anchor[1]) > self._radius
        ):
            self._controller.position = self._anchor
            self._last = self._anchor

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
                    if msg == WM_MOUSEMOVE:
                        # It moved the real cursor, so it is where the next
                        # real event starts from -- but it was not the
                        # user's hand, so it is not movement to forward.
                        self._last = (data.pt.x, data.pt.y)
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
