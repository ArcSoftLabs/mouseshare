import threading
import time

from mouseshare import protocol as p
from mouseshare.network import MessageServer, MessageClient


def test_client_sends_message_to_server_over_loopback():
    received = []
    done = threading.Event()

    def on_message(msg):
        received.append(msg)
        done.set()

    server = MessageServer("127.0.0.1", 0, on_message)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        client.send(p.hello("client", 800, 600))
        assert done.wait(timeout=5)
        assert received == [{"t": "hello", "role": "client", "w": 800, "h": 600}]
        client.close()
    finally:
        server.stop()


def test_server_sends_message_to_client():
    received = []
    done = threading.Event()

    server = MessageServer("127.0.0.1", 0, lambda m: None)
    server.start()
    try:
        client = MessageClient("127.0.0.1", server.port)
        client.connect()
        client.start_reader(lambda m: (received.append(m), done.set()))
        # wait for the server to register the connection
        deadline = time.time() + 5
        while not server.has_connection() and time.time() < deadline:
            time.sleep(0.01)
        server.send(p.pos(2, 3))
        assert done.wait(timeout=5)
        assert received == [{"t": "pos", "x": 2, "y": 3}]
        client.close()
    finally:
        server.stop()
