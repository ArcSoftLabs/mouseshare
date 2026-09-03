"""End-to-end pairing between two App instances over loopback.

No display, no second machine, no pynput -- the input layer is faked, so
what is under test is the handshake, the role assignment and the token
lifecycle: the parts that decide whether two real machines will ever talk.
"""
import json
import os
import socket
import threading
import time

import pytest

from mouseshare import app as app_module
from mouseshare import config, monitors
from mouseshare.app import App
from mouseshare.layout import Monitor


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    """Injector and InputCapture would import pynput and grab the machine's
    input. Neither is what these tests are about."""
    class FakeInjector:
        def __init__(self, *a, **k):
            self.released = 0
            self.held = set()

        @classmethod
        def create(cls):
            return cls()

        def release_all(self):
            self.released += 1
            self.held.clear()

        def key(self, kind, value, pressed):
            item = (kind, value)
            if pressed:
                self.held.add(item)
            else:
                self.held.discard(item)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    class FakeCapture:
        def __init__(self, **kwargs):
            self.started = False
            self.suppressing = False
            self.stop_remote_calls = 0

        def start(self):
            self.started = True

        def start_remote(self, *_args):
            self.suppressing = True

        def stop_remote(self):
            self.stop_remote_calls += 1
            self.suppressing = False

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


def test_setting_escape_key_rejects_invalid_values(tmp_path):
    instance = make_app(tmp_path, "settings", 0)
    with pytest.raises(ValueError):
        instance.set_escape_key("caps_lock")


def test_setting_escape_key_applies_to_a_running_capture(tmp_path):
    instance = make_app(tmp_path, "settings", 0)
    calls = []
    instance._capture = type(
        "Capture", (), {"set_escape_key": lambda self, value: calls.append(value)}
    )()
    snapshot = instance.set_escape_key("shift")
    assert instance.cfg.escape_key == "shift"
    assert snapshot["settings"]["escape_key"] == "shift"
    assert calls == ["shift"]


def test_remote_state_delivery_leaves_the_capture_callback_thread(tmp_path):
    calling_thread = threading.get_ident()
    delivered_on = []
    delivered = threading.Event()

    def record(snapshot):
        if snapshot.get("session", {}).get("remote"):
            delivered_on.append(threading.get_ident())
            delivered.set()

    instance = App(record, cfg_path=tmp_path / "state-thread.json")
    instance.state.mark_ready()
    instance.state.set(session={"role": "host", "remote": False})
    instance._host_remote_changed(True)

    assert delivered.wait(1)
    assert delivered_on != [calling_thread]


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
    assert connector.negotiated_version == 3
    assert connector._peer_caps == frozenset({"heartbeat"})
    assert target._peer_caps == frozenset({"heartbeat"})


def test_an_optional_ping_in_idle_is_ignored(tmp_path):
    instance = make_app(tmp_path, "idle", 0)
    instance._on_message({"t": "ping", "seq": 1, "v": 3})
    assert instance._phase == "idle"
    assert instance._active == "in"


def test_v2_peer_has_no_heartbeat_and_can_still_move_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    instance = a_client(tmp_path, monkeypatch)
    sent = []

    class FakeLink:
        def send(self, msg):
            sent.append(msg)

    instance._server = FakeLink()
    instance._active = "in"
    instance._on_message({"t": "layout", "monitors": [], "v": 2})
    instance._on_message({"t": "pos", "x": 12, "y": 34, "v": 2})
    time.sleep(0.03)
    instance._inbox.stop()
    assert instance.negotiated_version == 2
    assert instance._heartbeat_stop is None
    assert not any(msg["t"] == "ping" for msg in sent)
    assert ("move", 12, 34) in RecordingInjector.calls


def test_v2_speaking_peer_pairs_and_receives_cursor_movement(tmp_path):
    peer_id = "v2-peer"
    token = "cd" * 32
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []
    moved = threading.Event()

    def send_v2(conn, msg):
        conn.sendall(
            json.dumps({**msg, "v": 2}, separators=(",", ":")).encode() + b"\n"
        )

    def peer():
        conn, _ = listener.accept()
        stream = conn.makefile("rb")
        received.append(json.loads(stream.readline()))
        send_v2(conn, {
            "t": "pair_challenge", "nonce": "ab" * 16, "device_id": peer_id,
        })
        received.append(json.loads(stream.readline()))
        send_v2(conn, {
            "t": "pair_ok", "name": "Old peer",
            "monitors": monitors.to_wire([
                Monitor(peer_id, "0", 0, 0, 800, 600, primary=True)
            ]),
        })
        received.append(json.loads(stream.readline()))
        received.append(json.loads(stream.readline()))
        moved.set()
        conn.close()

    threading.Thread(target=peer, daemon=True).start()
    instance = make_app(tmp_path, "host", 0)
    instance.cfg.peers[peer_id] = config.Peer(name="Old peer", token=token)
    try:
        instance.connect_manually("127.0.0.1", listener.getsockname()[1])
        assert wait_for(lambda: instance.state.snapshot().get("session"))
        instance._outbox.put({"t": "pos", "x": 22, "y": 33})
        assert moved.wait(2.0)
        assert instance.negotiated_version == 2
        assert received[-1]["t"] == "pos"
        assert received[-1]["v"] == 2
        assert all("caps" not in msg for msg in received)
    finally:
        instance.stop()
        listener.close()


def test_heartbeat_timeout_tears_down_and_releases_host(pair, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "HEARTBEAT_TIMEOUT", 1.0)
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: connector._host is not None)

    local = connector.monitors[0]
    connector._host.on_move(local.x + local.w - 1, local.y + local.h // 2)
    connector._host.on_key("special", "ctrl_l", True)
    assert wait_for(lambda: ("special", "ctrl_l") in target._injector.held)
    capture = connector._capture
    target_injector = target._injector
    assert capture.suppressing is True

    # Leave the real socket open but make the peer stop all heartbeat traffic.
    monkeypatch.setattr(target, "_send", lambda _message: None)
    monkeypatch.setattr(app_module, "HEARTBEAT_TIMEOUT", 0.03)
    connector._last_received = time.monotonic()
    assert wait_for(lambda: connector._phase == "idle", timeout=1.0)
    assert capture.stop_remote_calls == 1
    assert capture.suppressing is False
    assert wait_for(lambda: not target_injector.held)
    assert target_injector.released >= 1
    assert "heartbeat" in connector.state.snapshot()["error"]


def test_stopped_heartbeat_cannot_tear_down_a_later_pairing(pair, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: connector._heartbeat_stop is not None)

    calls = []
    original = connector._teardown

    def counted_teardown(reason):
        calls.append(reason)
        original(reason)

    monkeypatch.setattr(connector, "_teardown", counted_teardown)
    connector.cancel()
    calls.clear()
    time.sleep(3 * app_module.HEARTBEAT_INTERVAL)
    assert calls == []

    assert wait_for(lambda: target._phase == "idle")
    connector.connect_manually("127.0.0.1", target._server.port)
    assert wait_for(lambda: connector.state.snapshot().get("session"))
    calls.clear()
    time.sleep(3 * app_module.HEARTBEAT_INTERVAL)
    assert connector.state.snapshot().get("session") is not None
    assert calls == []


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


def test_the_debug_log_goes_somewhere_a_packaged_app_can_write(tmp_path, monkeypatch):
    """A desktop-launched app has no usable stdout, and launching it from a
    shell to capture one costs it the accessibility grants it needs."""
    from mouseshare.__main__ import log_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = log_path()
    assert os.path.isdir(os.path.dirname(path))
    open(path, "w").close()  # writable, which is the whole point


# -- reaching a machine discovery has not found -------------------------------


def test_a_paired_machine_stays_reachable_when_discovery_is_silent(tmp_path):
    """Multicast is blocked often enough -- one dismissed firewall prompt
    does it -- that discovery cannot be what decides whether a machine you
    have already paired with and hold an address for can be connected to."""
    from mouseshare import config

    instance = make_app(tmp_path, "host", 0)
    instance.cfg.peers["mac"] = config.Peer(
        name="Mac mini", token="ab" * 32,
        last_address="192.168.1.50", last_port=39471,
    )
    instance._publish_peers()

    peer = instance.state.snapshot()["peers"][0]
    assert peer["online"] is False        # honest: we have not heard from it
    assert peer["reachable"] is True      # but we know where it lives
    assert peer["address"] == "192.168.1.50"
    assert peer["port"] == 39471          # its port, not ours


def test_a_paired_machine_we_have_no_address_for_is_not_reachable(tmp_path):
    from mouseshare import config

    instance = make_app(tmp_path, "host", 0)
    instance.cfg.peers["mac"] = config.Peer(name="Mac", token="ab" * 32)
    instance._publish_peers()

    assert instance.state.snapshot()["peers"][0]["reachable"] is False


def test_connecting_to_an_undiscovered_machine_dials_its_last_known_port(
    tmp_path, monkeypatch
):
    """Ports differ between machines: a port already taken here is free
    there, so a fallback on one side must not be assumed on the other."""
    from mouseshare import config

    instance = make_app(tmp_path, "host", 0)
    instance.cfg.port = 54009  # ours fell back; theirs did not
    instance.cfg.peers["mac"] = config.Peer(
        name="Mac", token="ab" * 32,
        last_address="192.168.1.50", last_port=39471,
    )
    dialled = []
    monkeypatch.setattr(
        instance, "_connect_to",
        lambda pid, addr, port: dialled.append((pid, addr, port)),
    )
    instance.connect("mac")

    assert dialled == [("mac", "192.168.1.50", 39471)]


def test_being_connected_to_does_not_erase_the_address_we_had(
    tmp_path, monkeypatch
):
    """Pairing again from the other side used to overwrite the address
    with nothing, which quietly cost us the only way back."""
    from mouseshare import config

    instance = make_app(tmp_path, "host", 0)
    instance.cfg.peers["mac"] = config.Peer(
        name="Mac", token="ab" * 32,
        last_address="192.168.1.50", last_port=39471,
    )
    instance._peer_id = "mac"
    instance._client = None  # they dialled us, so we have no outbound socket
    # What is stored is the point here, not the session that follows it.
    monkeypatch.setattr(instance, "_become_host", lambda: None)
    instance._on_pair_ok({"name": "Mac", "monitors": [], "token": "cd" * 32})

    assert instance.cfg.peers["mac"].last_address == "192.168.1.50"
    assert instance.cfg.peers["mac"].last_port == 39471
