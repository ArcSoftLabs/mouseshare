"""TCP transport for protocol messages. One host, one client, one link.

Disconnection is a first-class event here rather than something the caller
discovers by trying to send. The session must un-suppress input the moment
the peer goes away, and it can only do that if it is told. The callback
fires exactly once per connection, whatever ends it -- EOF, a read or write
error, a protocol violation, or local shutdown.
"""
import logging
import queue
import socket
import threading
from typing import Callable, Optional

from . import protocol

MessageHandler = Callable[[dict], None]
DisconnectHandler = Callable[[str], None]
log = logging.getLogger("mouseshare")
INBOUND_QUEUE_SIZE = 64


class _Link:
    """One connection plus the guarantee that its disconnect fires once."""

    def __init__(self, sock: socket.socket, on_disconnect: Optional[DisconnectHandler],
                 responder: bool = False):
        self.sock = sock
        self._on_disconnect = on_disconnect
        self._lock = threading.Lock()
        # sendall() can take several syscalls, and the mouse and keyboard
        # listeners forward from separate threads. Without this two frames
        # interleave and the peer drops a stream mid-session.
        self._send_lock = threading.Lock()
        self._closed = False
        self._responder = responder
        self.peer_version: Optional[int] = None
        self._inbound = queue.Queue(maxsize=INBOUND_QUEUE_SIZE)
        self._worker_stop = threading.Event()
        self._worker = None
        self._reader = None

    def start_handler(self, on_message: MessageHandler) -> None:
        self._worker = threading.Thread(
            target=self._handle_loop,
            args=(on_message,),
            name="mouseshare-inbound",
            daemon=True,
        )
        self._worker.start()

    def enqueue(self, msg: dict) -> bool:
        """Queue in wire order, discarding only obsolete positions."""
        while not self._worker_stop.is_set():
            if msg.get("t") == "pos":
                with self._inbound.mutex:
                    if (
                        self._inbound.queue
                        and self._inbound.queue[-1].get("t") == "pos"
                    ):
                        self._inbound.queue[-1] = msg
                        return True
            try:
                self._inbound.put(msg, timeout=0.05)
                return True
            except queue.Full:
                with self._inbound.not_full:
                    oldest_pos = next(
                        (i for i, queued in enumerate(self._inbound.queue)
                         if queued.get("t") == "pos"),
                        None,
                    )
                    if oldest_pos is not None:
                        del self._inbound.queue[oldest_pos]
                        self._inbound.unfinished_tasks -= 1
                        self._inbound.not_full.notify()
        return False

    def _handle_loop(self, on_message: MessageHandler) -> None:
        while not self._worker_stop.is_set():
            try:
                msg = self._inbound.get(timeout=0.05)
            except queue.Empty:
                continue
            if self._worker_stop.is_set():
                self._inbound.task_done()
                return
            try:
                on_message(msg)
            except protocol.ProtocolError:
                self.close("protocol")
                return
            except Exception as exc:  # noqa: BLE001 - isolate application handler
                log.warning("message handler failed (%s)", type(exc).__name__)
            finally:
                self._inbound.task_done()

    def finish(self) -> None:
        """Deliver complete frames received before an orderly peer EOF."""
        self._inbound.join()
        self.close("eof")

    def stop_inbound(self) -> None:
        """Drop queued inbound messages and wait for the handler to stop."""
        self._worker_stop.set()
        with self._inbound.not_full:
            discarded = len(self._inbound.queue)
            self._inbound.queue.clear()
            self._inbound.unfinished_tasks -= discarded
            if self._inbound.unfinished_tasks == 0:
                self._inbound.all_tasks_done.notify_all()
            self._inbound.not_full.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            try:
                self._worker.join(timeout=2.0)
            except RuntimeError:
                # close raced the tiny interval between assigning the
                # Thread object and start() marking it started.
                pass

    def close(self, reason: str) -> bool:
        """Close and report. Returns False if someone else got here first."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
        self.stop_inbound()
        # shutdown() before close(): close() alone does not tear down the
        # connection while another thread is blocked in recv() on this
        # socket, so the peer would never see EOF and the reader would
        # never wake.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self._on_disconnect is not None:
            self._on_disconnect(reason)
        return True

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


def _read_loop(link: _Link, on_message: MessageHandler) -> None:
    buf = protocol.LineBuffer()
    while True:
        try:
            data = link.sock.recv(4096)
        except OSError:
            link.close("error")
            return
        if not data:
            link.finish()
            return
        try:
            for msg in buf.feed(data):
                if link.peer_version is None:
                    advertised = (msg.get("max_v") or msg["v"]
                                  if link._responder
                                  and msg.get("t") == "pair_request" else msg["v"])
                    if (not isinstance(advertised, int)
                            or isinstance(advertised, bool)
                            or advertised < protocol.MIN_VERSION):
                        raise protocol.ProtocolError("invalid maximum version")
                    link.peer_version = min(protocol.VERSION, advertised)
                elif msg["v"] != link.peer_version:
                    raise protocol.ProtocolError("protocol version changed mid-stream")
                if not link.enqueue(msg):
                    return
        except protocol.ProtocolError:
            # A desynchronised stream cannot be trusted to say when to stop
            # suppressing input, so it ends the connection.
            link.close("protocol")
            return


class MessageServer:
    """Accepts a single peer. Further connections are refused, not swapped
    in -- otherwise any machine on the LAN could evict the paired peer."""

    def __init__(
        self,
        host: str,
        port: int,
        on_message: MessageHandler,
        on_disconnect: Optional[DisconnectHandler] = None,
    ):
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(2)  # room to accept-and-refuse a second caller
        self.port = self._sock.getsockname()[1]
        self._link: Optional[_Link] = None
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
                busy = self._link is not None and not self._link.closed
                if not busy:
                    self._link = _Link(conn, self._fire_disconnect, responder=True)
                    link = self._link
            if busy:
                try:
                    conn.sendall(protocol.encode(protocol.pair_err("busy")))
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            link.start_handler(self._on_message)
            link._reader = threading.Thread(
                target=_read_loop, args=(link, self._on_message), daemon=True
            )
            link._reader.start()

    def _fire_disconnect(self, reason: str) -> None:
        with self._lock:
            self._link = None
        if self._on_disconnect is not None:
            self._on_disconnect(reason)

    def has_connection(self) -> bool:
        with self._lock:
            return self._link is not None and not self._link.closed

    @property
    def peer_version(self) -> Optional[int]:
        with self._lock:
            return self._link.peer_version if self._link is not None else None

    def send(self, msg: dict) -> None:
        with self._lock:
            link = self._link
        if link is None or link.closed:
            return
        try:
            with link._send_lock:
                version = link.peer_version or protocol.VERSION
                link.sock.sendall(protocol.encode(msg, version=version))
        except OSError:
            link.close("error")

    def disconnect(self, reason: str = "closed") -> None:
        """Drop the current peer but keep listening.

        Refusing a peer is not a reason to stop being reachable; the user
        may well want to pair with it properly a moment later.
        """
        with self._lock:
            link = self._link
        if link is not None:
            link.close(reason)

    def stop_inbound(self) -> None:
        with self._lock:
            link = self._link
        if link is not None:
            link.stop_inbound()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            link = self._link
        if link is not None:
            link.close("shutdown")
        try:
            self._sock.close()
        except OSError:
            pass


class MessageClient:
    """Connects to a MessageServer and exchanges messages with it."""

    def __init__(self, host: str, port: int):
        self._addr = (host, port)
        self._link: Optional[_Link] = None
        self._first_send = True

    def connect(self, timeout: float = 10.0) -> None:
        sock = socket.create_connection(self._addr, timeout=timeout)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._link = _Link(sock, None)
        self._first_send = True

    def start_reader(
        self,
        on_message: MessageHandler,
        on_disconnect: Optional[DisconnectHandler] = None,
    ) -> None:
        assert self._link is not None
        self._link._on_disconnect = on_disconnect
        self._link.start_handler(on_message)
        self._link._reader = threading.Thread(
            target=_read_loop, args=(self._link, on_message), daemon=True
        )
        self._link._reader.start()

    def is_connected(self) -> bool:
        return self._link is not None and not self._link.closed

    @property
    def peer_version(self) -> Optional[int]:
        return self._link.peer_version if self._link is not None else None

    def send(self, msg: dict) -> None:
        if self._link is None or self._link.closed:
            return
        try:
            with self._link._send_lock:
                first_request = self._first_send and msg.get("t") == "pair_request"
                if first_request:
                    msg = {**msg, "max_v": protocol.VERSION}
                version = (protocol.MIN_VERSION if first_request else
                           self._link.peer_version or protocol.VERSION)
                self._link.sock.sendall(protocol.encode(msg, version=version))
                self._first_send = False
        except OSError:
            self._link.close("error")

    def close(self) -> None:
        if self._link is not None:
            self._link.close("shutdown")
            self._link = None

    def stop_inbound(self) -> None:
        if self._link is not None:
            self._link.stop_inbound()
