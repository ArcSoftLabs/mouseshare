import ctypes
import logging
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from mouseshare import protocol
from mouseshare.clipboard import (
    Clipboard,
    ClipboardSync,
    MacClipboard,
    WindowsClipboard,
    X11Clipboard,
)


class FakeClipboard(Clipboard):
    available = True

    def __init__(self, text=None):
        self.text = text
        self.counter = 0

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.text = text
        self.counter += 1

    def sequence(self):
        return self.counter

    def set_text(self, text):
        self.write_text(text)


def wait_for(predicate, timeout=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_fake_backend_satisfies_clipboard_contract():
    backend = FakeClipboard("")
    assert isinstance(backend, Clipboard)
    assert backend.sequence() == 0
    assert backend.read_text() == ""
    backend.write_text("hello")
    assert backend.sequence() == 1
    assert backend.read_text() == "hello"


def test_clipboard_messages_are_forward_compatible_optional_types():
    assert {"clip", "clip_chunk"} <= protocol.OPTIONAL_TYPES


def test_change_publishes_and_remote_apply_does_not_echo():
    backend = FakeClipboard("")
    sent = []
    sync = ClipboardSync(backend, "local", "Local",
                         lambda msg, skip: sent.append((msg, skip)),
                         lambda _notice: None, lambda: True)
    try:
        backend.set_text("one")
        sync.poll_once()
        assert sent[-1][0] == {"t": "clip", "seq": 1, "text": "one",
                               "device_id": "local", "name": "Local"}

        sync.receive("peer", "Other", {"t": "clip", "seq": 8,
            "text": "two", "device_id": "remote"})
        sync.poll_once()
        assert backend.read_text() == "two"
        assert len(sent) == 1
    finally:
        sync.stop()


def test_empty_unicode_multiline_and_chunked_text_round_trip():
    source, target = FakeClipboard(""), FakeClipboard("old")
    wire = []
    receiver = ClipboardSync(target, "target", "Target", lambda *_: None,
                             lambda _notice: None, lambda: True)
    sender = ClipboardSync(source, "source", "Source",
                           lambda msg, _skip: wire.append(msg),
                           lambda _notice: None, lambda: True)
    try:
        for text in ("", "héllo\n世界", "x" * (32 * 1024 + 1)):
            wire.clear()
            source.set_text(text)
            sender.poll_once()
            for msg in wire:
                receiver.receive("source", "Source", msg)
            assert target.read_text() == text
        assert len(wire) == 2
        assert all(msg["t"] == "clip_chunk" for msg in wire)
    finally:
        sender.stop()
        receiver.stop()


def test_over_cap_is_refused_with_one_notice():
    backend = FakeClipboard("")
    sent, notices = [], []
    sync = ClipboardSync(backend, "local", "Local",
                         lambda msg, _skip: sent.append(msg),
                         notices.append, lambda: True)
    try:
        backend.set_text("x" * (1024 * 1024 + 1))
        sync.poll_once()
        backend.set_text("y" * (1024 * 1024 + 1))
        sync.poll_once()
        assert sent == []
        assert notices == ["Clipboard too large to share"]
    finally:
        sync.stop()


def test_disabled_sync_drops_inbound_and_does_not_poll():
    backend = FakeClipboard("local")
    sent = []
    sync = ClipboardSync(backend, "local", "Local",
                         lambda msg, _skip: sent.append(msg),
                         lambda _notice: None, lambda: True)
    try:
        sync.set_enabled(False)
        backend.set_text("changed")
        time.sleep(0.3)
        sync.receive("peer", "Peer", {"t": "clip", "seq": 1,
            "text": "remote", "device_id": "peer"})
        assert sent == []
        assert backend.read_text() == "changed"
    finally:
        sync.stop()


def test_malformed_messages_are_dropped_without_clipboard_content(caplog):
    caplog.set_level(logging.DEBUG, logger="mouseshare")
    backend = FakeClipboard("safe")
    sync = ClipboardSync(backend, "local", "Local", lambda *_: None,
                         lambda _notice: None, lambda: True)
    try:
        sync.receive("peer", "Peer", {"t": "clip", "seq": "bad",
                                      "text": 42, "device_id": "peer"})
        assert backend.read_text() == "safe"
        assert "dropped malformed clip" in caplog.text
    finally:
        sync.stop()


def test_two_linked_clipboards_settle_after_exactly_one_exchange():
    left, right = FakeClipboard("old"), FakeClipboard("old")
    exchanges = []
    left_sync = right_sync = None

    def left_send(msg, _skip):
        exchanges.append(msg)
        right_sync.receive("left", "Left", msg)

    left_sync = ClipboardSync(left, "left", "Left", left_send,
                              lambda _notice: None, lambda: True)
    right_sync = ClipboardSync(right, "right", "Right", lambda *_: None,
                               lambda _notice: None, lambda: True)
    try:
        left.set_text("new")
        left_sync.poll_once()
        right_sync.poll_once()
        assert right.read_text() == "new"
        assert len(exchanges) == 1
    finally:
        left_sync.stop()
        right_sync.stop()


class FakeUser32:
    def __init__(self, opens, format_available=0, formats=0):
        self.opens = iter(opens)
        self.format_available = format_available
        self.formats = formats
        self.closed = 0

    def OpenClipboard(self, _owner):
        return next(self.opens)

    def CloseClipboard(self):
        self.closed += 1

    def IsClipboardFormatAvailable(self, _kind):
        return self.format_available

    def EnumClipboardFormats(self, _kind):
        return self.formats

    def GetClipboardSequenceNumber(self):
        return 7


def test_windows_open_retries_and_empty_clipboard_closes():
    user32 = FakeUser32([0, 0, 1])
    sleeps = []
    backend = WindowsClipboard(SimpleNamespace(user32=user32, kernel32=object()),
                               sleeps.append)
    assert backend.sequence() == 7
    assert backend.read_text() == ""
    assert sleeps == [0.01, 0.01]
    assert user32.closed == 1


def test_windows_non_text_clipboard_returns_none_and_closes():
    user32 = FakeUser32([1], formats=2)
    backend = WindowsClipboard(SimpleNamespace(user32=user32, kernel32=object()))
    assert backend.read_text() is None
    assert user32.closed == 1


class FakeCtypesFunction:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(self, *args)


def test_windows_write_configures_x64_handle_signatures():
    allocation = ctypes.create_unicode_buffer(" " * 20)
    large_handle = 2**32 + 17

    def lock(function, handle):
        if handle > 2**31 and not hasattr(function, "argtypes"):
            raise ctypes.ArgumentError(OverflowError("int too long to convert"))
        return ctypes.addressof(allocation)

    kernel32 = SimpleNamespace(
        GlobalAlloc=FakeCtypesFunction(lambda _fn, _flags, _size: large_handle),
        GlobalLock=FakeCtypesFunction(lock),
        GlobalUnlock=FakeCtypesFunction(lambda _fn, _handle: 1),
        GlobalFree=FakeCtypesFunction(lambda _fn, _handle: 0),
    )
    user32 = SimpleNamespace(
        OpenClipboard=FakeCtypesFunction(lambda _fn, _owner: 1),
        EmptyClipboard=lambda: 1,
        SetClipboardData=FakeCtypesFunction(
            lambda _fn, _kind, handle: handle),
        CloseClipboard=lambda: None,
        IsClipboardFormatAvailable=FakeCtypesFunction(
            lambda _fn, _kind: 1),
        GetClipboardData=FakeCtypesFunction(lambda _fn, _kind: large_handle),
        GetClipboardSequenceNumber=FakeCtypesFunction(lambda _fn: 1),
    )
    backend = WindowsClipboard(SimpleNamespace(user32=user32, kernel32=kernel32))

    backend.write_text("hello")

    assert kernel32.GlobalLock.argtypes == [ctypes.c_void_p]
    assert user32.SetClipboardData.argtypes == [ctypes.c_uint, ctypes.c_void_p]


def test_windows_write_closes_clipboard_when_global_free_raises():
    user32 = FakeUser32([1])
    user32.EmptyClipboard = lambda: 1
    user32.SetClipboardData = lambda _kind, _handle: 1
    kernel32 = SimpleNamespace(
        GlobalAlloc=lambda _flags, _size: 123,
        GlobalLock=lambda _handle: 0,
        GlobalUnlock=lambda _handle: 1,
        GlobalFree=lambda _handle: (_ for _ in ()).throw(RuntimeError("free")),
    )
    backend = WindowsClipboard(SimpleNamespace(user32=user32, kernel32=kernel32))

    with pytest.raises(RuntimeError, match="free"):
        backend.write_text("hello")

    assert user32.closed == 1


def test_macos_backend_imports_lazily_and_uses_pasteboard(monkeypatch):
    class Board:
        text = None
        count = 0

        def changeCount(self):
            return self.count

        def stringForType_(self, _kind):
            return self.text

        def clearContents(self):
            self.count += 1

        def setString_forType_(self, text, _kind):
            self.text = text
            return True

    board = Board()

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    appkit = SimpleNamespace(
        NSObject=NSObject,
        NSThread=SimpleNamespace(isMainThread=lambda: True),
        NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board),
        NSPasteboardTypeString="public.utf8-plain-text",
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace())
    monkeypatch.setattr(sys, "platform", "darwin")
    backend = Clipboard.create()
    backend.write_text("héllo")
    assert backend.read_text() == "héllo"
    assert backend.sequence() == 1


def test_macos_marshals_from_worker_and_stop_releases_waiter(monkeypatch):
    scheduled = threading.Event()
    calls = []

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
                self, selector, box, wait):
            calls.append((selector, box, wait))
            scheduled.set()

    appkit = SimpleNamespace(
        NSObject=NSObject,
        NSThread=SimpleNamespace(isMainThread=lambda: False),
        NSPasteboard=SimpleNamespace(generalPasteboard=lambda: None),
        NSPasteboardTypeString="public.utf8-plain-text",
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace())
    backend = MacClipboard()
    errors = []
    worker = threading.Thread(target=lambda: _capture_error(
        errors, backend.sequence))
    worker.start()
    assert scheduled.wait(1)

    backend.stop()
    worker.join(1)

    assert not worker.is_alive()
    assert isinstance(errors[0], RuntimeError)
    assert calls[0][0] == "invoke:"
    assert calls[0][2] is False
    assert callable(calls[0][1]["call"])


def _capture_error(errors, call):
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_x11_serve_thread_hands_selection_notify_to_reader():
    events = queue.Queue()
    X = SimpleNamespace(NONE=0, SelectionNotify=1, SelectionRequest=2,
                        CurrentTime=0, AnyPropertyType=0)

    class Window:
        id = 10

        def convert_selection(self, *_args):
            events.put(SimpleNamespace(type=X.SelectionNotify, property=4))

        def get_full_property(self, *_args):
            return SimpleNamespace(value="foreign text".encode())

    class Display:
        def get_selection_owner(self, _selection):
            return SimpleNamespace(id=20)

        def flush(self):
            pass

        def pending_events(self):
            return not events.empty()

        def next_event(self):
            return events.get_nowait()

    backend = X11Clipboard.__new__(X11Clipboard)
    backend._X = X
    backend._display = Display()
    backend._window = Window()
    backend._clipboard, backend._utf8, backend._property = 2, 3, 4
    backend._selection_notifies = queue.Queue()
    backend._stop = threading.Event()
    worker = threading.Thread(target=backend._serve)
    worker.start()
    try:
        assert backend.read_text() == "foreign text"
    finally:
        backend._stop.set()
        worker.join(1)


def test_linux_backend_fake_module_reports_unavailable_display(monkeypatch):
    xlib = SimpleNamespace(
        X=SimpleNamespace(), Xatom=SimpleNamespace(),
        display=SimpleNamespace(Display=lambda: (_ for _ in ()).throw(
            OSError("no display"))),
        protocol=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setattr(sys, "platform", "linux")
    assert Clipboard.create() is None


def test_sync_logs_never_contain_clipboard_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="mouseshare")
    payload = "SECRET-CONTENT-987654"
    backend = FakeClipboard("")
    sync = ClipboardSync(backend, "local", "Local", lambda *_: None,
                         lambda _notice: None, lambda: True)
    raised = []
    try:
        backend.set_text(payload)
        sync.poll_once()
        sync.receive("peer", "Peer", {"t": "clip", "seq": 2,
            "text": payload, "device_id": "peer"})
        malformed = "MALFORMED-SECRET-24680"
        sync.receive("peer", "Peer", {"t": "clip", "seq": "bad",
            "text": malformed, "device_id": "peer"})
        backend.write_text = lambda text: (_ for _ in ()).throw(
            RuntimeError(text))
        try:
            sync.receive("peer", "Peer", {"t": "clip", "seq": 3,
                "text": payload, "device_id": "peer"})
        except Exception as exc:  # pragma: no cover - receive must isolate it
            raised.append(repr(exc))
        assert all(payload not in record.getMessage() for record in caplog.records)
        assert malformed not in caplog.text
        assert all(payload not in text and malformed not in text for text in raised)
    finally:
        sync.stop()
