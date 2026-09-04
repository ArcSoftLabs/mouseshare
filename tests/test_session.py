import pytest

from mouseshare.layout import Layout, Monitor
from mouseshare.session import ClientSession, HostSession, pick_winner

PC = "pc"
MAC = "mac"


def a_layout() -> Layout:
    return Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(MAC, "0", 0, 0, 1728, 1117, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (1920, 0)},
    )


class FakeCapture:
    def __init__(self):
        self.suppressing = False
        self.stops = 0
        self.anchor = None

    def start_remote(self, anchor, limit):
        self.suppressing = True
        self.anchor = anchor
        self.limit = limit

    def stop_remote(self):
        self.suppressing = False
        self.stops += 1


class FakeInjector:
    def __init__(self):
        self.calls = []
        self.releases = 0

    def move_to(self, x, y):
        self.calls.append(("move", x, y))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def click(self, name, pressed):
        self.calls.append(("click", name, pressed))

    def key(self, kind, value, pressed):
        self.calls.append(("key", kind, value, pressed))

    def release_all(self):
        self.releases += 1


class FakeSender:
    def __init__(self):
        self.sent = []
        self.broken = False

    def __call__(self, peer_id, msg):
        if self.broken:
            raise OSError("peer went away")
        self.sent.append(msg)


def a_host():
    capture, injector, send = FakeCapture(), FakeInjector(), FakeSender()
    host = HostSession(
        layout=a_layout(),
        local_id=PC,
        peer_id=MAC,
        capture=capture,
        injector=injector,
        send=send,
    )
    return host, capture, injector, send


def kinds(sender):
    return [m["t"] for m in sender.sent]


# -- crossing ----------------------------------------------------------------


def test_touching_the_shared_edge_hands_the_cursor_to_the_peer():
    host, capture, _, send = a_host()
    host.on_move(1919, 500)
    assert host.remote is True
    assert capture.suppressing is True
    assert send.sent[-1] == {"t": "enter", "x": 0, "y": 500}


def test_the_cursor_is_parked_in_the_middle_of_the_screen_it_left():
    """Parked on the edge it just touched, the OS clamps every further
    move: pushing right yields no movement at all, and near a step in the
    desktop outline it jumps sideways. Both read as an uncontrollable
    cursor. The middle of the screen has room in every direction."""
    host, capture, _, _ = a_host()
    host.on_move(1919, 500)
    assert capture.anchor == (960, 540)


def test_the_capture_is_told_how_big_a_jump_can_still_be_real_movement():
    """Anything larger is the cursor being moved for us -- the event in
    flight when suppression began still carries the pre-park position."""
    host, capture, _, _ = a_host()
    host.on_move(1919, 500)
    assert capture.limit == 1080 // 4


def test_the_park_uses_the_screen_the_cursor_actually_left():
    """Two local screens; the one it left is not always the primary."""
    layout = Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(PC, "1", 0, 1080, 1280, 720, primary=False),
            Monitor(MAC, "0", 0, 0, 1728, 1117, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (1920, 0)},
    )
    capture, injector, send = FakeCapture(), FakeInjector(), FakeSender()
    host = HostSession(
        layout=layout, local_id=PC, peer_id=MAC,
        capture=capture, injector=injector, send=send,
    )
    host.on_move(1919, 300)
    assert capture.anchor == (960, 540)


def test_moving_inside_the_host_screen_does_not_hand_over():
    host, capture, _, send = a_host()
    host.on_move(500, 500)
    assert host.remote is False
    assert capture.suppressing is False
    assert send.sent == []


def test_touching_an_edge_with_nothing_beyond_it_does_not_hand_over():
    host, capture, _, send = a_host()
    host.on_move(0, 500)  # left edge; the Mac is on the right
    assert host.remote is False
    assert capture.suppressing is False


def test_while_remote_movement_is_forwarded_as_absolute_peer_coordinates():
    host, _, _, send = a_host()
    host.on_move(1919, 500)
    host.on_delta(10, 5)
    assert send.sent[-1] == {"t": "pos", "x": 10, "y": 505}


def test_crossing_back_returns_control_and_places_the_local_cursor():
    host, capture, injector, send = a_host()
    host.on_move(1919, 500)
    host.on_delta(-10, 0)
    assert host.remote is False
    assert capture.suppressing is False
    assert kinds(send)[-1] == "leave"
    assert ("move", 1910, 500) in injector.calls


def test_the_returning_cursor_is_put_clear_of_the_edge_it_arrived_on():
    """On the boundary pixel, the probe one pixel past it hits the peer
    again -- and our own injected move is enough to trigger that, so the
    cursor bounces straight back without the user touching anything."""
    host, _, injector, _ = a_host()
    host.on_move(1919, 500)
    host.on_delta(-1, 0)
    assert ("move", 1917, 500) in injector.calls

    host.on_move(1917, 500)  # the injected move, seen by the capture
    assert host.remote is False


def test_clicks_and_keys_only_travel_while_remote():
    host, _, _, send = a_host()
    host.on_click("left", True)
    host.on_key("char", "a", True)
    assert send.sent == []

    host.on_move(1919, 500)
    host.on_click("left", True)
    host.on_key("special", "shift_l", True)
    assert kinds(send)[-3:] == ["enter", "click", "key"]


def test_the_cursor_is_clamped_onto_the_peer_screen():
    host, _, _, send = a_host()
    host.on_move(1919, 500)
    host.on_delta(0, 10_000)
    assert send.sent[-1] == {"t": "pos", "x": 0, "y": 1116}


# -- safety ------------------------------------------------------------------


def test_a_disconnect_while_remote_releases_suppression():
    """The one failure the user cannot recover from inside the app: a dead
    keyboard means they cannot type to fix it."""
    host, capture, injector, _ = a_host()
    host.on_move(1919, 500)
    assert capture.suppressing is True
    host.on_disconnect("eof")
    assert capture.suppressing is False
    assert host.remote is False
    assert ("move", 960, 540) in injector.calls


def test_escape_while_remote_leaves_and_returns_near_the_modelled_peer_position():
    host, capture, injector, send = a_host()
    host.on_move(1919, 500)
    host.on_delta(0, 100)
    host.on_escape()
    assert host.remote is False
    assert capture.suppressing is False
    assert kinds(send)[-1] == "leave"
    assert injector.calls[-1] == ("move", 1917, 600)


def test_escape_while_local_is_a_no_op():
    host, capture, injector, send = a_host()
    host.on_escape()
    assert capture.stops == 0
    assert injector.calls == []
    assert send.sent == []


def test_suppression_is_released_even_when_notifying_the_peer_fails():
    host, capture, _, send = a_host()
    host.on_move(1919, 500)
    send.broken = True
    host.on_disconnect("error")
    assert capture.suppressing is False


def test_a_failed_send_while_remote_releases_suppression_immediately():
    host, capture, _, send = a_host()
    host.on_move(1919, 500)
    send.broken = True
    host.on_delta(5, 0)
    assert capture.suppressing is False
    assert host.remote is False


def test_disconnecting_when_not_remote_is_harmless():
    host, capture, _, _ = a_host()
    host.on_disconnect("eof")
    assert capture.suppressing is False
    assert host.remote is False


def test_stopping_releases_suppression():
    host, capture, _, _ = a_host()
    host.on_move(1919, 500)
    host.stop()
    assert capture.suppressing is False


# -- the client side ---------------------------------------------------------


def test_the_client_injects_what_it_is_told_to():
    injector = FakeInjector()
    client = ClientSession(injector, lambda msg: None)
    client.on_message({"t": "enter", "x": 5, "y": 6})
    client.on_message({"t": "pos", "x": 7, "y": 8})
    client.on_message({"t": "click", "button": "left", "pressed": True})
    client.on_message({"t": "scroll", "dx": 0, "dy": -1})
    client.on_message({"t": "key", "kind": "char", "value": "a", "pressed": True})
    assert injector.calls == [
        ("move", 5, 6), ("move", 7, 8),
        ("click", "left", True), ("scroll", 0, -1),
        ("key", "char", "a", True),
    ]


def test_the_client_lets_go_of_everything_when_the_cursor_leaves():
    injector = FakeInjector()
    client = ClientSession(injector, lambda msg: None)
    client.on_message({"t": "leave"})
    assert injector.releases == 1


def test_the_client_lets_go_of_everything_when_the_host_vanishes():
    """A chord held at the moment the link dropped must not stay down."""
    injector = FakeInjector()
    client = ClientSession(injector, lambda msg: None)
    client.on_message({"t": "key", "kind": "special", "value": "ctrl_l", "pressed": True})
    client.on_disconnect("eof")
    assert injector.releases == 1


def test_the_client_ignores_messages_it_does_not_understand():
    injector = FakeInjector()
    ClientSession(injector, lambda msg: None).on_message({"t": "something_new"})
    assert injector.calls == []


# -- role arbitration --------------------------------------------------------


def test_simultaneous_connections_are_resolved_by_device_id():
    """Both machines can hit Connect at once. Exactly one link survives,
    and both sides must agree which -- before either suppresses input."""
    assert pick_winner(initiator_a="aaa", initiator_b="bbb") == "aaa"
    assert pick_winner(initiator_a="bbb", initiator_b="aaa") == "aaa"


def test_the_tie_break_is_symmetric_so_both_machines_reach_it_independently():
    for a, b in [("m1", "m2"), ("zz", "aa"), ("abc", "abd")]:
        assert pick_winner(a, b) == pick_winner(b, a)


def test_a_device_cannot_tie_with_itself():
    with pytest.raises(ValueError):
        pick_winner("same", "same")
