"""Event injection on the client, plus cursor placement on the host.

The injector remembers every key and button it has pressed and not
released. Anything left down when the cursor leaves, the peer vanishes, or
the app shuts down is released explicitly. Without that registry a
connection dropped mid-chord leaves a modifier stuck on the other machine,
and the user cannot type their way out of it.

Platform work lives behind `PynputBackend` so the registry -- the part
that matters for safety -- is testable without a display.
"""
import logging
import threading
from typing import Set, Tuple

Held = Tuple[str, str, str]

log = logging.getLogger("mouseshare")


class PynputBackend:
    """The real thing. Imports pynput lazily so the core loads anywhere."""

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._mouse_mod = mouse
        self._keyboard_mod = keyboard
        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()

    def move_to(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    def button(self, name: str, pressed: bool) -> bool:
        btn = getattr(self._mouse_mod.Button, name, None)
        if btn is None:
            return False  # not on this platform, e.g. x1 on macOS
        (self._mouse.press if pressed else self._mouse.release)(btn)
        return True

    def key(self, kind: str, value: str, pressed: bool) -> bool:
        if kind == "special":
            resolved = getattr(self._keyboard_mod.Key, value, None)
        else:
            resolved = self._keyboard_mod.KeyCode.from_char(value)
        if resolved is None:
            return False
        (self._keyboard.press if pressed else self._keyboard.release)(resolved)
        return True


class Injector:
    def __init__(self, backend):
        self._backend = backend
        self._held: Set[Held] = set()
        # The reader thread injects while a disconnect drains. Without this
        # the drain can iterate a set that is still being added to.
        self._lock = threading.Lock()

    @classmethod
    def create(cls) -> "Injector":
        return cls(PynputBackend())

    def held(self) -> Set[Held]:
        with self._lock:
            return set(self._held)

    def move_to(self, x: int, y: int) -> None:
        self._backend.move_to(x, y)

    def scroll(self, dx: int, dy: int) -> None:
        self._backend.scroll(dx, dy)

    def click(self, name: str, pressed: bool) -> None:
        if not self._backend.button(name, pressed):
            return  # unresolvable: never pressed, so never track it
        self._track(("button", "", name), pressed)

    def key(self, kind: str, value: str, pressed: bool) -> None:
        if not self._backend.key(kind, value, pressed):
            return
        self._track(("key", kind, value), pressed)

    def _track(self, item: Held, pressed: bool) -> None:
        with self._lock:
            if pressed:
                self._held.add(item)  # a set, so autorepeat holds it once
            else:
                self._held.discard(item)

    def release_all(self) -> None:
        """Let go of everything. Safe to call at any time, including twice.

        Every item is released independently and the registry is cleared
        whatever happens. This is the last line of defence against a stuck
        modifier: giving up on the first failure would leave every
        remaining key down, and the user cannot type their way out of that.
        """
        with self._lock:
            pending, self._held = sorted(self._held), set()
        for kind, sub, value in pending:
            try:
                if kind == "button":
                    self._backend.button(value, False)
                else:
                    self._backend.key(sub, value, False)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not release %s %r: %s", kind, value, exc)
