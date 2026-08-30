"""Session logic: who is driving, where the cursor is, and when to let go.

`HostSession` runs on the machine whose keyboard and mouse are in use. It
watches for the cursor leaving a screen edge, suppresses local input while
the cursor is on the peer, and forwards events there.

`ClientSession` runs on the other machine and does nothing but inject what
it is told to, then release everything the moment the host stops talking.

The rule that shapes both: **local input is released before anything
else.** A machine left suppressing its own keyboard cannot be rescued from
inside the app, because the user cannot type.
"""
import logging
from typing import Callable, Optional, Tuple

from . import protocol
from .layout import Layout

log = logging.getLogger("mouseshare")


def pick_winner(initiator_a: str, initiator_b: str) -> str:
    """Which of two simultaneous connections survives.

    Both machines can press Connect at the same instant, leaving each with
    an outbound and an inbound link and each believing it is the host.
    Ordering on device id is arbitrary but identical on both sides, so they
    reach the same answer without another round trip -- and they reach it
    before either enables suppression.
    """
    if initiator_a == initiator_b:
        raise ValueError("two devices cannot share an id")
    return min(initiator_a, initiator_b)


class HostSession:
    def __init__(
        self,
        layout: Layout,
        local_id: str,
        peer_id: str,
        capture,
        injector,
        send: Callable[[dict], None],
    ):
        self.layout = layout
        self.local_id = local_id
        self.peer_id = peer_id
        self.capture = capture
        self.injector = injector
        self._send = send
        self.remote = False
        self._peer_pos = (0, 0)

    # -- capture callbacks ---------------------------------------------------

    def on_move(self, x: int, y: int) -> None:
        """Not remote: watch for the cursor pressing against a shared edge."""
        if not getattr(self, "_saw_a_move", False):
            self._saw_a_move = True
            log.debug("capture is delivering moves, first at (%d,%d)", x, y)
        if self.remote:
            return
        # The OS clamps the cursor inside the screen, so a cursor "leaving"
        # only ever sits *on* the edge. Probe one pixel past it.
        probe = self._probe(x, y)
        if probe is None:
            return
        hit = self.layout.map_exit(self.local_id, *probe)
        log.debug("edge at (%d,%d) probe=%s hit=%s", x, y, probe, hit)
        if hit is None or hit[0] != self.peer_id:
            return
        _, px, py = hit
        px, py = self.layout.clamp(self.peer_id, px, py)
        self.remote = True
        self._peer_pos = (px, py)
        self.capture.start_remote(self._park(x, y))
        log.info("cursor crossed to %s at (%d, %d)", self.peer_id, px, py)
        self._forward(protocol.enter(px, py))

    def _park(self, x: int, y: int) -> Tuple[int, int]:
        """Where to hold the real cursor while it is away.

        Not where it left. The OS clamps the cursor to the desktop, so an
        anchor on the edge reports nothing at all for further movement in
        that direction, and where the desktop outline steps in or out the
        clamp slides the position sideways and invents a large delta. The
        middle of the screen it left has room on every side.
        """
        for m in self.layout.monitors:
            if m.device_id != self.local_id:
                continue
            if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
                return (m.x + m.w // 2, m.y + m.h // 2)
        return (x, y)

    def _probe(self, x: int, y: int) -> Optional[tuple]:
        for m in self.layout.monitors:
            if m.device_id != self.local_id:
                continue
            if not (m.x <= x < m.x + m.w and m.y <= y < m.y + m.h):
                continue
            px = x + (1 if x >= m.x + m.w - 1 else -1 if x <= m.x else 0)
            py = y + (1 if y >= m.y + m.h - 1 else -1 if y <= m.y else 0)
            return None if (px, py) == (x, y) else (px, py)
        return None

    def on_delta(self, dx: int, dy: int) -> None:
        """Remote: move the tracked peer cursor, and watch for the return."""
        if not self.remote:
            return
        nx, ny = self._peer_pos[0] + dx, self._peer_pos[1] + dy
        hit = self.layout.map_exit(self.peer_id, nx, ny)
        if hit is not None and hit[0] == self.local_id:
            _, hx, hy = hit
            hx, hy = self.layout.clamp(self.local_id, hx, hy)
            log.info("cursor returned to %s at (%d, %d)", self.local_id, hx, hy)
            self._release()
            self._forward(protocol.leave())
            self.injector.move_to(hx, hy)
            return
        self._peer_pos = self.layout.clamp(self.peer_id, nx, ny)
        self._forward(protocol.pos(*self._peer_pos))

    def on_click(self, button: str, pressed: bool) -> None:
        if self.remote:
            self._forward(protocol.click(button, pressed))

    def on_scroll(self, dx: int, dy: int) -> None:
        if self.remote:
            self._forward(protocol.scroll(dx, dy))

    def on_key(self, kind: str, value: str, pressed: bool) -> None:
        if not self.remote:
            return
        msg = (
            protocol.key_special(value, pressed)
            if kind == "special"
            else protocol.key_char(value, pressed)
        )
        self._forward(msg)

    # -- teardown ------------------------------------------------------------

    def on_disconnect(self, reason: str) -> None:
        log.info("peer disconnected (%s)", reason)
        self._release()

    def stop(self) -> None:
        self._release()

    def _release(self) -> None:
        """Give local input back. Safe to call repeatedly."""
        if self.remote:
            self.remote = False
            self.capture.stop_remote()

    def _forward(self, msg: dict) -> None:
        """Send, and treat a failure as the peer being gone -- releasing
        input first, because that is the part that cannot wait."""
        try:
            self._send(msg)
        except OSError as exc:
            log.warning("send failed (%s); releasing input", exc)
            self._release()


class ClientSession:
    """Injects what the host sends. Holds no layout of its own."""

    def __init__(self, injector):
        self.injector = injector

    def on_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t in ("enter", "pos"):
            self.injector.move_to(msg["x"], msg["y"])
        elif t == "click":
            self.injector.click(msg["button"], msg["pressed"])
        elif t == "scroll":
            self.injector.scroll(msg["dx"], msg["dy"])
        elif t == "key":
            self.injector.key(msg["kind"], msg["value"], msg["pressed"])
        elif t == "leave":
            self.injector.release_all()

    def on_disconnect(self, reason: str) -> None:
        log.info("host disconnected (%s); releasing held input", reason)
        self.injector.release_all()
