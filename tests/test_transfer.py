import hashlib
import io
import logging
import os
import shutil
import threading
import time
import tracemalloc
from collections import namedtuple

import pytest

from mouseshare import protocol
from mouseshare import transfer as transfer_module
from mouseshare.transfer import SPACE_MARGIN, TRANSFER_CHUNK, TRANSFER_MAX, validate_offer
from tests.test_app import no_real_input  # noqa: F401
from tests.test_multi import make_app, pair
from tests.test_security import attacker


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("name", [
    "", ".", "..", "a/b", "a\\b", "bad\0name", "bad\nname",
    "CON", "con.txt", "LPT9.log", "x" * 256,
    "has:stream",
])
def test_offer_rejects_unsafe_file_names(name):
    with pytest.raises(ValueError):
        validate_offer([{"name": name, "size": 0, "sha256": "0" * 64}])


@pytest.mark.parametrize("name", ["résumé 世界.txt", "normal", "a.tar.gz"])
def test_offer_accepts_safe_unicode_file_names(name):
    assert validate_offer([
        {"name": name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    ])[0]["name"] == name


@pytest.mark.parametrize("files", [
    [{"name": "x", "size": -1, "sha256": "0" * 64}],
    [{"name": "x", "size": True, "sha256": "0" * 64}],
    [{"name": "x", "size": TRANSFER_MAX + 1, "sha256": "0" * 64}],
    [{"name": "x", "size": 0, "sha256": "z" * 64}],
    [{"name": "x", "size": 0, "sha256": "0" * 63}],
    [{"name": f"x{i}", "size": 0, "sha256": "0" * 64}
     for i in range(201)],
])
def test_offer_rejects_invalid_metadata_and_count(files):
    with pytest.raises(ValueError):
        validate_offer(files)


def test_two_apps_transfer_multiple_empty_and_nonempty_files(tmp_path):
    host, client = make_app(tmp_path, "host"), make_app(tmp_path, "client")
    destination = tmp_path / "received"
    client._transfers.destination = destination
    first, empty = tmp_path / "hello.txt", tmp_path / "empty.bin"
    first.write_bytes(b"hello" * 10000)
    empty.write_bytes(b"")
    try:
        pair(host, client)
        host.send_files(client.cfg.device_id, [str(first), str(empty)])
        assert wait_for(lambda: (destination / "hello.txt").exists())
        assert (destination / "hello.txt").read_bytes() == first.read_bytes()
        assert (destination / "empty.bin").read_bytes() == b""
        assert client.state.snapshot()["transfers"][-1]["status"] == "done"
    finally:
        host.stop()
        client.stop()


def test_chunk_wire_line_stays_below_reserved_protocol_limit():
    message = protocol.xfer_chunk("f" * 32, 0, 1, b"x" * (32 * 1024))
    assert len(protocol.encode(message)) < protocol.MAX_LINE - 256


def test_transfer_never_overwrites_an_existing_file(tmp_path):
    host, client = make_app(tmp_path, "host"), make_app(tmp_path, "client")
    destination = tmp_path / "received"
    destination.mkdir()
    (destination / "report.txt").write_text("old")
    source = tmp_path / "report.txt"
    source.write_text("new")
    client._transfers.destination = destination
    try:
        pair(host, client)
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: (destination / "report (2).txt").exists())
        assert (destination / "report.txt").read_text() == "old"
        assert (destination / "report (2).txt").read_text() == "new"
    finally:
        host.stop()
        client.stop()


def test_receiver_setting_off_rejects_offer_without_touching_disk(tmp_path):
    host, client = make_app(tmp_path, "host"), make_app(tmp_path, "client")
    destination = tmp_path / "received"
    client._transfers.destination = destination
    source = tmp_path / "private.txt"
    source.write_text("secret")
    try:
        pair(host, client)
        client.set_share_files(False)
        # The sender learned the updated capability, so exercise the inbound
        # protocol seam directly as an already-authenticated peer would.
        peer = next(iter(client._peers.values()))
        sent = []
        original = client._transfers._send
        client._transfers._send = lambda peer_id, msg: (sent.append(msg), original(peer_id, msg))[1]
        client._dispatch(peer, protocol.xfer_offer("off", [{"name": source.name,
            "size": 6, "sha256": hashlib.sha256(b"secret").hexdigest()}]))
        assert sent[-1] == protocol.xfer_reject("off", "disabled")
        assert not destination.exists()
    finally:
        host.stop()
        client.stop()


@pytest.mark.parametrize("name", ["trailing.", "trailing "])
def test_offer_rejects_nt_trailing_dot_and_space(monkeypatch, name):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError):
        validate_offer([{"name": name, "size": 0, "sha256": "0" * 64}])


@pytest.mark.parametrize("character", list('<>"|?*'))
def test_offer_rejects_nt_forbidden_characters(monkeypatch, character):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError):
        validate_offer([{"name": f"bad{character}name", "size": 0,
                         "sha256": "0" * 64}])


def _apps_for_transfer(tmp_path, size=TRANSFER_CHUNK * 4, name="payload.bin"):
    host, client = make_app(tmp_path, "host"), make_app(tmp_path, "client")
    destination = tmp_path / "received"
    client._transfers.destination = destination
    source = tmp_path / name
    source.write_bytes(bytes(range(251)) * (size // 251) + bytes(range(251))[:size % 251])
    pair(host, client)
    return host, client, source, destination


def _statuses(app):
    return [item["status"] for item in app.state.snapshot()["transfers"]]


def test_one_mib_transfer_streams_with_bounded_sender_and_receiver_memory(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, 1024 * 1024)
    try:
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: (destination / source.name).exists())
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert (destination / source.name).read_bytes() == source.read_bytes()
        # Both endpoints share this test process, so bound their combined peak.
        # The source and the .part are two buffered handles; Python 3.14 raised
        # io.DEFAULT_BUFFER_SIZE from 8 KiB to 128 KiB, so allow for both.
        assert peak - baseline < 512 * 1024 + 2 * io.DEFAULT_BUFFER_SIZE
    finally:
        host.stop()
        client.stop()


def test_integrity_mismatch_removes_part_and_reports_bare_reason(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path)
    original = host._transfers._send
    corrupted = False

    def send(peer_id, msg):
        nonlocal corrupted
        if msg["t"] == "xfer_chunk" and not corrupted:
            msg = {**msg, "data": protocol.xfer_chunk(msg["id"], msg["i"], msg["n"], b"x")["data"]}
            corrupted = True
        original(peer_id, msg)

    host._transfers._send = send
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: "failed" in _statuses(client))
        view = client.state.snapshot()["transfers"][-1]
        assert view["error"] == "integrity"
        assert str(destination) not in view["error"]
        assert not list(destination.glob("*.part"))
    finally:
        host.stop()
        client.stop()


def test_sender_cancel_midstream_removes_part_and_cancels_both(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 40)
    original = host._transfers._send
    host._transfers._send = lambda peer_id, msg: (time.sleep(0.02) if msg["t"] == "xfer_chunk" else None,
                                                   original(peer_id, msg))[1]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        transfer_id = host.state.snapshot()["transfers"][-1]["id"]
        assert wait_for(lambda: host.state.snapshot()["transfers"][-1]["bytes_done"] > 0)
        host.cancel_transfer(transfer_id)
        assert wait_for(lambda: _statuses(client) and _statuses(client)[-1] == "cancelled"), client.state.snapshot()["transfers"]
        assert _statuses(host)[-1] == "cancelled"
        assert not list(destination.glob("*.part"))
    finally:
        host.stop()
        client.stop()


def test_receiver_cancel_stops_sender_chunks_and_removes_part(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 40)
    original_send = host._transfers._send
    chunks = []
    host._transfers._send = lambda peer_id, msg: (chunks.append(msg["i"]) if msg["t"] == "xfer_chunk" else None,
                                                   time.sleep(0.02), original_send(peer_id, msg))[2]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: client.state.snapshot()["transfers"] and
                        client.state.snapshot()["transfers"][-1]["bytes_done"] > 0)
        transfer_id = client.state.snapshot()["transfers"][-1]["id"]
        client.cancel_transfer(transfer_id)
        assert wait_for(lambda: _statuses(host)[-1] == "cancelled"), host.state.snapshot()["transfers"]
        count = len(chunks)
        time.sleep(0.05)
        assert len(chunks) == count
        assert not list(destination.glob("*.part"))
    finally:
        host.stop()
        client.stop()


def test_receiver_cancel_on_last_chunk_cancels_both_without_done(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 2)
    sent = []
    original_send = host._transfers._send
    original_chunk = client._transfers._receive_chunk

    def send(peer_id, msg):
        sent.append(msg["t"])
        original_send(peer_id, msg)

    def receive_chunk(transfer_id, msg):
        if msg["i"] + 1 == msg["n"]:
            client._transfers.cancel(transfer_id)
            return
        original_chunk(transfer_id, msg)

    host._transfers._send = send
    client._transfers._receive_chunk = receive_chunk
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: _statuses(host) and _statuses(host)[-1] == "cancelled")
        assert _statuses(client)[-1] == "cancelled"
        assert "xfer_done" not in sent
        assert not list(destination.glob("*.part"))
        assert not (destination / source.name).exists()
    finally:
        host.stop()
        client.stop()


def test_receiver_cancel_racing_chunk_keeps_link_up(tmp_path, monkeypatch):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 2)
    original_decode = transfer_module.base64.b64decode
    cancelled = False
    positions = []

    def decode(data, validate):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            transfer_id = client.state.snapshot()["transfers"][-1]["id"]
            client._transfers.cancel(transfer_id)
        return original_decode(data, validate=validate)

    monkeypatch.setattr(transfer_module.base64, "b64decode", decode)
    client._inject = lambda msg: positions.append(msg["x"]) if msg["t"] == "pos" else None
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: _statuses(client) and _statuses(client)[-1] == "cancelled")
        assert not list(destination.glob("*.part"))
        assert host.cfg.device_id in client._peers
        host._send(client.cfg.device_id, protocol.pos(123, 0))
        assert wait_for(lambda: positions == [123])
    finally:
        host.stop()
        client.stop()


def test_disconnect_midstream_cleans_up_and_sender_thread_exits(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 40)
    original = host._transfers._send
    host._transfers._send = lambda peer_id, msg: (time.sleep(0.02), original(peer_id, msg))[1]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: client.state.snapshot()["transfers"] and
                        client.state.snapshot()["transfers"][-1]["bytes_done"] > 0)
        host.disconnect(client.cfg.device_id)
        assert wait_for(lambda: _statuses(host)[-1] == "failed")
        assert wait_for(lambda: not list(destination.glob("*.part")))
        assert wait_for(lambda: not any(t.name == "mouseshare-transfer" and t.is_alive()
                                        for t in threading.enumerate()), timeout=1)
    finally:
        host.stop()
        client.stop()


def test_free_space_precheck_rejects_and_writes_nothing(tmp_path, monkeypatch):
    host, client, source, destination = _apps_for_transfer(tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(1, 1, SPACE_MARGIN - 1))
    sent = []
    original = client._transfers._send
    client._transfers._send = lambda peer_id, msg: (sent.append(msg), original(peer_id, msg))[1]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: any(m.get("reason") == "space" for m in sent))
        assert not list(destination.iterdir())
    finally:
        host.stop()
        client.stop()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_read_only_destination_reports_error_and_leaves_nothing(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path)
    destination.mkdir()
    destination.chmod(0o500)
    if os.access(destination, os.W_OK):
        pytest.skip("chmod is ineffective for this user")
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: "failed" in _statuses(host))
        assert not list(destination.iterdir())
    finally:
        destination.chmod(0o700)
        host.stop()
        client.stop()


def test_non_session_peer_offer_is_ignored_and_writes_nothing(tmp_path):
    victim = make_app(tmp_path, "victim")
    victim._transfers.destination = tmp_path / "received"
    raw, received = attacker(victim)
    try:
        raw.send(protocol.xfer_offer("attack", [{"name": "loot", "size": 0,
                 "sha256": hashlib.sha256(b"").hexdigest()}]))
        time.sleep(0.1)
        assert not received
        assert not victim._transfers.destination.exists()
    finally:
        raw.close()
        victim.stop()


def test_position_traffic_remains_ordered_during_multichunk_transfer(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 200)
    seen = []
    seen_in_flight = []
    position_receiver = host if host._client_session else client
    position_sender = client if position_receiver is host else host
    def inject(msg):
        if msg["t"] == "pos":
            seen.append(msg["x"])
            if not (destination / source.name).exists():
                seen_in_flight.append(msg["x"])

    position_receiver._inject = inject
    original = host._transfers._send
    host._transfers._send = lambda peer_id, msg: (
        time.sleep(0.01) if msg["t"] == "xfer_chunk" else None,
        original(peer_id, msg),
    )[1]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        for index in range(200):
            position_sender._send(position_receiver.cfg.device_id, protocol.pos(index, 0))
            time.sleep(0.01)
        assert wait_for(lambda: seen and seen[-1] == 199, timeout=1)
        assert wait_for(lambda: (destination / source.name).exists())
        assert all(left < right for left, right in zip(seen, seen[1:]))
        assert seen_in_flight
    finally:
        host.stop()
        client.stop()


def test_transfer_content_and_destination_are_never_logged(tmp_path, caplog):
    host, client, source, destination = _apps_for_transfer(tmp_path)
    secret = b"never-log-content-marker"
    source.write_bytes(secret * 5000)
    try:
        with caplog.at_level(logging.DEBUG, logger="mouseshare"):
            host.send_files(client.cfg.device_id, [str(source)])
            assert wait_for(lambda: (destination / source.name).exists())
            (destination / source.name).unlink()
            source.write_bytes(secret)
            source_digest = hashlib.sha256(secret).hexdigest()
            original = host._transfers._send
            host._transfers._send = lambda peer_id, msg: original(peer_id, {
                **msg, "data": protocol.xfer_chunk(msg["id"], 0, 1, b"bad")["data"]
            }) if msg["t"] == "xfer_chunk" else original(peer_id, msg)
            host.send_files(client.cfg.device_id, [str(source)])
            assert wait_for(lambda: _statuses(client)[-1] == "failed")
        assert secret.decode() not in caplog.text
        assert str(destination) not in caplog.text
        assert source_digest not in caplog.text
    finally:
        host.stop()
        client.stop()


def test_overrun_is_rejected_and_part_removed(tmp_path):
    host, client, _, destination = _apps_for_transfer(tmp_path, 1)
    peer = next(iter(client._peers.values()))
    transfer_id = "overrun"
    client._dispatch(peer, protocol.xfer_offer(transfer_id, [{"name": "tiny", "size": 0,
        "sha256": hashlib.sha256(b"").hexdigest()}]))
    client._dispatch(peer, protocol.xfer_chunk(transfer_id, 0, 1, b"x"))
    assert client.state.snapshot()["transfers"][-1]["error"] == "overrun"
    assert not list(destination.glob("*.part"))
    host.stop()
    client.stop()


@pytest.mark.parametrize("counts", [(2,), (3, 2)])
def test_wrong_or_changed_chunk_count_is_rejected(tmp_path, counts):
    host, client, _, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 3)
    peer = next(iter(client._peers.values()))
    transfer_id = "bad-count"
    data = b"x" * (TRANSFER_CHUNK * 3)
    client._dispatch(peer, protocol.xfer_offer(transfer_id, [{"name": "three", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()}]))
    for index, count in enumerate(counts):
        client._dispatch(peer, protocol.xfer_chunk(transfer_id, index, count, b"x" * TRANSFER_CHUNK))
    assert client.state.snapshot()["transfers"][-1]["status"] == "failed"
    assert not list(destination.glob("*.part"))
    host.stop()
    client.stop()


def test_target_created_after_accept_is_not_overwritten(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path)
    original = client._transfers._send

    def send(peer_id, msg):
        if msg["t"] == "xfer_accept":
            (destination / source.name).write_bytes(b"winner")
        original(peer_id, msg)

    client._transfers._send = send
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: _statuses(client) and _statuses(client)[-1] == "failed")
        assert (destination / source.name).read_bytes() == b"winner"
    finally:
        host.stop()
        client.stop()


def test_two_offers_sharing_later_name_do_not_unlink_winners_part(tmp_path):
    host, client, _, destination = _apps_for_transfer(tmp_path, 1)
    peer = next(iter(client._peers.values()))
    empty = hashlib.sha256(b"").hexdigest()
    files = [{"name": "first-a", "size": 0, "sha256": empty},
             {"name": "shared", "size": 0, "sha256": empty}]
    client._dispatch(peer, protocol.xfer_offer("winner", files))
    # Build the wire shape explicitly: the second offer has a distinct first
    # file but collides on the already-reserved later file.
    client._dispatch(peer, {"t": "xfer_offer", "id": "loser", "files": [
        {"name": "first-b", "size": 0, "sha256": empty}, files[1]]})
    client._transfers.cancel("loser")
    assert (destination / "shared.part").exists()
    assert "winner" in client._transfers._receives
    for file_index in range(2):
        client._dispatch(peer, protocol.xfer_chunk("winner", 0, 1, b""))
        client._dispatch(peer, protocol.xfer_done("winner", file_index))
    assert (destination / "first-a").read_bytes() == b""
    assert (destination / "shared").read_bytes() == b""
    host.stop()
    client.stop()


def test_malformed_base64_type_fails_transfer_but_keeps_link(tmp_path):
    host, client, _, _ = _apps_for_transfer(tmp_path, 1)
    peer = next(iter(client._peers.values()))
    empty = hashlib.sha256(b"").hexdigest()
    client._dispatch(peer, {"t": "xfer_offer", "id": "bad", "files": [
        {"name": "bad", "size": 0, "sha256": empty}]})
    client._dispatch(peer, {"t": "xfer_chunk", "id": "bad", "i": 0, "n": 1,
                            "data": {"not": "a string"}})
    assert client.state.snapshot()["transfers"][-1]["error"] == "malformed"
    assert host.cfg.device_id in client._peers
    host.stop()
    client.stop()


def test_turning_setting_off_midtransfer_cancels_both_sides(tmp_path):
    host, client, source, destination = _apps_for_transfer(tmp_path, TRANSFER_CHUNK * 40)
    original = host._transfers._send
    host._transfers._send = lambda peer_id, msg: (time.sleep(0.02), original(peer_id, msg))[1]
    try:
        host.send_files(client.cfg.device_id, [str(source)])
        assert wait_for(lambda: client.state.snapshot()["transfers"] and
                        client.state.snapshot()["transfers"][-1]["bytes_done"] > 0)
        client.set_share_files(False)
        assert wait_for(lambda: _statuses(host)[-1] == "cancelled")
        assert _statuses(client)[-1] == "cancelled"
        assert not list(destination.glob("*.part"))
    finally:
        host.stop()
        client.stop()
