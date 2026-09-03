"""The single owner of everything the UI renders.

Discovery, the socket reader and the input listeners all run on their own
threads, and none of them may touch the webview. They mutate state here
instead; this class serialises the changes, stamps each with a revision,
and hands a complete snapshot to one delivery function.

One shape in, one render out. The alternative -- each thread patching the
DOM for its own event -- is where this kind of UI usually rots.
"""
import copy
import logging
import threading
from typing import Any, Callable, Dict

log = logging.getLogger("mouseshare")


class StateOwner:
    def __init__(self, deliver: Callable[[dict], None], initial: Dict[str, Any]):
        self._deliver = deliver
        self._state = copy.deepcopy(initial)
        self._lock = threading.RLock()
        # Delivery happens outside the state lock (calling into the webview
        # while holding it invites a lock inversion), so it gets its own --
        # otherwise two threads can be inside evaluate_js at once.
        self._deliver_lock = threading.Lock()
        self._revision = 0
        self._ready = False
        self._last_delivered = -1

    # -- reading -------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {**copy.deepcopy(self._state), "revision": self._revision}

    @staticmethod
    def snapshot_at(revision: int, state: Dict[str, Any]) -> dict:
        """Build a snapshot by hand. Only useful for testing the guard."""
        return {**state, "revision": revision}

    # -- writing -------------------------------------------------------------

    def set(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
            self._revision += 1
            snapshot = self.snapshot()
        self.deliver(snapshot)

    def set_async(self, **fields: Any) -> None:
        """Update immediately, but deliver away from a time-sensitive caller."""
        with self._lock:
            self._state.update(fields)
            self._revision += 1
            snapshot = self.snapshot()
        threading.Thread(
            target=self.deliver,
            args=(snapshot,),
            name="mouseshare-state-delivery",
            daemon=True,
        ).start()

    def mark_ready(self) -> dict:
        """Called once by the page when its listener exists. Returns the
        current state, so anything that changed while it was loading is
        picked up rather than lost."""
        with self._lock:
            self._ready = True
            snapshot = self.snapshot()
            self._last_delivered = snapshot["revision"]
        return snapshot

    def deliver(self, snapshot: dict) -> None:
        # The revision check and the delivery happen under one lock, so two
        # concurrent publishers cannot both pass the check and then deliver
        # in the opposite order.
        with self._deliver_lock:
            with self._lock:
                if not self._ready:
                    return  # the page has no listener yet
                if snapshot["revision"] <= self._last_delivered:
                    return  # a slower thread caught up; never rewind the UI
                self._last_delivered = snapshot["revision"]
            try:
                self._deliver(snapshot)
            except Exception as exc:  # noqa: BLE001
                # A closed window invalidates the JS target mid-push. That is
                # not a reason to take the session down with it.
                log.debug("state delivery failed: %s", exc)
