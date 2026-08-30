"""Mouse event injection (client side, also used by the host to warp)."""


class Injector:
    def __init__(self) -> None:
        from pynput import mouse

        self._mouse = mouse
        self._controller = mouse.Controller()

    def move_to(self, x: int, y: int) -> None:
        self._controller.position = (x, y)

    def move_by(self, dx: int, dy: int) -> None:
        self._controller.move(dx, dy)

    def position(self):
        return self._controller.position

    def click(self, button: str, pressed: bool) -> None:
        btn = getattr(self._mouse.Button, button, self._mouse.Button.left)
        if pressed:
            self._controller.press(btn)
        else:
            self._controller.release(btn)

    def scroll(self, dx: int, dy: int) -> None:
        self._controller.scroll(dx, dy)
