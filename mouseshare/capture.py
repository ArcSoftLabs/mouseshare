"""Global mouse capture with conditional suppression (host side).

Thin adapter over pynput. All pynput imports are lazy so the tested core
imports cleanly on platforms without a display. Suppression uses
win32_event_filter on Windows and darwin_intercept on macOS; while
`self.suppressing` is True, captured events do not reach local apps.
"""
import sys
from typing import Callable


class MouseCapture:
    def __init__(
        self,
        on_move: Callable[[int, int], None],
        on_click: Callable[[str, bool], None],
        on_scroll: Callable[[int, int], None],
    ):
        self._on_move = on_move
        self._on_click = on_click
        self._on_scroll = on_scroll
        self.suppressing = False
        self._listener = None

    def start(self) -> None:
        from pynput import mouse

        def handle_move(x, y):
            self._on_move(int(x), int(y))

        def handle_click(x, y, button, pressed):
            self._on_click(button.name, pressed)

        def handle_scroll(x, y, dx, dy):
            self._on_scroll(int(dx), int(dy))

        kwargs = {}
        if sys.platform == "win32":
            def win32_event_filter(msg, data):
                if self.suppressing:
                    self._listener.suppress_event()
                return True

            kwargs["win32_event_filter"] = win32_event_filter
        elif sys.platform == "darwin":
            def darwin_intercept(event_type, event):
                if self.suppressing:
                    return None  # swallow the event
                return event

            kwargs["darwin_intercept"] = darwin_intercept

        self._listener = mouse.Listener(
            on_move=handle_move,
            on_click=handle_click,
            on_scroll=handle_scroll,
            **kwargs,
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
