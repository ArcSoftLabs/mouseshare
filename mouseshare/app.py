"""Role orchestration: HostApp captures and forwards, ClientApp injects."""
import logging
import threading

from . import protocol
from .config import Config
from .layout import Layout
from .network import MessageClient, MessageServer

log = logging.getLogger("mouseshare")


def screen_size():
    """Actual size of the primary display, via tkinter (stdlib)."""
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    size = (root.winfo_screenwidth(), root.winfo_screenheight())
    root.destroy()
    return size


class HostApp:
    """Runs on the machine the mouse is plugged into."""

    def __init__(self, cfg: Config):
        self.layout = Layout(cfg.screens)
        self.server = MessageServer("0.0.0.0", cfg.port, self._on_message)
        self.remote = False  # True while the cursor is on the client screen
        self._client_pos = (0, 0)  # tracked position on the client screen
        self._lock = threading.Lock()
        from .capture import MouseCapture
        from .inject import Injector

        self.injector = Injector()
        self.capture = MouseCapture(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        host = self.layout.screens["host"]
        self._center = (host.w // 2, host.h // 2)

    def run(self) -> None:
        self.server.start()
        self.capture.start()
        log.info("Host listening on port %d. Waiting for client…", self.server.port)
        threading.Event().wait()  # run until killed

    def _on_message(self, msg: dict) -> None:
        if msg.get("t") == "hello":
            log.info("Client connected: %sx%s", msg.get("w"), msg.get("h"))

    # -- capture callbacks -------------------------------------------------

    def _on_move(self, x: int, y: int) -> None:
        with self._lock:
            if self.remote:
                self._remote_move(x, y)
            else:
                self._local_move(x, y)

    def _local_move(self, x: int, y: int) -> None:
        if not self.server.has_connection():
            return
        host = self.layout.screens["host"]
        # The OS clamps the cursor to the screen, so probe one pixel beyond
        # whichever edge the cursor is touching.
        px = x + (1 if x >= host.w - 1 else -1 if x <= 0 else 0)
        py = y + (1 if y >= host.h - 1 else -1 if y <= 0 else 0)
        if (px, py) == (x, y):
            return
        hit = self.layout.map_exit("host", px, py)
        if hit is None:
            return
        _, cx, cy = hit
        cx, cy = self.layout.clamp("client", cx, cy)
        log.info("Cursor crossed to client at (%d, %d)", cx, cy)
        self.remote = True
        self._client_pos = (cx, cy)
        self.capture.suppressing = True
        self.server.send(protocol.enter(cx, cy))
        self.injector.move_to(*self._center)

    def _remote_move(self, x: int, y: int) -> None:
        dx, dy = x - self._center[0], y - self._center[1]
        if (dx, dy) == (0, 0):
            return  # synthetic event from our own warp
        cx, cy = self._client_pos[0] + dx, self._client_pos[1] + dy
        hit = self.layout.map_exit("client", cx, cy)
        if hit is not None:
            _, hx, hy = hit
            hx, hy = self.layout.clamp("host", hx, hy)
            log.info("Cursor returned to host at (%d, %d)", hx, hy)
            self.remote = False
            self.capture.suppressing = False
            self.server.send(protocol.leave())
            self.injector.move_to(hx, hy)
            return
        self._client_pos = self.layout.clamp("client", cx, cy)
        self.server.send(protocol.move(dx, dy))
        self.injector.move_to(*self._center)

    def _on_click(self, button: str, pressed: bool) -> None:
        if self.remote:
            self.server.send(protocol.click(button, pressed))

    def _on_scroll(self, dx: int, dy: int) -> None:
        if self.remote:
            self.server.send(protocol.scroll(dx, dy))


class ClientApp:
    """Runs on the other machine; injects events received from the host."""

    def __init__(self, cfg: Config):
        if not cfg.peer_host:
            raise SystemExit(
                "No host address. Run 'mouseshare layout' and set the host IP, "
                "or pass it on the command line."
            )
        from .inject import Injector

        self.injector = Injector()
        self.client = MessageClient(cfg.peer_host, cfg.port)

    def run(self) -> None:
        self.client.connect()
        self.client.start_reader(self._on_message)
        w, h = screen_size()
        self.client.send(protocol.hello("client", w, h))
        log.info("Connected to host. Mouse events will be injected here.")
        threading.Event().wait()  # run until killed

    def _on_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "enter":
            self.injector.move_to(msg["x"], msg["y"])
        elif t == "move":
            self.injector.move_by(msg["dx"], msg["dy"])
        elif t == "click":
            self.injector.click(msg["button"], msg["pressed"])
        elif t == "scroll":
            self.injector.scroll(msg["dx"], msg["dy"])
        # "leave": nothing to do; the host stops sending events
