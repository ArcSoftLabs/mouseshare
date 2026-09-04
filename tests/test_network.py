import socket
import threading
import time

import pytest

from mouseshare import protocol as p
from mouseshare.network import MessageClient, MessageServer


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_client_sends_message_to_server_over_loopback():
    received = []
    done = threading.Event()

    server = MessageServer("127.0.0.1", 0, lambda m: (received.append(m), done.set()))
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        client.send(p.pair_request("abc", "laptop"))
        assert done.wait(timeout=5)
        assert received[0]["t"] == "pair_request"
        assert received[0]["device_id"] == "abc"
        client.close()
    finally:
        server.stop()


def test_client_first_frame_advertises_v3_in_a_v2_envelope():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = MessageClient("127.0.0.1", listener.getsockname()[1])
    client.connect()
    conn, _ = listener.accept()
    conn.settimeout(2.0)
    try:
        client.send(p.pair_request("abc", "laptop"))
        first = p.decode(conn.makefile("rb").readline())
        assert first["v"] == 2
        assert first["max_v"] == 3
    finally:
        client.close()
        conn.close()
        listener.close()


@pytest.mark.parametrize("reply_version", [2, 3])
def test_client_uses_responder_version_after_first_reply(reply_version):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []
    reply_seen = threading.Event()
    client = MessageClient("127.0.0.1", listener.getsockname()[1])
    client.connect()
    conn, _ = listener.accept()
    stream = conn.makefile("rb")
    client.start_reader(lambda msg: reply_seen.set())
    try:
        client.send(p.pair_request("abc", "laptop"))
        received.append(p.decode(stream.readline()))
        conn.sendall(p.encode(p.pair_challenge("nonce", "peer"), reply_version))
        assert reply_seen.wait(2.0)
        client.send(p.auth("abc", "proof"))
        received.append(p.decode(stream.readline()))
        assert received[1]["v"] == reply_version
        assert "max_v" not in received[1]
    finally:
        client.close()
        conn.close()
        listener.close()


def test_server_sends_message_to_client():
    received = []
    done = threading.Event()

    server = MessageServer("127.0.0.1", 0, lambda m: None)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        client.start_reader(lambda m: (received.append(m), done.set()))
        assert wait_for(server.has_connection)
        server.send(p.pos(2, 3))
        assert done.wait(timeout=5)
        assert received[0]["t"] == "pos"
        client.close()
    finally:
        server.stop()


def test_link_remembers_first_peer_version_and_replies_with_it():
    received = []
    server = MessageServer("127.0.0.1", 0, lambda m: server.send(p.leave()))
    server.start()
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(2.0)
    try:
        raw.sendall(b'{"t":"pair_request","device_id":"v2","name":"old","v":2}\n')
        reply = raw.recv(4096)
        received.append(p.decode(reply))
        assert server.peer_version == 2
        assert received == [{"t": "leave", "v": 2}]
    finally:
        raw.close()
        server.stop()


def test_server_negotiates_advertised_max_version_and_replies_with_it():
    received = []
    server = MessageServer("127.0.0.1", 0, lambda m: server.send(p.leave()))
    server.start()
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(2.0)
    try:
        raw.sendall(
            b'{"t":"pair_request","device_id":"new","name":"new",'
            b'"max_v":3,"v":2}\n'
        )
        received.append(p.decode(raw.recv(4096)))
        assert server.peer_version == 3
        assert received == [{"t": "leave", "v": 3}]
    finally:
        raw.close()
        server.stop()


# -- disconnect events -------------------------------------------------------
#
# The host must un-suppress input the instant the peer goes away. v0.1 had
# no way to learn that: the read loop returned silently and has_connection()
# kept reporting True until a send happened to fail. Harmless in a mouse-only
# prototype; with keyboard suppression it can leave a machine unable to type.


def test_server_reports_disconnect_when_the_client_closes():
    events = []
    server = MessageServer("127.0.0.1", 0, lambda m: None, on_disconnect=events.append)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        assert wait_for(server.has_connection)
        client.close()
        assert wait_for(lambda: events != [])
        assert events == ["eof"]
    finally:
        server.stop()


def test_has_connection_is_false_after_the_peer_disappears():
    server = MessageServer("127.0.0.1", 0, lambda m: None)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        assert wait_for(server.has_connection)
        client.close()
        assert wait_for(lambda: not server.has_connection())
    finally:
        server.stop()


def test_disconnect_fires_exactly_once_even_when_a_send_also_fails():
    events = []
    server = MessageServer("127.0.0.1", 0, lambda m: None, on_disconnect=events.append)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        assert wait_for(server.has_connection)
        client.close()
        assert wait_for(lambda: events != [])
        for _ in range(5):
            server.send(p.pos(1, 1))
        time.sleep(0.2)
        assert len(events) == 1
    finally:
        server.stop()


def test_a_protocol_error_disconnects_the_peer():
    """Garbage on the wire is fatal, and must reach the session as a
    disconnect so suppression is released."""
    events = []
    server = MessageServer("127.0.0.1", 0, lambda m: None, on_disconnect=events.append)
    server.start()
    try:
        raw = socket.create_connection(("127.0.0.1", server.port))
        assert wait_for(server.has_connection)
        raw.sendall(b"this is not json\n")
        assert wait_for(lambda: events != [])
        assert events == ["protocol"]
        raw.close()
    finally:
        server.stop()


def test_a_second_connection_is_refused_while_one_is_live():
    """One peer at a time. v0.1 replaced the live connection with the new
    one, which would let any machine on the LAN evict the paired peer."""
    server = MessageServer("127.0.0.1", 0, lambda m: None)
    server.start()
    try:
        first = MessageClient("127.0.0.1", server.port)
        first.connect()
        assert wait_for(server.has_connection)

        second = socket.create_connection(("127.0.0.1", server.port))
        second.settimeout(2.0)
        assert p.decode(second.recv(4096)) == {
            "t": "pair_err", "reason": "busy", "v": 3,
        }
        assert second.recv(4096) == b""  # then closed immediately
        second.close()

        # the original peer is untouched
        assert server.has_connection()
        first.close()
    finally:
        server.stop()


def test_stop_reports_a_disconnect_for_a_live_connection():
    events = []
    server = MessageServer("127.0.0.1", 0, lambda m: None, on_disconnect=events.append)
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    assert wait_for(server.has_connection)
    server.stop()
    assert events == ["shutdown"]
    client.close()


def test_client_reports_disconnect_when_the_server_goes_away():
    events = []
    server = MessageServer("127.0.0.1", 0, lambda m: None)
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    client.start_reader(lambda m: None, on_disconnect=events.append)
    assert wait_for(server.has_connection)
    server.stop()
    assert wait_for(lambda: events != [])
    assert events == ["eof"]
    client.close()


def test_slow_handler_does_not_stall_receipt_of_a_second_message():
    first_started = threading.Event()
    release = threading.Event()
    server = MessageServer(
        "127.0.0.1", 0,
        lambda msg: (first_started.set(), release.wait(2.0)),
    )
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    try:
        client.send(p.pos(1, 1))
        assert first_started.wait(2.0)
        client.send(p.leave())
        assert wait_for(lambda: server._link._inbound.qsize() == 1)
    finally:
        release.set()
        client.close()
        server.stop()


def test_reader_thread_ends_after_close():
    server = MessageServer("127.0.0.1", 0, lambda _msg: None)
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    client.start_reader(lambda _msg: None)
    link = client._link

    client.close()
    link._reader.join(1.0)

    assert not link._reader.is_alive()
    server.stop()


def test_handler_runtime_error_keeps_link_open():
    handled = threading.Event()

    def handler(msg):
        if msg["t"] == "pos":
            raise RuntimeError("boom")
        handled.set()

    server = MessageServer("127.0.0.1", 0, handler)
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    try:
        client.send(p.pos(1, 1))
        client.send(p.leave())
        assert handled.wait(2.0)
        assert server.has_connection()
    finally:
        client.close()
        server.stop()


def test_handler_protocol_error_closes_link():
    events = []

    def handler(_msg):
        raise p.ProtocolError("bad state")

    server = MessageServer(
        "127.0.0.1", 0, handler, on_disconnect=events.append
    )
    server.start()
    client = MessageClient("127.0.0.1", server.port)
    client.connect()
    try:
        client.send(p.leave())
        assert wait_for(lambda: events == ["protocol"])
        assert not server.has_connection()
    finally:
        client.close()
        server.stop()
