"""TCP transport for protocol messages. One host, one client connection."""
import socket
import threading
from typing import Callable, Optional

from . import protocol

MessageHandler = Callable[[dict], None]


def _read_loop(sock: socket.socket, handler: MessageHandler) -> None:
    buf = protocol.LineBuffer()
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                return
            for msg in buf.feed(data):
                handler(msg)
    except OSError:
        return


class MessageServer:
    """Accepts a single client connection and exchanges messages with it."""

    def __init__(self, host: str, port: int, on_message: MessageHandler):
        self._on_message = on_message
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._conn: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except OSError:
                        pass
                self._conn = conn
            threading.Thread(
                target=_read_loop, args=(conn, self._on_message), daemon=True
            ).start()

    def has_connection(self) -> bool:
        with self._lock:
            return self._conn is not None

    def send(self, msg: dict) -> None:
        with self._lock:
            conn = self._conn
        if conn is None:
            return
        try:
            conn.sendall(protocol.encode(msg))
        except OSError:
            with self._lock:
                if self._conn is conn:
                    self._conn = None

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None
        try:
            self._sock.close()
        except OSError:
            pass


class MessageClient:
    """Connects to a MessageServer and exchanges messages with it."""

    def __init__(self, host: str, port: int):
        self._addr = (host, port)
        self._sock: Optional[socket.socket] = None

    def connect(self, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection(self._addr, timeout=timeout)
        self._sock.settimeout(None)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def start_reader(self, on_message: MessageHandler) -> None:
        assert self._sock is not None
        threading.Thread(
            target=_read_loop, args=(self._sock, on_message), daemon=True
        ).start()

    def send(self, msg: dict) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendall(protocol.encode(msg))
        except OSError:
            pass

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
