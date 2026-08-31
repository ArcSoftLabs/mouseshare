"""End-to-end pairing between two App instances over loopback.

No display, no second machine, no pynput -- the input layer is faked, so
what is under test is the handshake, the role assignment and the token
lifecycle: the parts that decide whether two real machines will ever talk.
"""
import time

import pytest

from mouseshare import app as app_module
from mouseshare.app import App


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    """Injector and InputCapture would import pynput and grab the machine's
    input. Neither is what these tests are about."""
    class FakeInjector:
        def __init__(self, *a, **k):
            self.released = 0

        @classmethod
        def create(cls):
            return cls()

        def release_all(self):
            self.released += 1

        def __getattr__(self, _name):
            return lambda *a, **k: None

    class FakeCapture:
        def __init__(self, **kwargs):
            self.started = False

        def start(self):
            self.started = True

        def start_remote(self):
            pass

        def stop_remote(self):
            pass

        def stop(self):
            self.started = False

    monkeypatch.setattr(app_module, "Injector", FakeInjector)
    monkeypatch.setattr(app_module, "InputCapture", FakeCapture)


class RecordingInjector:
    """Injection is not free: on a real Mac it is a Quartz event per call,
    far slower than a gaming mouse reports."""

    calls = None
    delay = 0.0

    @classmethod
    def create(cls):
        return cls()

    def move_to(self, x, y):
        time.sleep(self.delay)
        RecordingInjector.calls.append(("move", x, y))

    def click(self, button, pressed):
        RecordingInjector.calls.append(("click", button, pressed))

    def release_all(self):
        RecordingInjector.calls.append(("release",))


def a_client(tmp_path, monkeypatch, delay=0.0):
    RecordingInjector.calls = []
    RecordingInjector.delay = delay
    monkeypatch.setattr(app_module, "Injector", RecordingInjector)
    instance = make_app(tmp_path, "client", 0)
    instance._peer_id, instance._peer_name = "host", "Host"
    instance._become_client()
    return instance


def test_a_burst_of_positions_is_collapsed_before_it_is_injected(
    tmp_path, monkeypatch
):
    """A position is absolute, so an older one says nothing a newer one
    does not. Injected one at a time the backlog only grows, and the
    cursor keeps gliding after the hand has stopped."""
    instance = a_client(tmp_path, monkeypatch, delay=0.002)
    for x in range(500):
        instance._on_message({"t": "pos", "x": x, "y": 10})
    instance._inbox.stop()

    assert len(RecordingInjector.calls) < 100
    assert RecordingInjector.calls[-1] == ("move", 499, 10)


def test_a_click_still_lands_at_the_position_it_was_made_at(
    tmp_path, monkeypatch
):
    """Collapsing must not reach across a click, or the button goes down
    somewhere the user never pressed it."""
    instance = a_client(tmp_path, monkeypatch)
    instance._on_message({"t": "pos", "x": 5, "y": 5})
    instance._on_message({"t": "click", "button": "left", "pressed": True})
    instance._on_message({"t": "pos", "x": 9, "y": 9})
    instance._inbox.stop()

    assert RecordingInjector.calls == [
        ("move", 5, 5), ("click", "left", True), ("move", 9, 9),
    ]


def test_a_failed_injection_does_not_stop_the_ones_after_it(
    tmp_path, monkeypatch
):
    """One event the platform refuses is no reason to stop obeying the
    host -- and the link itself is still perfectly healthy."""
    instance = a_client(tmp_path, monkeypatch)
    boom = {"t": "click", "button": "nonsense", "pressed": True}
    monkeypatch.setattr(
        RecordingInjector, "click",
        lambda self, button, pressed: (_ for _ in ()).throw(ValueError(button)),
    )
    instance._on_message(boom)
    instance._on_message({"t": "pos", "x": 7, "y": 7})
    instance._inbox.stop()

    assert ("move", 7, 7) in RecordingInjector.calls


def test_nothing_is_injected_after_input_has_been_released(
    tmp_path, monkeypatch
):
    """Stopping the queue drains it. Drained after the release, a held
    key goes back down on a machine whose owner cannot then type their
    way out of it."""
    instance = a_client(tmp_path, monkeypatch, delay=0.002)
    for x in range(200):
        instance._on_message({"t": "pos", "x": x, "y": 1})
    instance._do_teardown("peer went away")

    assert RecordingInjector.calls[-1] == ("release",)


def make_app(tmp_path, name, port):
    """An App with discovery disabled -- multicast is tested separately."""
    instance = App(lambda snapshot: None, cfg_path=tmp_path / f"{name}.json")
    instance.cfg.name = name
    instance.cfg.port = port
    instance.state.mark_ready()
    return instance


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def start_listener(instance):
    from mouseshare.network import MessageServer

    instance._server = MessageServer(
        "127.0.0.1", instance.cfg.port, instance._on_message, instance._on_disconnect
    )
    instance._server.start()
    return instance


@pytest.fixture
def pair(tmp_path):
    target = start_listener(make_app(tmp_path, "Target", 0))
    connector = make_app(tmp_path, "Connector", 0)
    yield connector, target
    connector.stop()
    target.stop()


def code_of(target):
    assert wait_for(lambda: target.state.snapshot().get("pairing"))
    return target.state.snapshot()["pairing"]["code"]


def ready_to_type(connector):
    """The connector can only prove a code once the challenge has arrived
    with its nonce. The UI gates the code entry on exactly this, so the
    tests wait for it too rather than racing the network."""
    assert wait_for(lambda: connector._nonce), "no challenge received"


def pair_up(connector, target):
    code = code_of(target)
    ready_to_type(connector)
    connector.submit_code(code)


def test_the_target_shows_a_six_digit_code_when_connected_to(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    code = code_of(target)
    assert len(code) == 6 and code.isdigit()


def test_the_right_code_pairs_both_machines(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)

    assert wait_for(lambda: connector.state.snapshot().get("session"))
    assert connector.state.snapshot()["session"]["role"] == "host"
    assert wait_for(lambda: target.state.snapshot().get("session"))
    assert target.state.snapshot()["session"]["role"] == "client"


def test_pairing_stores_the_same_token_on_both_machines(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: connector.cfg.peers)
    assert wait_for(lambda: target.cfg.peers)

    stored = list(connector.cfg.peers.values())[0].token
    assert stored == list(target.cfg.peers.values())[0].token
    assert len(bytes.fromhex(stored)) == 32


def test_the_wrong_code_does_not_pair(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    real = code_of(target)
    ready_to_type(connector)
    wrong = "000000" if real != "000000" else "111111"
    connector.submit_code(wrong)
    time.sleep(0.5)
    assert connector.state.snapshot().get("session") is None
    assert connector.cfg.peers == {}


def test_the_host_learns_the_peers_monitors(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    # The session is the observable signal that pair_ok arrived; the
    # monitor list is set in the same handler.
    assert wait_for(lambda: connector.state.snapshot().get("session"))
    assert connector._peer_monitors
    assert connector._peer_monitors[0].device_id == target.cfg.device_id


def test_the_layout_shows_both_machines_after_pairing(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(
        lambda: len(connector.state.snapshot()["layout"]["devices"]) == 2
    )


def test_a_disconnect_clears_the_session_on_both_sides(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: target.state.snapshot().get("session"))

    connector.cancel()
    assert wait_for(lambda: target.state.snapshot().get("session") is None)


def test_a_second_pairing_skips_the_code_using_the_stored_token(pair):
    """The whole point of the token: type the code once, ever."""
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: connector.state.snapshot().get("session"))
    connector.cancel()
    assert wait_for(lambda: target.state.snapshot().get("session") is None)

    connector.connect_manually("127.0.0.1", target._server.port)
    assert wait_for(lambda: connector.state.snapshot().get("session"))
    # No code was ever displayed the second time round.
    assert target.state.snapshot().get("pairing") is None


def test_an_unknown_device_claiming_to_be_paired_is_refused(tmp_path):
    """A machine that never paired cannot authenticate with a made-up token."""
    from mouseshare import pairing, protocol
    from mouseshare.network import MessageClient

    target = start_listener(make_app(tmp_path, "Target", 0))
    try:
        target.cfg.peers["intruder"] = None  # not a real Peer: no token
        target.cfg.peers.pop("intruder")

        client = MessageClient("127.0.0.1", target._server.port)
        client.connect()
        got = []
        client.start_reader(got.append)
        client.send(protocol.pair_request("intruder", "Rogue"))
        assert wait_for(lambda: got)
        nonce = got[0]["nonce"]

        client.send(protocol.auth("intruder", pairing.proof(
            b"\x00" * 32, nonce, "intruder", target.cfg.device_id
        )))
        assert wait_for(lambda: any(m["t"] == "pair_err" for m in got))
        assert target.state.snapshot().get("session") is None
        client.close()
    finally:
        target.stop()
