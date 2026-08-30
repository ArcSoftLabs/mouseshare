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

  Suppressed events never move the real cursor, so it stays frozen at the
  anchor and every `WM_MOUSEMOVE` carries anchor+delta in `data.pt`.

- **macOS.** `_handler` dispatches callbacks *before* consulting
  `darwin_intercept`, so the normal handlers keep firing while suppressing;
  we compute deltas against the anchor and warp the cursor back to it. Our
  warp is an injected event, which the intercept passes through.
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

    def start_remote(self, anchor: Tuple[int, int]) -> None:
        """Suppress local input and report movement as deltas instead.

        The caller chooses the anchor -- the middle of the screen the
        cursor left -- and the real cursor is parked there. Left on the
        edge it crossed, the OS clamp would eat every further move in that
        direction and fabricate sideways jumps where the desktop steps.
        """
        self._anchor = (int(anchor[0]), int(anchor[1]))
        # Park before suppressing, not after: anything still in the hook
        # pipeline then takes the normal path, where the session drops it
        # for being remote already, instead of being read as a deliberate
        # move away from an anchor the cursor has not reached yet.
        self._controller.position = self._anchor
        self.suppressing = True

    def stop_remote(self) -> None:
        self.suppressing = False

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
                dx, dy = int(x) - self._anchor[0], int(y) - self._anchor[1]
                if (dx, dy) != (0, 0):
                    self._on_delta(dx, dy)
                    self._controller.position = self._anchor
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
                    dx = data.pt.x - self._anchor[0]
                    dy = data.pt.y - self._anchor[1]
                    if (dx, dy) != (0, 0):
                        self._on_delta(dx, dy)
                        # Put it back every time. The cursor does drift off
                        # the anchor -- injected events are let through
                        # above and move it for real, and events already in
                        # the hook pipeline when suppression began still
                        # carry the old position. Left uncorrected the gap
                        # never closes: every later event repeats the same
                        # bias and the peer cursor sticks in a corner.
                        self._controller.position = self._anchor
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
