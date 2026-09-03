"""The failure modes that leave a machine unusable, and the races that
only show up under load.
"""
import threading
import time

from mouseshare import protocol, session
from mouseshare.inject import Injector
from mouseshare.layout import Layout, Monitor
from mouseshare.network import MessageClient, MessageServer

PC, MAC = "pc", "mac"


# -- held input must always drain -------------------------------------------


class BrokenBackend:
    """A backend where one key refuses to release, as a wedged driver would."""

    def __init__(self, broken):
        self.released = []
        self._broken = broken

    def move_to(self, x, y):
        pass

    def scroll(self, dx, dy):
        pass

    def button(self, name, pressed):
        if not pressed and name in self._broken:
            raise OSError("button release failed")
        self.released.append(("button", name, pressed))
        return True

    def key(self, kind, value, pressed):
        if not pressed and value in self._broken:
            raise OSError("key release failed")
        self.released.append(("key", value, pressed))
        return True


def test_one_key_that_refuses_to_release_does_not_strand_the_others():
    """release_all is the last line of defence. If it gives up on the first
    failure, every key after it stays down and the user cannot type."""
    backend = BrokenBackend(broken={"b"})
    inj = Injector(backend)
    for ch in "abc":
        inj.key("char", ch, True)
    inj.click("left", True)

    inj.release_all()

    released = {v for kind, v, pressed in backend.released if not pressed}
    assert {"a", "c", "left"} <= released
    assert inj.held() == set(), "held set must be cleared even after a failure"


def test_release_all_clears_state_even_if_every_release_fails():
    backend = BrokenBackend(broken={"a"})
    inj = Injector(backend)
    inj.key("char", "a", True)
    inj.release_all()
    assert inj.held() == set()


def test_presses_from_another_thread_do_not_break_release_all():
    """The reader thread injects while the disconnect path drains."""
    class Backend:
        def move_to(self, x, y): pass
        def scroll(self, dx, dy): pass
        def button(self, name, pressed): return True
        def key(self, kind, value, pressed): return True

    inj = Injector(Backend())
    stop = threading.Event()

    def presser():
        i = 0
        while not stop.is_set():
            inj.key("char", chr(97 + i % 26), True)
            i += 1

    t = threading.Thread(target=presser, daemon=True)
    t.start()
    try:
        for _ in range(200):
            inj.release_all()  # must never raise "set changed size"
    finally:
        stop.set()
        t.join(timeout=2)


# -- the wire must not interleave -------------------------------------------


def test_concurrent_sends_do_not_corrupt_the_stream():
    """Mouse and keyboard listeners are separate threads and both forward.
    Two sendall calls interleaving would split a JSON line in half and the
    peer would drop the connection mid-session."""
    received = []
    done = threading.Event()

    def on_message(msg):
        received.append(msg)
        keys = sum(item["t"] == "key" for item in received)
        if keys == 200 and any(item["t"] == "pos" for item in received):
            done.set()

    server = MessageServer("127.0.0.1", 0, on_message)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()

        def spam(kind):
            for i in range(200):
                client.send(
                    protocol.pos(i, i) if kind else protocol.key_char("x" * 40, True)
                )

        threads = [threading.Thread(target=spam, args=(k,)) for k in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert done.wait(timeout=5), f"only {len(received)} messages arrived intact"
        assert sum(msg["t"] == "key" for msg in received) == 200
        positions = [msg["x"] for msg in received if msg["t"] == "pos"]
        assert positions == sorted(positions)
        assert positions[-1] <= 199
        client.close()
    finally:
        server.stop()


def test_position_flood_does_not_block_leave():
    handled = []
    release = threading.Event()

    def on_message(msg):
        if not handled:
            release.wait(2.0)
        handled.append(msg)

    server = MessageServer("127.0.0.1", 0, on_message)
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    try:
        for i in range(10_000):
            client.send(protocol.pos(i, i))
        client.send(protocol.leave())
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and not any(m["t"] == "leave" for m in handled):
            time.sleep(0.01)
        assert any(m["t"] == "leave" for m in handled)
    finally:
        client.close()
        server.stop()


# -- the layout must never persist an overlap -------------------------------


def a_layout():
    return Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(MAC, "0", 0, 0, 1728, 1117, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (1920, 0)},
    )


def test_a_drop_that_would_overlap_is_snapped_clear_not_stored():
    """Overlapping screens make cursor ownership ambiguous, which sends
    input to the wrong machine."""
    layout = a_layout()
    layout.set_offset(MAC, (900, 0))  # squarely on top of the PC
    assert not layout.can_place(MAC, (900, 0))
    layout.snap_device(MAC, PC)
    assert layout.can_place(MAC, layout.offsets[MAC])


# -- simultaneous connections ------------------------------------------------


def test_both_machines_pick_the_same_winner_independently():
    a, b = "aaa", "bbb"
    assert session.pick_winner(a, b) == session.pick_winner(b, a)


def test_the_loser_of_a_simultaneous_connect_is_the_one_that_yields():
    """Whichever machine each side is on, they must agree, or both suppress
    input at once and neither can be driven."""
    a, b = "m-alpha", "m-beta"
    winner = session.pick_winner(a, b)
    assert winner == a
    # From b's point of view the same call gives the same answer.
    assert session.pick_winner(b, a) == a
