"""Clipboard backends and text synchronisation.

The AppKit documentation describes pasteboard semantics and ``changeCount`` but
does not promise that NSPasteboard is safe from worker threads.  MouseShare
therefore takes the defensive route: every macOS pasteboard call is synchronously
marshalled to the main thread with PyObjC's safe NSObject selector helper.  See
https://pyobjc.readthedocs.io/en/latest/api/threading-helpers.html.

The X11 backend supports the ordinary CLIPBOARD selection and the UTF8_STRING,
STRING, and TARGETS targets.  It intentionally omits incremental (INCR) transfers
and times reads out after 200 ms.  Selection-owner changes are observable; a
same-owner rewrite is only observable when it was made through this backend.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import queue
import sys
import threading
import time
import uuid
from typing import Callable, Protocol, runtime_checkable

from . import protocol

CLIPBOARD_POLL = 0.25
CLIPBOARD_INLINE = 32 * 1024
CLIPBOARD_MAX = 1024 * 1024
log = logging.getLogger("mouseshare")


def _invoke_pasteboard_call(self, box):
    try:
        box["result"] = box["call"]()
    except Exception as exc:  # noqa: BLE001
        box["error"] = exc
    finally:
        box["done"].set()


_pasteboard_call_classes = {}


def _pasteboard_call_class(nsobject):
    """Create one PyObjC helper class for each loaded NSObject class."""
    if nsobject not in _pasteboard_call_classes:
        _pasteboard_call_classes[nsobject] = type(
            "PasteboardCall", (nsobject,), {"invoke_": _invoke_pasteboard_call})
    return _pasteboard_call_classes[nsobject]


@runtime_checkable
class Clipboard(Protocol):
    available: bool

    def read_text(self) -> str | None: ...
    def write_text(self, text: str) -> None: ...
    def sequence(self) -> int: ...

    @staticmethod
    def create() -> Clipboard | None:
        try:
            backend: Clipboard
            if sys.platform == "win32":
                backend = WindowsClipboard()
            elif sys.platform == "darwin":
                backend = MacClipboard()
            elif sys.platform.startswith("linux"):
                backend = X11Clipboard()
            else:
                return None
            return backend if backend.available else None
        except Exception:  # noqa: BLE001 - optional OS integration
            log.debug("clipboard backend unavailable", exc_info=True)
            return None


class WindowsClipboard:
    """Win32 Unicode clipboard, with contention retries."""
    available = True
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self, windll=None, sleep: Callable[[float], None] = time.sleep):
        api = windll or ctypes.windll
        self._user32, self._kernel32, self._sleep = api.user32, api.kernel32, sleep
        for owner, name in (
            (self._user32, "GetClipboardData"),
            (self._user32, "SetClipboardData"),
            (self._kernel32, "GlobalAlloc"),
            (self._kernel32, "GlobalLock"),
        ):
            function = getattr(owner, name, None)
            try:
                function.restype = ctypes.c_void_p
            except AttributeError:  # absent/simple fake functions in unit tests
                pass
        signatures = (
            (self._kernel32, "GlobalLock", [ctypes.c_void_p]),
            (self._kernel32, "GlobalUnlock", [ctypes.c_void_p]),
            (self._kernel32, "GlobalFree", [ctypes.c_void_p]),
            (self._kernel32, "GlobalAlloc", [ctypes.c_uint, ctypes.c_size_t]),
            (self._user32, "OpenClipboard", [ctypes.c_void_p]),
            (self._user32, "SetClipboardData", [ctypes.c_uint, ctypes.c_void_p]),
            (self._user32, "IsClipboardFormatAvailable", [ctypes.c_uint]),
        )
        for owner, name, argtypes in signatures:
            function = getattr(owner, name, None)
            try:
                function.argtypes = argtypes
            except AttributeError:  # absent/simple fake functions in unit tests
                pass
        try:
            self._user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
        except AttributeError:  # absent/simple fake functions in unit tests
            pass

    def _open(self) -> None:
        for attempt in range(5):
            if self._user32.OpenClipboard(None):
                return
            if attempt < 4:
                self._sleep(0.01)
        raise OSError("clipboard is locked")

    def sequence(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    def read_text(self) -> str | None:
        self._open()
        try:
            if not self._user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                return "" if not self._user32.EnumClipboardFormats(0) else None
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                raise OSError("could not get clipboard data")
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("could not lock clipboard data")
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def write_text(self, text: str) -> None:
        raw = (text + "\0").encode("utf-16-le")
        self._open()
        handle = None
        try:
            if not self._user32.EmptyClipboard():
                raise OSError("could not empty clipboard")
            handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(raw))
            if not handle:
                raise MemoryError("could not allocate clipboard data")
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("could not lock clipboard data")
            try:
                ctypes.memmove(pointer, raw, len(raw))
            finally:
                self._kernel32.GlobalUnlock(handle)
            if not self._user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                raise OSError("could not set clipboard data")
            handle = None  # ownership passed to the system
        finally:
            if handle:
                try:
                    self._kernel32.GlobalFree(handle)
                finally:
                    self._user32.CloseClipboard()
            else:
                self._user32.CloseClipboard()


class MacClipboard:
    available = True

    def __init__(self):
        import AppKit
        import objc

        self._appkit = AppKit
        self._objc = objc
        self._stop = threading.Event()
        self._helper = _pasteboard_call_class(AppKit.NSObject).alloc().init()

    def _call(self, call):
        if self._stop.is_set():
            raise RuntimeError("clipboard is stopped")
        box = {"call": call, "done": threading.Event()}
        if self._appkit.NSThread.isMainThread():
            self._helper.invoke_(box)
        else:
            self._helper.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
                "invoke:", box, False)
            while not box["done"].wait(0.01):
                if self._stop.is_set():
                    raise RuntimeError("clipboard is stopped")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def stop(self) -> None:
        self._stop.set()

    def _pasteboard(self):
        return self._appkit.NSPasteboard.generalPasteboard()

    def sequence(self) -> int:
        return int(self._call(lambda: self._pasteboard().changeCount()))

    def read_text(self) -> str | None:
        return self._call(lambda: self._pasteboard().stringForType_(
            self._appkit.NSPasteboardTypeString))

    def write_text(self, text: str) -> None:
        def write():
            board = self._pasteboard()
            board.clearContents()
            if not board.setString_forType_(text, self._appkit.NSPasteboardTypeString):
                raise OSError("could not set pasteboard text")
        self._call(write)


class X11Clipboard:
    """Small X11 selection owner; unavailable under a missing X display."""

    def __init__(self):
        self.available = False
        self._text = ""
        self._counter = 0
        self._stop = threading.Event()
        self._selection_notifies = queue.Queue()
        from Xlib import X, Xatom, display
        from Xlib import protocol as xprotocol

        self._X, self._Xatom, self._xprotocol = X, Xatom, xprotocol
        self._display = display.Display()
        root = self._display.screen().root
        self._window = root.create_window(-1, -1, 1, 1, 0, X.CopyFromParent)
        self._clipboard = self._display.intern_atom("CLIPBOARD")
        self._utf8 = self._display.intern_atom("UTF8_STRING")
        self._targets = self._display.intern_atom("TARGETS")
        self._property = self._display.intern_atom("MOUSESHARE_CLIPBOARD")
        self._last_owner = self._owner_id()
        self.available = True
        threading.Thread(target=self._serve, name="mouseshare-x11-clipboard",
                         daemon=True).start()

    def _owner_id(self) -> int:
        owner = self._display.get_selection_owner(self._clipboard)
        return int(getattr(owner, "id", 0) or 0)

    def sequence(self) -> int:
        owner = self._owner_id()
        if owner != self._last_owner:
            self._last_owner = owner
            self._counter += 1
        return self._counter

    def read_text(self) -> str | None:
        owner = self._display.get_selection_owner(self._clipboard)
        if owner == self._X.NONE:
            return ""
        if getattr(owner, "id", None) == self._window.id:
            return self._text
        # A reply that arrived after an earlier read timed out would
        # otherwise be taken as the answer to this one, one owner behind.
        while True:
            try:
                self._selection_notifies.get_nowait()
            except queue.Empty:
                break
        self._window.convert_selection(self._clipboard, self._utf8,
                                       self._property, self._X.CurrentTime)
        self._display.flush()
        try:
            event = self._selection_notifies.get(timeout=0.2)
        except queue.Empty as exc:
            raise TimeoutError("clipboard selection timed out") from exc
        if event.property == self._X.NONE:
            return None
        value = self._window.get_full_property(
            self._property, self._X.AnyPropertyType)
        return bytes(value.value).decode("utf-8") if value else None

    def write_text(self, text: str) -> None:
        self._text = text
        self._window.set_selection_owner(self._clipboard, self._X.CurrentTime)
        self._display.flush()
        self._last_owner = self._window.id
        self._counter += 1

    def _serve(self) -> None:
        while not self._stop.wait(0.01):
            while self._display.pending_events():
                event = self._display.next_event()
                if event.type == self._X.SelectionNotify:
                    self._selection_notifies.put(event)
                    continue
                if event.type != self._X.SelectionRequest:
                    continue
                prop = event.property or event.target
                reply = self._xprotocol.event.SelectionNotify(
                    time=event.time, requestor=event.requestor,
                    selection=event.selection, target=event.target, property=prop)
                requestor = self._display.create_resource_object(
                    "window", event.requestor)
                if event.target == self._targets:
                    requestor.change_property(prop, self._Xatom.ATOM, 32,
                        [self._targets, self._utf8, self._Xatom.STRING])
                elif event.target in (self._utf8, self._Xatom.STRING):
                    encoding = "utf-8" if event.target == self._utf8 else "latin-1"
                    requestor.change_property(prop, event.target, 8,
                                               self._text.encode(encoding, "replace"))
                else:
                    reply.property = self._X.NONE
                requestor.send_event(reply, propagate=False)
                self._display.flush()


class ClipboardSync:
    """Poll one clipboard and exchange validated, bounded protocol messages."""

    def __init__(self, backend: Clipboard, device_id: str, device_name: str,
                 publish: Callable[[dict, str | None], None],
                 notice: Callable[[str], None], active: Callable[[], bool]):
        self.backend = backend
        self.device_id = device_id
        self.device_name = device_name
        self._publish, self._notice, self._active = publish, notice, active
        self._enabled = True
        self._stop = threading.Event()
        try:
            self._last_sequence = backend.sequence()
        except Exception as exc:  # noqa: BLE001
            log.debug("clipboard sequence failed: %s", type(exc).__name__)
            self._last_sequence = 0
        self._last_hash: bytes | None = None
        self._applied_sequence: int | None = None
        self._assemblers: dict[str, protocol.ChunkAssembler] = {}
        self._chunk_meta: dict[str, tuple[str, int, str]] = {}
        self._oversize_noticed = False
        threading.Thread(target=self._watch, name="mouseshare-clipboard",
                         daemon=True).start()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def stop(self) -> None:
        self._stop.set()
        stop = getattr(self.backend, "stop", None)
        if stop is not None:
            stop()

    def discard_peer(self, peer_id: str) -> None:
        self._assemblers.pop(peer_id, None)
        self._chunk_meta.pop(peer_id, None)

    @staticmethod
    def _hash(text: str) -> bytes:
        return hashlib.sha256(text.encode("utf-8")).digest()

    def _watch(self) -> None:
        while not self._stop.wait(CLIPBOARD_POLL):
            if self._enabled and self._active():
                self.poll_once()

    def poll_once(self) -> None:
        try:
            current = self.backend.sequence()
            if current == self._last_sequence:
                return
            self._last_sequence = current
            text = self.backend.read_text()
            if text is None:
                return
            digest = self._hash(text)
            if current == self._applied_sequence or digest == self._last_hash:
                self._applied_sequence = None
                return
            self._last_hash = digest
            self._send(text, current)
        except Exception as exc:  # noqa: BLE001 - clipboard failures are isolated
            log.debug("clipboard read failed: %s", type(exc).__name__)

    def _send(self, text: str, seq: int) -> None:
        data = text.encode("utf-8")
        if len(data) > CLIPBOARD_MAX:
            if not self._oversize_noticed:
                self._notice("Clipboard too large to share")
                self._oversize_noticed = True
            return
        self._oversize_noticed = False
        inline = {"t": "clip", "seq": seq, "text": text,
                  "device_id": self.device_id, "name": self.device_name}
        if (len(data) <= CLIPBOARD_INLINE
                and len(protocol.encode(inline)) <= protocol.MAX_LINE - 256):
            self._publish(inline, None)
            return
        chunk_id = uuid.uuid4().hex
        for msg in protocol.chunk_payload("clip_chunk", chunk_id, data):
            msg.update(seq=seq, device_id=self.device_id, name=self.device_name)
            self._publish(msg, None)

    def receive(self, peer_id: str, source_name: str, msg: dict) -> dict | None:
        if not self._enabled:
            return None
        kind = msg.get("t")
        if kind == "clip":
            text = msg.get("text")
            if (not isinstance(text, str) or not self._valid_meta(msg)):
                log.debug("dropped malformed clip")
                return None
        elif kind == "clip_chunk":
            if not self._valid_meta(msg):
                log.debug("dropped malformed clip_chunk")
                return None
            meta = (msg["device_id"], msg["seq"], source_name)
            if peer_id not in self._assemblers:
                self._assemblers[peer_id] = protocol.ChunkAssembler(CLIPBOARD_MAX)
                self._chunk_meta[peer_id] = meta
            elif self._chunk_meta[peer_id] != meta:
                self.discard_peer(peer_id)
                log.debug("dropped malformed clip_chunk")
                return None
            try:
                data = self._assemblers[peer_id].add(msg)
            except protocol.ProtocolError:
                self.discard_peer(peer_id)
                log.debug("dropped malformed clip_chunk")
                return None
            if data is None:
                return msg
            self.discard_peer(peer_id)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                log.debug("dropped malformed clip_chunk")
                return None
        else:
            return None
        try:
            digest = self._hash(text)
            self._last_hash = digest
            self.backend.write_text(text)
            self._applied_sequence = self.backend.sequence()
            self._last_sequence = self._applied_sequence
        except Exception as exc:  # noqa: BLE001
            log.debug("clipboard write failed: %s", type(exc).__name__)
            return None
        self._notice(f"Clipboard from {source_name}")
        return msg

    @staticmethod
    def _valid_meta(msg: dict) -> bool:
        seq = msg.get("seq")
        return (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0
                and isinstance(msg.get("device_id"), str)
                and bool(msg["device_id"]))
