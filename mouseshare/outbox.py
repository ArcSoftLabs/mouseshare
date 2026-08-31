"""A queue between something fast and something slow, used at both ends.

On the host it stands between the input hook and the socket; on the
client, between the socket and injection, which is slower still. Both
have the same shape: a producer that must not be made to wait, and a
consumer that cannot keep up with a mouse at full tilt.


A Windows low-level hook callback runs on the thread that is delivering
the event, and Windows silently stops calling a hook that takes too long.
Writing to a socket from inside it is far too slow: a gaming mouse
reports a thousand times a second and a Wi-Fi link answers in
milliseconds, so the hook spends its whole budget waiting and the OS
starts dropping movement.

So the hook only appends here and returns. One thread does the sending.

Movement is collapsed on the way in. Positions are absolute, so an older
one carries no information the newer one lacks -- sending a backlog would
replay stale movement and fall further behind the faster the user moves.
Nothing else is collapsed, and nothing is reordered: a click belongs at
the position it was made, and a dropped key release leaves a modifier
stuck down on the other machine.
"""
import logging
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger("mouseshare")

REPORT_EVERY = 5.0


class Outbox:
    def __init__(
        self,
        send: Callable[[dict], None],
        on_error: Callable[[Exception], None],
        min_pos_interval: float = 0.0,
    ):
        self._send = send
        self._on_error = on_error
        self._min_pos_interval = min_pos_interval
        self._last_pos = 0.0
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._running = True
        self._queued = 0
        self._sent = 0
        self._thread = threading.Thread(
            target=self._run, name="mouseshare-outbox", daemon=True
        )
        self._thread.start()

    def put(self, msg: dict) -> None:
        with self._ready:
            if not self._running:
                return
            self._queued += 1
            if (
                msg.get("t") == "pos"
                and self._queue
                and self._queue[-1].get("t") == "pos"
            ):
                self._queue[-1] = msg
            else:
                self._queue.append(msg)
            self._ready.notify()

    def stop(self) -> None:
        """Stop after sending what is already queued."""
        with self._ready:
            if not self._running:
                return
            self._running = False
            self._ready.notify()
        self._thread.join(timeout=2.0)

    def _take(self) -> Optional[dict]:
        with self._ready:
            while self._running and not self._queue:
                self._ready.wait(0.2)
            if not self._queue:
                return None
            if self._queue[0].get("t") == "pos":
                self._wait_for_the_slot()
                if not self._queue:
                    return None
                self._last_pos = time.monotonic()
            return self._queue.pop(0)

    def _wait_for_the_slot(self) -> None:
        """Hold movement back to the pace the far end can actually take.

        Caller holds the lock. Waiting here rather than after popping is
        the point: positions arriving meanwhile replace the one at the
        head, so what finally goes is the newest, and the surplus never
        reaches the peer's socket buffer -- the one place in this system
        where nothing can collapse it.

        Only movement waits. A click or a key at the head goes straight
        out, and a stop stops waiting, so the last position always
        follows the hand that stopped.
        """
        due = self._last_pos + self._min_pos_interval
        now = time.monotonic()
        while self._running and now < due:
            self._ready.wait(due - now)
            now = time.monotonic()

    def _run(self) -> None:
        last_report = 0.0
        while True:
            msg = self._take()
            if msg is None:
                return
            try:
                self._send(msg)
            except Exception as exc:  # noqa: BLE001 - any failure means gone
                with self._ready:
                    self._running = False
                    self._queue.clear()
                self._on_error(exc)
                return
            self._sent += 1
            last_report = self._report(last_report)

    def _report(self, last_report: float) -> float:
        if not log.isEnabledFor(logging.DEBUG):
            return last_report
        now = time.monotonic()
        if now - last_report < REPORT_EVERY:
            return last_report
        with self._ready:
            depth = len(self._queue)
        log.debug(
            "outbox: %d queued, %d sent, %d collapsed, depth %d",
            self._queued, self._sent, self._queued - self._sent, depth,
        )
        return now
