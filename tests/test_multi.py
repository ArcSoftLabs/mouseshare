"""Star-topology integration tests over loopback."""
import time

import pytest

from mouseshare import app as app_module
from mouseshare import protocol
from mouseshare.app import MAX_CLIENTS, App
from mouseshare.network import MessageClient, MessageServer

from .test_app import no_real_input  # noqa: F401


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_app(tmp_path, name):
    app = App(lambda _state: None, tmp_path / f"{name}.json")
    app.cfg.name = name
    app.cfg.port = 0
    app.state.mark_ready()
    app._server = MessageServer("127.0.0.1", 0,
        app._on_inbound_message, app._on_inbound_disconnect)
    app._server.start()
    return app


def pair(host, client):
    host.connect_manually("127.0.0.1", client._server.port)
    assert wait_for(lambda: client.state.snapshot().get("pairing"))
    code = client.state.snapshot()["pairing"]["code"]
    assert wait_for(lambda: any(p.nonce for p in host._handshakes))
    host.submit_code(code)
    assert wait_for(lambda: client.cfg.device_id in host._peers)


@pytest.fixture
def star(tmp_path):
    apps = [make_app(tmp_path, name) for name in ("host", "one", "two", "three")]
    yield apps
    for app in apps:
        app.stop()


def test_three_clients_disconnect_and_reconnect_without_disturbing_others(star):
    host, one, two, three = star
    for client in (one, two, three):
        pair(host, client)
    ids = {one.cfg.device_id, two.cfg.device_id, three.cfg.device_id}
    assert set(host.state.snapshot()["session"]["clients"]) == ids

    host._host.on_move(1919, 500)
    assert host._host.remote is True
    assert host._host.peer_id == one.cfg.device_id

    host.disconnect(two.cfg.device_id)
    assert wait_for(lambda: set(host.state.snapshot()["session"]["clients"])
                    == {one.cfg.device_id, three.cfg.device_id})
    assert wait_for(lambda: not two._peers)
    assert host._host.remote is True
    assert host._host.peer_id == one.cfg.device_id
    host.connect_manually("127.0.0.1", two._server.port)
    assert wait_for(lambda: set(host.state.snapshot()["session"]["clients"]) == ids)
    assert host._host.remote is True
    assert host._host.peer_id == one.cfg.device_id


def test_client_cannot_connect_and_host_refuses_inbound_as_busy(star):
    host, one, _, three = star
    pair(host, one)
    snapshot = one.connect_manually("127.0.0.1", three._server.port)
    assert snapshot["error"] == "Disconnect from host first"

    caller = MessageClient("127.0.0.1", host._server.port)
    caller.connect()
    got = []
    caller.start_reader(got.append)
    caller.send(protocol.pair_request("stranger", "Stranger"))
    assert wait_for(lambda: got)
    assert got[0]["reason"] == "busy"
    assert one.cfg.device_id in host._peers
    caller.close()


def test_inbound_stranger_does_not_make_an_idle_machine_a_client(star):
    host, _, _, target = star
    caller = MessageClient("127.0.0.1", host._server.port)
    caller.connect()
    caller.send(protocol.pair_request("stranger", "Stranger"))
    assert wait_for(lambda: host.state.snapshot().get("pairing"))

    snapshot = host.connect_manually("127.0.0.1", target._server.port)

    assert snapshot["error"] != "Disconnect from host first"
    caller.close()


def test_duplicate_identity_is_refused_without_dropping_original(star):
    host, one, two, _ = star
    pair(host, one)
    pair(host, two)
    host._host.on_move(1919, 500)
    assert host._host.peer_id == one.cfg.device_id
    got = []
    fake = MessageServer("127.0.0.1", 0, lambda msg: got.append(msg))
    fake.start()
    fake._on_message = lambda msg: (
        got.append(msg),
        fake.send(protocol.pair_challenge("ab" * 16, one.cfg.device_id))
        if msg["t"] == "pair_request" else None,
    )
    try:
        host.connect_manually("127.0.0.1", fake.port)
        assert wait_for(lambda: any(m.get("reason") == "duplicate" for m in got))
        assert one.cfg.device_id in host._peers
        assert host._host.remote is True
        assert host._host.peer_id == one.cfg.device_id
    finally:
        fake.stop()


def test_ninth_real_loopback_client_is_refused_with_full(tmp_path):
    host = make_app(tmp_path, "full-host")
    targets = [make_app(tmp_path, f"client-{index}")
               for index in range(MAX_CLIENTS + 1)]
    try:
        for target in targets[:MAX_CLIENTS]:
            pair(host, target)
        ninth = targets[-1]
        snapshot = host.connect_manually("127.0.0.1", ninth._server.port)
        assert "maximum number of clients" in snapshot["error"]
        assert ninth.state.snapshot()["pairing"] is None
        assert not ninth._handshakes
        assert len(host._peers) == MAX_CLIENTS
    finally:
        host.stop()
        for target in targets:
            target.stop()


def test_four_devices_cross_each_direction_and_route_peer_hops(star):
    host, left, right, below = star
    for client in (left, right, below):
        pair(host, client)
    offsets = {
        left.cfg.device_id: (-1920, 0),
        right.cfg.device_id: (1920, 0),
        below.cfg.device_id: (0, 1080),
    }
    host.cfg.offsets.update(offsets)
    host._host.update_layout(host._build_layout())

    for edge, peer, back in [
        ((0, 500), left.cfg.device_id, (1, 0)),
        ((1919, 500), right.cfg.device_id, (-1, 0)),
        ((500, 1079), below.cfg.device_id, (0, -1)),
    ]:
        host._host.on_move(*edge)
        assert host._host.peer_id == peer
        assert host.state.snapshot()["session"]["active_peer"] == peer
        host._host.on_delta(*back)
        assert host._host.remote is False

    sent = []
    original_send = host._host._send
    host._host._send = lambda peer_id, msg: (
        sent.append((peer_id, msg["t"])), original_send(peer_id, msg))
    host._host.on_move(1919, 500)
    host._host.on_delta(-1, 0)
    assert host._host.remote is False
    assert sent[-1] == (right.cfg.device_id, "leave")

    host._host.on_move(500, 1079)
    assert host._host.peer_id == below.cfg.device_id
    host._host.on_delta(0, -1)

    host.cfg.offsets[below.cfg.device_id] = (1920, 1080)
    host._host.update_layout(host._build_layout())
    host._host.on_move(1919, 500)
    host._host.on_delta(500, 580)
    assert host._host.remote is True
    assert host._host.peer_id == below.cfg.device_id
    assert host.state.snapshot()["session"]["active_peer"] == below.cfg.device_id
    assert sent[-2:] == [
        (right.cfg.device_id, "leave"),
        (below.cfg.device_id, "enter"),
    ]

    host._host.on_delta(0, -1)
    host.cfg.offsets[below.cfg.device_id] = (0, 1080)
    host._host.update_layout(host._build_layout())
    host._host.on_move(1919, 500)
    host._host.on_delta(500, 580)
    assert host._host.peer_id == right.cfg.device_id
    assert sent[-1] == (right.cfg.device_id, "pos")


def test_active_peer_death_releases_but_unrelated_peer_death_does_not(
        star, monkeypatch):
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "HEARTBEAT_TIMEOUT", 1.0)
    host, one, two, three = star
    for client in (one, two, three):
        pair(host, client)
    host._host.on_move(1919, 500)
    assert host._host.peer_id == one.cfg.device_id

    two.stop()
    assert wait_for(lambda: two.cfg.device_id not in host._peers)
    assert host._host.remote is True
    assert host._host.peer_id == one.cfg.device_id

    host._host.on_delta(-1, 0)
    host.cfg.offsets[three.cfg.device_id] = (1920, 0)
    host.cfg.offsets[one.cfg.device_id] = (-1920, 0)
    host._host.update_layout(host._build_layout())
    host._host.on_move(1919, 500)
    assert host._host.peer_id == three.cfg.device_id
    target_peer = next(iter(three._peers.values()))
    host_peer = host._peers[three.cfg.device_id]
    monkeypatch.setattr(target_peer.outbox, "_send", lambda _message: None)
    monkeypatch.setattr(app_module, "HEARTBEAT_TIMEOUT", 0.03)
    host_peer.last_received = time.monotonic()
    assert wait_for(lambda: three.cfg.device_id not in host._peers, timeout=1.0)
    assert wait_for(lambda: not host._host.remote)
    assert host._capture.suppressing is False
    assert host._injector.moves[-1] == (960, 540)
