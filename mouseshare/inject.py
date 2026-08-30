"""Mouse event injection (client side, and host-side cursor placement)."""


class Injector:
    def __init__(self) -> None:
        from pynput import mouse

        self._mouse = mouse
        self._controller = mouse.Controller()

    def move_to(self, x: int, y: int) -> None:
        self._controller.position = (x, y)

    def click(self, button: str, pressed: bool) -> None:
        btn = getattr(self._mouse.Button, button, None)
        if btn is None:
            return  # button not supported on this platform (e.g. x1 on macOS)
        if pressed:
            self._controller.press(btn)
        else:
            self._controller.release(btn)

    def scroll(self, dx: int, dy: int) -> None:
        self._controller.scroll(dx, dy)
