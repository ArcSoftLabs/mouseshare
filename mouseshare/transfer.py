"""Bounded, streaming file transfer for authenticated MouseShare peers."""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import protocol

TRANSFER_CHUNK = 32 * 1024
TRANSFER_MAX = 4 * 1024 * 1024 * 1024
TRANSFER_FILES_MAX = 200
SPACE_MARGIN = 64 * 1024 * 1024
ACK_EVERY = 4
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError("name")
    if ("/" in name or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError("name")
    if len(name.encode("utf-8")) > 255:
        raise ValueError("name")
    if name.split(".", 1)[0].upper() in _RESERVED:
        raise ValueError("name")
    if os.name == "nt" and (name.endswith((".", " "))
                            or any(c in '<>"|?*' for c in name)):
        raise ValueError("name")
    return name


def validate_offer(files: object) -> list[dict]:
    if not isinstance(files, list) or not files or len(files) > TRANSFER_FILES_MAX:
        raise ValueError("files")
    clean = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("files")
        name = validate_name(item.get("name"))
        size, digest = item.get("size"), item.get("sha256")
        if (not isinstance(size, int) or isinstance(size, bool)
                or size < 0 or size > TRANSFER_MAX):
            raise ValueError("size")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdefABCDEF" for c in digest)):
            raise ValueError("sha256")
        clean.append({"name": name, "size": size, "sha256": digest.lower()})
    return clean


def destination_directory() -> Path:
    base = Path(os.environ.get("USERPROFILE", str(Path.home()))) if os.name == "nt" else Path.home()
    return base / "Downloads" / "MouseShare"


def _hash_file(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while block := source.read(TRANSFER_CHUNK):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


@dataclass
class _Send:
    peer_id: str
    peer_name: str
    paths: list[Path]
    files: list[dict]
    accepted: threading.Event = field(default_factory=threading.Event)
    acked: threading.Condition = field(default_factory=threading.Condition)
    ack_index: int = -1
    cancelled: bool = False
    failed: bool = False


@dataclass
class _Receive:
    peer_id: str
    peer_name: str
    files: list[dict]
    targets: list[Path]
    parts: list[Path]
    handles: list[object]
    index: int = 0
    next_chunk: int = 0
    chunk_count: int = 0
    written: int = 0
    digest: object = field(default_factory=hashlib.sha256)


class TransferManager:
    def __init__(self, send: Callable[[str, dict], None],
                 publish: Callable[[list[dict]], None], destination: Path | None = None):
        self._send, self._publish = send, publish
        self.destination = destination or destination_directory()
        self._lock = threading.RLock()
        self._sends: dict[str, _Send] = {}
        self._receives: dict[str, _Receive] = {}
        self._views: dict[str, dict] = {}
        self._last_publish = 0.0

    def offer(self, peer_id: str, peer_name: str, paths: list[Path]) -> str:
        metadata = []
        for path in paths:
            size, digest = _hash_file(path)
            metadata.append({"name": validate_name(path.name), "size": size, "sha256": digest})
        validate_offer(metadata)
        transfer_id = uuid.uuid4().hex
        send = _Send(peer_id, peer_name, paths, metadata)
        with self._lock:
            self._sends[transfer_id] = send
            self._add_view(transfer_id, "send", peer_name, metadata, "offered")
        self._send(peer_id, protocol.xfer_offer(transfer_id, metadata))
        threading.Thread(target=self._send_files, args=(transfer_id, send),
                         name="mouseshare-transfer", daemon=True).start()
        return transfer_id

    def receive(self, peer_id: str, peer_name: str, msg: dict, enabled: bool = True) -> None:
        kind, transfer_id = msg.get("t"), msg.get("id")
        if not isinstance(transfer_id, str):
            return
        if kind == "xfer_offer":
            self._receive_offer(peer_id, peer_name, transfer_id, msg, enabled)
        elif kind == "xfer_accept":
            send = self._sends.get(transfer_id)
            if send:
                send.accepted.set()
        elif kind == "xfer_ack":
            send = self._sends.get(transfer_id)
            if send and isinstance(msg.get("i"), int):
                with send.acked:
                    send.ack_index = max(send.ack_index, msg["i"])
                    send.acked.notify_all()
        elif kind == "xfer_chunk":
            self._receive_chunk(transfer_id, msg)
        elif kind == "xfer_done":
            self._receive_done(transfer_id, msg)
        elif kind in {"xfer_cancel", "xfer_reject", "xfer_error"}:
            self._finish_remote(transfer_id, kind, msg.get("reason", ""))

    def cancel(self, transfer_id: str, notify: bool = True) -> None:
        with self._lock:
            send = self._sends.get(transfer_id)
            receive = self._receives.pop(transfer_id, None)
            peer_id = send.peer_id if send else receive.peer_id if receive else None
            if send:
                send.cancelled = True
                send.accepted.set()
                with send.acked:
                    send.acked.notify_all()
                self._sends.pop(transfer_id, None)
            if receive:
                self._cleanup_receive(receive)
            self._set_status(transfer_id, "cancelled", "")
        if notify and peer_id:
            self._send(peer_id, protocol.xfer_cancel(transfer_id))

    def cancel_all(self) -> None:
        with self._lock:
            transfer_ids = list(self._sends) + list(self._receives)
        for transfer_id in set(transfer_ids):
            self.cancel(transfer_id)

    def discard_peer(self, peer_id: str) -> None:
        with self._lock:
            ids = [key for key, item in self._sends.items() if item.peer_id == peer_id]
            ids += [key for key, item in self._receives.items() if item.peer_id == peer_id]
        for transfer_id in set(ids):
            self.cancel(transfer_id, notify=False)
            self._set_status(transfer_id, "failed", "disconnected")

    def _receive_offer(self, peer_id, peer_name, transfer_id, msg, enabled):
        if not enabled:
            self._send(peer_id, protocol.xfer_reject(transfer_id, "disabled"))
            return
        try:
            files = validate_offer(msg.get("files"))
        except ValueError as exc:
            self._send(peer_id, protocol.xfer_reject(transfer_id, str(exc)))
            return
        try:
            existed = self.destination.exists()
            self.destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt" and not existed:
                self.destination.chmod(0o700)
            if shutil.disk_usage(self.destination).free < sum(f["size"] for f in files) + SPACE_MARGIN:
                self._send(peer_id, protocol.xfer_reject(transfer_id, "space"))
                return
            targets, parts = self._reserve(files)
            handles = []
            try:
                for part in parts:
                    handles.append(part.open("xb"))
            except OSError:
                for handle in handles:
                    handle.close()
                for part in parts[:len(handles)]:
                    part.unlink(missing_ok=True)
                raise
        except OSError:
            self._send(peer_id, protocol.xfer_error(transfer_id, "write"))
            return
        receive = _Receive(peer_id, peer_name, files, targets, parts, handles)
        with self._lock:
            self._receives[transfer_id] = receive
            self._add_view(transfer_id, "receive", peer_name, files, "active")
        self._send(peer_id, protocol.xfer_accept(transfer_id))

    def _reserve(self, files):
        targets, parts, chosen = [], [], set()
        for item in files:
            source = Path(item["name"])
            stem, suffix, number = source.stem, source.suffix, 1
            target = self.destination / source.name
            while target.exists() or target in chosen or target.with_name(target.name + ".part").exists():
                number += 1
                target = self.destination / f"{stem} ({number}){suffix}"
            chosen.add(target)
            targets.append(target)
            parts.append(target.with_name(target.name + ".part"))
        return targets, parts

    def _receive_chunk(self, transfer_id, msg):
        try:
            with self._lock:
                receive = self._receives.get(transfer_id)
                if not receive:
                    return
                i, n = msg.get("i"), msg.get("n")
                expected = max(1, (receive.files[receive.index]["size"]
                                   + TRANSFER_CHUNK - 1) // TRANSFER_CHUNK)
                if (not isinstance(i, int) or isinstance(i, bool) or i != receive.next_chunk
                        or not isinstance(n, int) or isinstance(n, bool)
                        or (receive.chunk_count and n != receive.chunk_count)
                        or n != expected or i >= n):
                    raise ValueError
                data = base64.b64decode(msg.get("data", ""), validate=True)
                if self._receives.get(transfer_id) is not receive:
                    return
                if len(data) > TRANSFER_CHUNK:
                    raise ValueError
                if receive.written + len(data) > receive.files[receive.index]["size"]:
                    raise OverflowError
                receive.chunk_count = n
                receive.handles[receive.index].write(data)
                receive.digest.update(data)
                receive.written += len(data)
                receive.next_chunk += 1
                self._advance(transfer_id, receive.index, len(data))
        except (TypeError, ValueError, binascii.Error):
            if transfer_id not in self._receives:
                return
            return self._receive_error(transfer_id, "malformed")
        except OverflowError:
            return self._receive_error(transfer_id, "overrun")
        except OSError:
            return self._receive_error(transfer_id, "write")
        if (i + 1) % ACK_EVERY == 0 or i + 1 == n:
            self._send(receive.peer_id, protocol.xfer_ack(transfer_id, i))

    def _receive_done(self, transfer_id, msg):
        if transfer_id in self._sends:
            return
        error = None
        try:
            with self._lock:
                receive = self._receives.get(transfer_id)
                if not receive or msg.get("file_index") != receive.index:
                    error = "protocol"
                else:
                    handle = receive.handles[receive.index]
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
                    item = receive.files[receive.index]
                    if (receive.digest.hexdigest() != item["sha256"]
                            or receive.parts[receive.index].stat().st_size != item["size"]):
                        error = "integrity"
                    else:
                        self._install(receive.parts[receive.index], receive.targets[receive.index])
                        receive.parts[receive.index] = None
                        receive.index += 1
                        if receive.index == len(receive.files):
                            self._receives.pop(transfer_id, None)
                            self._set_status(transfer_id, "done", "")
                            return
                        receive.next_chunk = 0
                        receive.chunk_count = 0
                        receive.written = 0
                        receive.digest = hashlib.sha256()
        except OSError:
            error = "write"
        if error:
            self._receive_error(transfer_id, error)

    @staticmethod
    def _install(part, target):
        try:
            os.link(part, target)
            os.unlink(part)
            return
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EXDEV, errno.ENOTSUP}:
                raise
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            # This fallback is POSIX-only; Windows relies on os.link on NTFS.
            os.rename(part, target)
        except OSError:
            target.unlink(missing_ok=True)
            raise

    def _receive_error(self, transfer_id, reason):
        if transfer_id in self._sends:
            return
        receive = self._receives.pop(transfer_id, None)
        if receive:
            self._cleanup_receive(receive)
            self._send(receive.peer_id, protocol.xfer_error(transfer_id, reason))
        self._set_status(transfer_id, "failed", reason)

    def _cleanup_receive(self, receive):
        for handle in receive.handles:
            try:
                handle.close()
            except (OSError, AttributeError):
                pass
        for part in receive.parts:
            if part is None:
                continue
            try:
                part.unlink()
            except FileNotFoundError:
                pass

    def _send_files(self, transfer_id, send):
        if not send.accepted.wait(timeout=30):
            self._fail_send(transfer_id, send, "timeout")
            return
        if send.cancelled or send.failed:
            return
        self._set_status(transfer_id, "active", "")
        try:
            for file_index, (path, item) in enumerate(zip(send.paths, send.files)):
                count = max(1, (item["size"] + TRANSFER_CHUNK - 1) // TRANSFER_CHUNK)
                with path.open("rb") as source:
                    for i in range(count):
                        data = source.read(TRANSFER_CHUNK)
                        with send.acked:
                            if send.cancelled or send.failed:
                                return
                            self._send(send.peer_id, protocol.xfer_chunk(
                                transfer_id, i, count, data))
                        self._advance(transfer_id, file_index, len(data))
                        if (i + 1) % ACK_EVERY == 0 or i + 1 == count:
                            with send.acked:
                                if not send.acked.wait_for(
                                        lambda: send.cancelled or send.failed or send.ack_index >= i,
                                        timeout=30):
                                    self._fail_send(transfer_id, send, "timeout")
                                    return
                                if send.cancelled or send.failed:
                                    return
                    with send.acked:
                        send.ack_index = -1
                if send.cancelled or send.failed:
                    return
                self._send(send.peer_id, protocol.xfer_done(transfer_id, file_index))
            if send.cancelled or send.failed:
                return
            self._set_status(transfer_id, "done", "")
            with self._lock:
                self._sends.pop(transfer_id, None)
        except OSError:
            self._send(send.peer_id, protocol.xfer_error(transfer_id, "read"))
            self._set_status(transfer_id, "failed", path.name)
            with self._lock:
                self._sends.pop(transfer_id, None)

    def _fail_send(self, transfer_id, send, reason):
        send.failed = True
        self._send(send.peer_id, protocol.xfer_error(transfer_id, reason))
        self._set_status(transfer_id, "failed", reason)
        with self._lock:
            self._sends.pop(transfer_id, None)

    def _finish_remote(self, transfer_id, kind, reason):
        receive = self._receives.pop(transfer_id, None)
        send = self._sends.get(transfer_id)
        if receive:
            self._cleanup_receive(receive)
        if send:
            if kind == "xfer_cancel":
                send.cancelled = True
            else:
                send.failed = True
            send.accepted.set()
            with send.acked:
                send.acked.notify_all()
        status = "cancelled" if kind == "xfer_cancel" else "failed"
        self._set_status(transfer_id, status, str(reason))
        with self._lock:
            self._sends.pop(transfer_id, None)

    def _add_view(self, transfer_id, direction, peer_name, files, status):
        self._views[transfer_id] = {"id": transfer_id, "direction": direction,
            "peer_name": peer_name, "files": [{"name": f["name"], "size": f["size"], "done": 0} for f in files],
            "bytes_done": 0, "bytes_total": sum(f["size"] for f in files),
            "status": status, "error": ""}
        self._publish_now()

    def _advance(self, transfer_id, file_index, amount):
        with self._lock:
            view = self._views.get(transfer_id)
            if not view:
                return
            view["files"][file_index]["done"] += amount
            view["bytes_done"] += amount
            if time.monotonic() - self._last_publish >= 0.25:
                self._publish_now()

    def _set_status(self, transfer_id, status, error):
        with self._lock:
            if transfer_id in self._views:
                self._views[transfer_id]["status"] = status
                self._views[transfer_id]["error"] = error
                if status in {"done", "failed", "cancelled"}:
                    finished = [key for key, view in self._views.items()
                                if view["status"] in {"done", "failed", "cancelled"}]
                    for key in finished[:-20]:
                        self._views.pop(key, None)
                self._publish_now()

    def _publish_now(self):
        self._last_publish = time.monotonic()
        self._publish([dict(v) for v in self._views.values()])
