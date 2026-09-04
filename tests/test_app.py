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
from mouseshare import config, monitors, protocol
from mouseshare.app import App, _Peer
from mouseshare.layout import Monitor


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    """Injector and InputCapture would import pynput and grab the machine's
    input. Neither is what these tests are about."""
    class FakeInjector:
        def __init__(self, *a, **k):
            self.released = 0
            self.held = set()
            self.moves = []

        @classmethod
        def create(cls):
            return cls()

        def release_all(self):
            self.released += 1
            self.held.clear()

        def move_to(self, x, y):
            self.moves.append((x, y))

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
    monkeypatch.setattr(
        app_module.monitors,
        "enumerate_local",
        lambda device_id: [
            Monitor(device_id, "0", 0, 0, 1920, 1080, primary=True)
        ],
    )


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

    def key(self, kind, value, pressed):
        RecordingInjector.calls.append(("key", kind, value, pressed))

    def release_all(self):
        RecordingInjector.calls.append(("release",))


def a_client(tmp_path, monkeypatch, delay=0.0):
    RecordingInjector.calls = []
    RecordingInjector.delay = delay
    monkeypatch.setattr(app_module, "Injector", RecordingInjector)
    instance = make_app(tmp_path, "client", 0)
    class Link:
        def send(self, _msg): pass
        def stop_inbound(self): pass
        def close(self): pass
    peer = _Peer("host", "Host", Link(), phase="challenged", role="client", version=3)
    instance._handshakes.append(peer)
    instance._become_client(peer)
    return instance


def send_to_client(instance, messages):
    from mouseshare.network import MessageClient, MessageServer

    peer = instance._peers["host"]
    server = MessageServer("127.0.0.1", 0, lambda msg: instance._on_message(peer, msg))
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    for message in messages:
        client.send(message)
    return server, client


def test_challenge_reader_survives_heartbeat_teardown_race(tmp_path):
    """Peer iteration and identity updates share the teardown lock."""
    iterating = threading.Event()
    teardown_started = threading.Event()
    continue_iteration = threading.Event()

    class ValuesView:
        def __init__(self, values):
            self._iterator = iter(values)

        def __iter__(self):
            first = next(self._iterator)
            yield first
            iterating.set()
            assert teardown_started.wait(1)
            continue_iteration.wait(1)
            yield from self._iterator

    class GatedPeers(dict):
        def values(self):
            return ValuesView(super().values())

    class Link:
        def send(self, _msg): pass
        def stop_inbound(self): pass
        def close(self): pass

    instance = make_app(tmp_path, "challenge-race", 0)
    live = _Peer("live", "Live", Link(), phase="session", role="host")
    other = _Peer("other", "Other", Link(), phase="session", role="host")
    incoming = _Peer("", "", Link(), phase="offered", role="host")
    instance._peers = GatedPeers({"live": live, "other": other})
    instance._handshakes.append(incoming)
    errors = []

    reader = threading.Thread(target=lambda: _record_error(
        errors, instance._on_challenge, incoming,
        protocol.pair_challenge("ab" * 16, "new-peer")))
    reader.start()
    assert iterating.wait(1)
    teardown = threading.Thread(target=lambda: (
        teardown_started.set(), instance._teardown_peer(live, "heartbeat", False)))
    teardown.start()
    continue_iteration.set()
    reader.join(1)
    teardown.join(1)

    assert not reader.is_alive()
    assert errors == []


def _record_error(errors, function, *args):
    try:
        function(*args)
    except Exception as exc:  # noqa: BLE001 - the exception is the assertion
        errors.append(exc)


def test_a_burst_of_positions_is_collapsed_before_it_is_injected(
    tmp_path, monkeypatch
):
    """A position is absolute, so an older one says nothing a newer one
    does not. Injected one at a time the backlog only grows, and the
    cursor keeps gliding after the hand has stopped."""
    instance = a_client(tmp_path, monkeypatch, delay=0.002)
    server, client = send_to_client(
        instance, ({"t": "pos", "x": x, "y": 10} for x in range(500))
    )
    assert wait_for(lambda: RecordingInjector.calls[-1:] == [("move", 499, 10)])
    client.close()
    server.stop()

    assert len(RecordingInjector.calls) < 100
    assert RecordingInjector.calls[-1] == ("move", 499, 10)


def test_a_click_still_lands_at_the_position_it_was_made_at(
    tmp_path, monkeypatch
):
    """Collapsing must not reach across a click, or the button goes down
    somewhere the user never pressed it."""
    instance = a_client(tmp_path, monkeypatch)
    server, client = send_to_client(instance, [
        {"t": "pos", "x": 5, "y": 5},
        {"t": "click", "button": "left", "pressed": True},
        {"t": "pos", "x": 9, "y": 9},
    ])
    assert wait_for(lambda: len(RecordingInjector.calls) == 3)
    client.close()
    server.stop()

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
    server, client = send_to_client(
        instance, [boom, {"t": "pos", "x": 7, "y": 7}]
    )
    assert wait_for(lambda: ("move", 7, 7) in RecordingInjector.calls)
    client.close()
    server.stop()

    assert ("move", 7, 7) in RecordingInjector.calls


def test_nothing_is_injected_after_input_has_been_released(
    tmp_path, monkeypatch
):
    """Stopping the queue drains it. Drained after the release, a held
    key goes back down on a machine whose owner cannot then type their
    way out of it."""
    instance = a_client(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original = instance._on_message

    def gated(peer, message):
        entered.set()
        release.wait(1.0)
        original(peer, message)

    instance._on_message = gated
    server, client = send_to_client(instance, [
        {"t": "pos", "x": 1, "y": 1},
        {"t": "key", "kind": "char", "value": "z", "pressed": True},
    ])
    instance._server = server
    peer = instance._peers["host"]
    peer.link = server
    assert entered.wait(1.0)
    teardown = threading.Thread(
        target=instance._teardown_peer, args=(peer, "peer went away")
    )
    teardown.start()
    assert wait_for(lambda: server._link._worker_stop.is_set())
    release.set()
    teardown.join(1.0)
    assert not teardown.is_alive()

    assert RecordingInjector.calls[-1] == ("release",)
    assert not any(call[:2] == ("key", "char") for call in RecordingInjector.calls)
    client.close()


def test_teardown_does_not_join_an_inbound_worker_while_holding_app_lock(
    tmp_path, monkeypatch
):
    instance = a_client(tmp_path, monkeypatch)
    handler_ready = threading.Event()
    enter_handler = threading.Event()
    reasons = []
    original = instance._on_message

    def gated(peer, message):
        handler_ready.set()
        enter_handler.wait(1.0)
        original(peer, message)

    instance._on_message = gated
    original_teardown = instance._teardown_peer
    instance._teardown_peer = lambda peer, reason, publish=True: (
        reasons.append(reason), original_teardown(peer, reason, publish)
    )[1]
    server, client = send_to_client(instance, [{"t": "pos", "x": 1, "y": 1}])
    instance._server = server
    peer = instance._peers["host"]
    peer.link = server
    assert handler_ready.wait(1.0)
    threading.Timer(0.02, enter_handler.set).start()

    started = time.perf_counter()
    instance._teardown_peer(peer, "first reason")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert reasons == ["first reason"]
    assert "first reason" in instance.state.snapshot()["error"]
    client.close()


def test_teardown_flushes_a_queued_leave_before_closing_the_socket(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: bool(connector._peers))
    peer = next(iter(connector._peers.values()))
    assert wait_for(lambda: peer.outbox is not None)
    received = threading.Event()
    original = target._dispatch
    target._dispatch = lambda peer, msg: (
        received.set() if msg.get("t") == "leave" else original(peer, msg)
    )

    peer.outbox.put({"t": "leave"})
    connector._teardown_peer(peer, "test flush")

    assert received.wait(1.0)


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


def test_setting_clipboard_off_applies_immediately_and_persists(tmp_path):
    instance = make_app(tmp_path, "settings", 0)
    calls = []
    instance._clipboard = type("Sync", (), {
        "set_enabled": lambda self, value: calls.append(value),
    })()
    snapshot = instance.set_share_clipboard(False)
    assert calls == [False]
    assert snapshot["settings"]["share_clipboard"] is False
    assert config.load(instance._cfg_path).share_clipboard is False


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
    instance._host = type("Host", (), {"remote": True, "peer_id": "peer"})()
    instance._peers["peer"] = _Peer("peer", "Peer", None,
                                    phase="session", role="host")
    instance._host_remote_changed(True)

    assert delivered.wait(1)
    assert delivered_on != [calling_thread]


def start_listener(instance):
    from mouseshare.network import MessageServer

    instance._server = MessageServer(
        "127.0.0.1", instance.cfg.port,
        instance._on_inbound_message, instance._on_inbound_disconnect,
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
    assert wait_for(lambda: any(p.nonce for p in connector._handshakes)), \
        "no challenge received"


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
    assert next(iter(connector._peers.values())).caps == {
        "heartbeat", "clipboard", "files"}
    assert next(iter(target._peers.values())).caps == {
        "heartbeat", "clipboard", "files"}


def test_new_peers_authenticate_pair_ok_at_negotiated_v3(pair):
    connector, target = pair
    sent = []
    original_send = target._server.send

    def record_send(msg):
        sent.append(msg.copy())
        original_send(msg)

    target._server.send = record_send
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)

    assert wait_for(lambda: connector.state.snapshot().get("session"))
    pair_ok = next(msg for msg in sent if msg.get("t") == "pair_ok")
    assert connector.negotiated_version == target.negotiated_version == 3
    assert pair_ok["hmac"]


def test_client_sends_edge_to_host_through_its_outbox(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(lambda: target._client_session is not None)
    received = []
    connector._dispatch = lambda peer, msg: received.append(msg)
    target._client_session.report_edge(12, 34)
    assert wait_for(lambda: any(msg.get("t") == "edge" for msg in received))
    assert next(msg for msg in received if msg.get("t") == "edge")["x"] == 12


def test_start_heartbeat_is_gated_when_idle_and_after_teardown(tmp_path):
    instance = make_app(tmp_path, "idle-heartbeat", 0)
    peer = _Peer("peer", "Peer", None, caps=frozenset({"heartbeat"}))
    before = {t.ident for t in threading.enumerate()}
    instance._start_heartbeat(peer)
    assert peer.heartbeat_stop is None
    assert {t.ident for t in threading.enumerate()} == before


def test_an_optional_ping_in_idle_is_ignored(tmp_path):
    instance = make_app(tmp_path, "idle", 0)
    peer = _Peer("", "", None)
    instance._on_message(peer, {"t": "ping", "seq": 1, "v": 3})
    assert peer.phase == "idle"


def test_v2_peer_has_no_heartbeat_and_can_still_move_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    instance = a_client(tmp_path, monkeypatch)
    sent = []

    class FakeLink:
        def send(self, msg):
            sent.append(msg)

    peer = instance._peers["host"]
    peer.link = FakeLink()
    peer.version = 2
    peer.caps = frozenset()
    instance._on_message(peer, {"t": "layout", "monitors": [], "v": 2})
    instance._on_message(peer, {"t": "pos", "x": 12, "y": 34, "v": 2})
    time.sleep(0.03)
    peer.outbox.stop()
    assert instance.negotiated_version == 2
    assert peer.heartbeat_stop is None
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
        if received[-1]["v"] != 2:
            conn.close()
            return
        send_v2(conn, {
            "t": "pair_challenge", "nonce": "ab" * 16, "device_id": peer_id,
        })
        received.append(json.loads(stream.readline()))
        if received[-1]["v"] != 2:
            conn.close()
            return
        send_v2(conn, {
            "t": "pair_ok", "name": "Old peer",
            "monitors": monitors.to_wire([
                Monitor(peer_id, "0", 0, 0, 800, 600, primary=True)
            ]),
        })
        received.append(json.loads(stream.readline()))
        if received[-1]["v"] != 2:
            conn.close()
            return
        received.append(json.loads(stream.readline()))
        if received[-1]["v"] != 2:
            conn.close()
            return
        moved.set()
        conn.close()

    threading.Thread(target=peer, daemon=True).start()
    instance = make_app(tmp_path, "host", 0)
    instance.cfg.peers[peer_id] = config.Peer(name="Old peer", token=token)
    try:
        instance.connect_manually("127.0.0.1", listener.getsockname()[1])
        assert wait_for(lambda: instance.state.snapshot().get("session"))
        next(iter(instance._peers.values())).outbox.put(
            {"t": "pos", "x": 22, "y": 33})
        assert moved.wait(2.0)
        assert instance.negotiated_version == 2
        assert received[0]["v"] == 2
        assert received[0]["max_v"] == 3
        assert all(msg["v"] == 2 for msg in received)
        assert received[-1]["t"] == "pos"
        assert received[-1]["v"] == 2
        assert all("caps" not in msg for msg in received)
    finally:
        instance.stop()
        listener.close()


def test_strict_v2_peer_pairs_over_code(tmp_path):
    peer_id = "strict-v2-peer"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []
    paired = threading.Event()

    def send_v2(conn, msg):
        conn.sendall(
            json.dumps({**msg, "v": 2}, separators=(",", ":")).encode() + b"\n"
        )

    def peer():
        conn, _ = listener.accept()
        stream = conn.makefile("rb")
        first = json.loads(stream.readline())
        received.append(first)
        if first["v"] != 2:
            conn.close()
            return
        send_v2(conn, {
            "t": "pair_challenge", "nonce": "ab" * 16, "device_id": peer_id,
        })
        received.append(json.loads(stream.readline()))
        if received[-1]["v"] != 2:
            conn.close()
            return
        send_v2(conn, {
            "t": "pair_ok", "name": "Strict old peer", "monitors": [],
            "token": "ef" * 32,
        })
        paired.set()
        time.sleep(0.2)
        conn.close()

    threading.Thread(target=peer, daemon=True).start()
    instance = make_app(tmp_path, "host", 0)
    try:
        instance.connect_manually("127.0.0.1", listener.getsockname()[1])
        assert wait_for(lambda: any(p.nonce for p in instance._handshakes))
        instance.submit_code("123456")
        assert paired.wait(2.0)
        assert wait_for(lambda: instance.state.snapshot().get("session"))
        assert instance.negotiated_version == 2
        assert received[0]["max_v"] == 3
        assert all(msg["v"] == 2 for msg in received)
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
    target_peer = next(iter(target._peers.values()))
    connector_peer = next(iter(connector._peers.values()))
    monkeypatch.setattr(target_peer.outbox, "_send", lambda _message: None)
    monkeypatch.setattr(app_module, "HEARTBEAT_TIMEOUT", 0.03)
    connector_peer.last_received = time.monotonic()
    assert wait_for(lambda: connector_peer.phase == "idle", timeout=1.0)
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
    assert wait_for(lambda: bool(connector._peers))
    old_peer = next(iter(connector._peers.values()))
    assert wait_for(lambda: old_peer.heartbeat_stop is not None)
    calls = []
    original = connector._teardown_peer

    def counted_teardown(peer, reason, publish=True):
        calls.append((peer, reason))
        return original(peer, reason, publish)

    monkeypatch.setattr(connector, "_teardown_peer", counted_teardown)

    connector.cancel()
    calls.clear()
    time.sleep(3 * app_module.HEARTBEAT_INTERVAL)
    assert calls == []

    assert wait_for(lambda: not target._peers)
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
    peer = connector._peers[target.cfg.device_id]
    assert peer.monitors
    assert peer.monitors[0].device_id == target.cfg.device_id


def test_the_layout_shows_both_machines_after_pairing(pair):
    connector, target = pair
    connector.connect_manually("127.0.0.1", target._server.port)
    pair_up(connector, target)
    assert wait_for(
        lambda: len(connector.state.snapshot()["layout"]["devices"]) == 2
    )


def test_default_offset_places_a_third_device_right_of_the_current_layout(tmp_path):
    instance = make_app(tmp_path, "Host", 0)
    first = _Peer("first", "One", object(), phase="session", role="host",
                  monitors=[Monitor("first", "0", 0, 0, 800, 600)])
    instance._peers[first.device_id] = first
    instance.cfg.offsets[instance.cfg.device_id] = (0, 0)
    instance.cfg.offsets[first.device_id] = (1920, 0)
    assert instance._default_offset() == (2720, 0)


def test_build_layout_assigns_unsaved_peer_offsets_sequentially(tmp_path):
    instance = make_app(tmp_path, "Host", 0)
    instance.monitors = [Monitor(instance.cfg.device_id, "0", 0, 0, 100, 100)]
    for device_id in ("first", "second"):
        instance.cfg.peers[device_id] = config.Peer(device_id.title(), "token")
        instance._known_monitors[device_id] = [
            Monitor(device_id, "0", 0, 0, 100, 100)]

    layout = instance._build_layout()

    assert layout.offsets == {
        instance.cfg.device_id: (0, 0), "first": (100, 0), "second": (200, 0)}
    assert layout.map_exit(instance.cfg.device_id, 100, 50) == ("first", 0, 50)
    assert layout.map_exit("first", 100, 50) == ("second", 0, 50)


def test_geometryless_offline_peer_is_not_draggable(tmp_path):
    instance = make_app(tmp_path, "Host", 0)
    instance.cfg.peers["ghost"] = config.Peer("Ghost", "token")
    ghost = instance._layout_view(instance._build_layout())["devices"][1]
    assert ghost["monitors"] == []
    assert ghost["draggable"] is False
    instance.set_offset("ghost", 20, 30)


def test_forget_prunes_cached_monitor_geometry(tmp_path):
    instance = make_app(tmp_path, "Host", 0)
    instance.cfg.peers["old"] = config.Peer("Old", "token")
    instance._known_monitors["old"] = [Monitor("old", "0", 0, 0, 100, 100)]
    instance.forget("old")
    assert "old" not in instance._known_monitors


def test_layout_view_keeps_disconnected_paired_devices_and_marks_connections(tmp_path):
    instance = make_app(tmp_path, "Host", 0)
    instance.cfg.peers["offline"] = config.Peer("Offline", "token")
    instance.cfg.offsets["offline"] = (1920, 0)
    instance._known_monitors["offline"] = [
        Monitor("offline", "0", 0, 0, 1280, 720, primary=True)]
    online = _Peer("online", "Online", object(), phase="session", role="host",
                   monitors=[Monitor("online", "0", 0, 0, 1000, 800)])
    instance._peers["online"] = online
    instance.cfg.peers["online"] = config.Peer("Online", "token")
    instance.cfg.offsets["online"] = (3200, 0)
    view = instance._layout_view(instance._build_layout())
    assert [d["device_id"] for d in view["devices"]] == [
        instance.cfg.device_id, "offline", "online"]
    assert [d["connected"] for d in view["devices"]] == [True, False, True]


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
    class Link:
        def send(self, _msg): pass
    peer = _Peer("mac", "Mac", Link(), phase="proved", role="host", version=3)
    peer.nonce = "ab" * 16
    peer.secret = bytes.fromhex(instance.cfg.peers["mac"].token)
    instance._handshakes.append(peer)
    # What is stored is the point here, not the session that follows it.
    monkeypatch.setattr(instance, "_become_host", lambda _peer: None)
    instance._on_pair_ok(peer, {
        "name": "Mac",
        "monitors": [],
        "token": "cd" * 32,
        "hmac": app_module.pairing.ok_proof(
            peer.secret,
            peer.nonce,
            "mac",
            instance.cfg.device_id,
        ),
    })

    assert instance.cfg.peers["mac"].last_address == "192.168.1.50"
    assert instance.cfg.peers["mac"].last_port == 39471


def test_default_offsets_use_only_placed_devices(tmp_path):
    """A wide undragged peer must not push an earlier peer out of reach, and
    a peer with a saved offset must be counted even when it is offline."""
    instance = make_app(tmp_path, "Host", 0)
    instance.monitors = [Monitor(instance.cfg.device_id, "0", 0, 0, 100, 100)]
    for device_id, width in (("first", 100), ("second", 3000)):
        instance.cfg.peers[device_id] = config.Peer(device_id.title(), "token")
        instance._known_monitors[device_id] = [
            Monitor(device_id, "0", 0, 0, width, 100)]

    layout = instance._build_layout()
    assert layout.offsets["first"] == (100, 0)
    assert layout.offsets["second"] == (200, 0)
    assert layout.map_exit(instance.cfg.device_id, 100, 50) == ("first", 0, 50)

    # An offline peer with a saved offset occupies its place; the undragged
    # one goes to the right of it instead of on top of it.
    instance.cfg.offsets["second"] = (100, 0)
    layout = instance._build_layout()
    assert layout.offsets["second"] == (100, 0)
    assert layout.offsets["first"] == (3100, 0)
    assert layout.can_place("first", layout.offsets["first"])
