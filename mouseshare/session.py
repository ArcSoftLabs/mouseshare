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
import time
from typing import Callable, Optional, Tuple

from . import protocol
from .layout import Layout

log = logging.getLogger("mouseshare")

# How far inside its own screen a returning cursor is placed. The edge
# probe reaches one pixel past the boundary, so anything more than that
# stops the cursor bouncing straight back to the peer.
EDGE_MARGIN = 2


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
        on_remote_change: Callable[[bool], None] = lambda remote: None,
    ):
        self.layout = layout
        self.local_id = local_id
        self.peer_id = peer_id
        self.capture = capture
        self.injector = injector
        self._send = send
        self._on_remote_change = on_remote_change
        self.remote = False
        self._peer_pos = (0, 0)
        self._return_anchor = None
        self._m_since = None  # temporary; see _log_movement

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
        self._on_remote_change(True)
        self._peer_pos = (px, py)
        park = self._park(x, y)
        self._return_anchor = park[0]
        self.capture.start_remote(*park)
        log.info("cursor crossed to %s at (%d, %d)", self.peer_id, px, py)
        self._forward(protocol.enter(px, py))

    def _park(self, x: int, y: int) -> Tuple[Tuple[int, int], int]:
        """Where to hold the real cursor while it is away, and the largest
        offset from there that can still be real movement.

        Not where it left. The OS clamps the cursor to the desktop, so an
        anchor on the edge reports nothing at all for further movement in
        that direction, and where the desktop outline steps in or out the
        clamp slides the position sideways and invents a large delta. The
        middle of the screen it left has room on every side, and no hand
        covers a quarter of that screen inside one hook event.
        """
        for m in self.layout.monitors:
            if m.device_id != self.local_id:
                continue
            if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
                return (m.x + m.w // 2, m.y + m.h // 2), min(m.w, m.h) // 4
        return (x, y), 1

    def _inset(self, x: int, y: int) -> Tuple[int, int]:
        """Pull a returning cursor clear of the edge it arrived on.

        Left on the boundary pixel, the probe one pixel past it hits the
        peer again -- and the injected move putting it there is itself
        enough to trigger that, so the cursor leaves again before the user
        has touched anything.
        """
        for m in self.layout.monitors:
            if m.device_id != self.local_id:
                continue
            if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
                return (
                    min(max(x, m.x + EDGE_MARGIN), m.x + m.w - 1 - EDGE_MARGIN),
                    min(max(y, m.y + EDGE_MARGIN), m.y + m.h - 1 - EDGE_MARGIN),
                )
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
        self._log_movement(dx, dy)
        nx, ny = self._peer_pos[0] + dx, self._peer_pos[1] + dy
        hit = self.layout.map_exit(self.peer_id, nx, ny)
        if hit is not None and hit[0] == self.local_id:
            _, hx, hy = hit
            hx, hy = self._inset(*self.layout.clamp(self.local_id, hx, hy))
            log.info("cursor returned to %s at (%d, %d)", self.local_id, hx, hy)
            self._release((hx, hy))
            self._forward(protocol.leave())
            return
        self._peer_pos = self.layout.clamp(self.peer_id, nx, ny)
        self._forward(protocol.pos(*self._peer_pos))

    def _log_movement(self, dx: int, dy: int) -> None:
        """Temporary instrumentation: how fast, and in how big a step.

        Counting events alone cannot tell a rate ceiling from a mouse that
        simply polls that often. The size of the steps can: if the biggest
        delta grows when the hand moves faster, the host is delivering the
        speed and the lag is downstream of it.
        """
        now = time.monotonic()
        if self._m_since is None:
            self._m_since, self._m_n, self._m_max, self._m_travel = now, 0, 0, 0
        self._m_n += 1
        self._m_max = max(self._m_max, abs(dx), abs(dy))
        self._m_travel += abs(dx) + abs(dy)
        if now - self._m_since >= 1.0:
            log.debug(
                "movement: %d events, biggest step %d px, %d px travelled",
                self._m_n, self._m_max, self._m_travel,
            )
            self._m_since, self._m_n, self._m_max, self._m_travel = now, 0, 0, 0

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

    def on_escape(self) -> None:
        """Return local control without forwarding the gesture."""
        if not self.remote:
            return
        self._release(self._escape_target())
        self._forward(protocol.leave())

    def on_capture_lost(self) -> None:
        """A dead listener is equivalent to the peer-side cursor leaving."""
        if not self.remote:
            return
        self._release()
        self._forward(protocol.leave())

    def _escape_target(self) -> Tuple[int, int]:
        try:
            vx, vy = self.layout.to_plane(self.peer_id, *self._peer_pos)
            point = self.layout.from_plane(self.local_id, vx, vy)
            return self._inset(*self.layout.clamp(self.local_id, *point))
        except (KeyError, ValueError):
            return self._return_anchor

    # -- teardown ------------------------------------------------------------

    def on_disconnect(self, reason: str) -> None:
        log.info("peer disconnected (%s)", reason)
        self._release()

    def stop(self) -> None:
        self._release()

    def _release(self, target=None) -> None:
        """Give local input back. Safe to call repeatedly."""
        if self.remote:
            self.remote = False
            self.capture.stop_remote()
            point = target or self._return_anchor
            if point is not None:
                self.injector.move_to(*self._inset(*point))
            self._on_remote_change(False)

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
