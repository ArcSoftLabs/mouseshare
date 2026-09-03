"""The queue between the input hook and the socket.

A Windows low-level hook callback must return quickly or the OS starts
dropping events, and a socket write on Wi-Fi does not qualify. Everything
here is about getting off that thread without losing anything that
matters.
"""
import threading
import time  # noqa: F401 - used by the pacing tests below

import pytest

from mouseshare.outbox import Outbox


class Recorder:
    def __init__(self):
        self.sent = []
        self.threads = set()
        self.gate = None
        self.fail_after = None

    def __call__(self, msg):
        if self.gate is not None:
            self.gate.wait(2.0)
        self.threads.add(threading.get_ident())
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError("peer went away")
        self.sent.append(msg)


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def pos(x):
    return {"t": "pos", "x": x, "y": 0}


@pytest.fixture
def rig():
    recorder = Recorder()
    errors = []
    box = Outbox(recorder, on_error=errors.append)
    yield box, recorder, errors
    box.stop()


def test_put_does_not_send_on_the_calling_thread(rig):
    """The whole point: the hook returns before the socket is touched."""
    box, recorder, _ = rig
    box.put(pos(1))
    assert wait_for(lambda: recorder.sent)
    assert recorder.threads and threading.get_ident() not in recorder.threads


def test_everything_is_delivered_in_order(rig):
    box, recorder, _ = rig
    sent = [
        {"t": "enter", "x": 0, "y": 0},
        {"t": "click", "button": "left", "pressed": True},
        {"t": "key", "kind": "char", "value": "a", "pressed": True},
        {"t": "leave"},
    ]
    for msg in sent:
        box.put(msg)
    assert wait_for(lambda: len(recorder.sent) == 4)
    assert recorder.sent == sent


def test_a_backlog_of_movement_collapses_to_the_newest_position(rig):
    """Positions are absolute, so an older one is worthless. Sending the
    whole backlog would replay stale movement and lag further behind the
    faster the user moves."""
    box, recorder, _ = rig
    recorder.gate = threading.Event()
    for x in range(100):
        box.put(pos(x))
    recorder.gate.set()
    assert wait_for(lambda: recorder.sent and recorder.sent[-1] == pos(99))
    assert len(recorder.sent) < 100
    assert all(m["t"] == "pos" for m in recorder.sent)


def test_movement_does_not_collapse_across_a_button_press(rig):
    """A click belongs at the position it was made. Collapsing past it
    would move the press somewhere the user never clicked."""
    box, recorder, _ = rig
    recorder.gate = threading.Event()
    click = {"t": "click", "button": "left", "pressed": True}
    box.put(pos(1))
    box.put(pos(2))
    box.put(click)
    box.put(pos(3))
    box.put(pos(4))
    recorder.gate.set()
    assert wait_for(lambda: len(recorder.sent) == 3)
    assert recorder.sent == [pos(2), click, pos(4)]


def test_ping_is_not_collapsed_with_position_traffic(rig):
    box, recorder, _ = rig
    recorder.gate = threading.Event()
    box.put(pos(1))
    box.put(pos(2))
    box.put({"t": "ping", "seq": 7})
    box.put(pos(3))
    box.put(pos(4))
    recorder.gate.set()
    assert wait_for(lambda: len(recorder.sent) == 3)
    assert recorder.sent == [pos(2), {"t": "ping", "seq": 7}, pos(4)]


def test_keys_are_never_collapsed(rig):
    """A dropped release leaves a modifier stuck down on the other
    machine, which the user cannot type their way out of."""
    box, recorder, _ = rig
    recorder.gate = threading.Event()
    keys = [
        {"t": "key", "kind": "special", "value": "shift_l", "pressed": True},
        {"t": "key", "kind": "char", "value": "a", "pressed": True},
        {"t": "key", "kind": "char", "value": "a", "pressed": False},
        {"t": "key", "kind": "special", "value": "shift_l", "pressed": False},
    ]
    for msg in keys:
        box.put(msg)
    recorder.gate.set()
    assert wait_for(lambda: len(recorder.sent) == 4)
    assert recorder.sent == keys


def test_a_send_failure_is_reported_once(rig):
    box, recorder, errors = rig
    recorder.fail_after = 0
    box.put(pos(1))
    assert wait_for(lambda: errors)
    assert isinstance(errors[0], OSError)


def test_nothing_is_sent_after_a_failure(rig):
    """The link is gone; queueing into it forever would hide that."""
    box, recorder, errors = rig
    recorder.fail_after = 0
    box.put(pos(1))
    assert wait_for(lambda: errors)
    box.put(pos(2))
    time.sleep(0.1)
    assert recorder.sent == []


def test_stop_waits_for_what_is_already_queued(rig):
    box, recorder, _ = rig
    for x in range(5):
        box.put({"t": "key", "kind": "char", "value": str(x), "pressed": True})
    box.stop()
    assert len(recorder.sent) == 5


def test_stop_is_safe_to_call_twice(rig):
    box, _, _ = rig
    box.stop()
    box.stop()


def test_put_after_stop_is_ignored(rig):
    box, recorder, _ = rig
    box.stop()
    box.put(pos(1))
    time.sleep(0.05)
    assert recorder.sent == []


def test_positions_are_paced_so_the_peer_is_not_buried():
    """A gaming mouse reports a thousand times a second; a cursor needs
    about a hundred. Sending every one buries the peer's reader, and the
    surplus lands in its socket buffer where nothing can collapse it --
    the cursor then keeps travelling after the hand has stopped."""
    rec = Recorder()
    box = Outbox(rec, on_error=lambda exc: None, min_pos_interval=0.05)
    for x in range(200):
        box.put({"t": "pos", "x": x, "y": 0})
        time.sleep(0.001)
    time.sleep(0.12)
    box.stop()

    assert len(rec.sent) < 12  # ~0.2s of movement at 20/s, not 200
    assert rec.sent[-1]["x"] == 199  # and it ends where the hand did


def test_pacing_sends_the_newest_position_not_the_one_it_first_saw():
    rec = Recorder()
    box = Outbox(rec, on_error=lambda exc: None, min_pos_interval=0.05)
    box.put({"t": "pos", "x": 1, "y": 0})   # goes at once
    time.sleep(0.01)
    box.put({"t": "pos", "x": 2, "y": 0})   # held back
    box.put({"t": "pos", "x": 3, "y": 0})   # replaces it while held
    time.sleep(0.12)
    box.stop()

    assert [m["x"] for m in rec.sent] == [1, 3]


def test_pacing_never_holds_back_a_click():
    """Movement can wait a few milliseconds. A button cannot: it belongs
    at the position it was pressed, and a release that waits is a button
    left down."""
    rec = Recorder()
    box = Outbox(rec, on_error=lambda exc: None, min_pos_interval=0.05)
    box.put({"t": "pos", "x": 1, "y": 0})
    box.put({"t": "click", "button": "left", "pressed": True})
    time.sleep(0.02)
    box.stop()

    assert [m["t"] for m in rec.sent] == ["pos", "click"]
