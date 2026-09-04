"""The macOS event tap, driven with fake pynput and Quartz objects."""
import sys
import time
import types

from mouseshare.capture import InputCapture


class DarwinListener:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.running = False
        self.tap = object()

    def _create_event_tap(self):
        return self.tap

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def join(self, timeout=None):
        return None


class RecordingController:
    def __init__(self):
        self._position = (0, 0)
        self.warps = []

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value
        self.warps.append(value)


def make_quartz(calls):
    return types.SimpleNamespace(
        kCGEventSourceUnixProcessID=1,
        kCGMouseEventDeltaX=2,
        kCGMouseEventDeltaY=3,
        kCGEventMouseMoved=4,
        kCGEventLeftMouseDragged=5,
        kCGEventRightMouseDragged=6,
        kCGEventOtherMouseDragged=7,
        CGEventGetIntegerValueField=lambda event, field: event.get(field, 0),
        CGWarpMouseCursorPosition=lambda point: calls.append(("warp", point)),
        CGAssociateMouseAndMouseCursorPosition=lambda associated: calls.append(
            ("associate", associated)
        ),
        CGEventTapEnable=lambda tap, enabled: calls.append(
            ("tap_enable", tap, enabled)
        ),
    )


def make_capture(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    quartz = make_quartz(calls)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    events = []
    capture = InputCapture(
        on_move=lambda x, y: events.append(("move", x, y)),
        on_delta=lambda dx, dy: events.append(("delta", dx, dy)),
        on_click=lambda *_: None,
        on_scroll=lambda *_: None,
        on_key=lambda *_: None,
    )
    capture._controller = RecordingController()
    capture._start_mouse(types.SimpleNamespace(Listener=DarwinListener))
    return capture, capture._mouse_listener, quartz, calls, events


def event(quartz, dx=0, dy=0, pid=0):
    return {
        quartz.kCGEventSourceUnixProcessID: pid,
        quartz.kCGMouseEventDeltaX: dx,
        quartz.kCGMouseEventDeltaY: dy,
    }


def test_suppressed_moves_use_event_deltas_not_drifting_locations(monkeypatch):
    capture, listener, quartz, _, events = make_capture(monkeypatch)
    capture.suppressing = True
    callbacks = listener.kwargs
    positions = (1281, 1290, 1400, 2100)
    deltas = ((1, 0), (2, 0), (0, 0), (3, -1))
    for x, delta in zip(positions, deltas):
        raw = event(quartz, *delta)
        callbacks["on_move"](x, 720)
        assert callbacks["darwin_intercept"](quartz.kCGEventMouseMoved, raw) is None
    assert events == [("delta", 1, 0), ("delta", 2, 0), ("delta", 3, -1)]
    # Requirement 3: the drifting positions never reach _moved, whose
    # beyond-limit branch would warp the controller back to the anchor.
    assert capture._controller.warps == []


def test_suppressed_dragged_events_report_deltas(monkeypatch):
    capture, listener, quartz, _, events = make_capture(monkeypatch)
    capture.suppressing = True
    raw = event(quartz, 4, -2)
    assert listener.kwargs["darwin_intercept"](
        quartz.kCGEventLeftMouseDragged, raw
    ) is None
    assert events == [("delta", 4, -2)]


def test_injected_mouse_event_passes_without_a_delta(monkeypatch):
    capture, listener, quartz, _, events = make_capture(monkeypatch)
    capture.suppressing = True
    raw = event(quartz, 4, -2, pid=99)
    assert listener.kwargs["darwin_intercept"](
        quartz.kCGEventMouseMoved, raw
    ) is raw
    assert events == []


def test_not_suppressing_reports_absolute_moves_and_passes_events(monkeypatch):
    _, listener, quartz, _, events = make_capture(monkeypatch)
    raw = event(quartz, 4, -2)
    listener.kwargs["on_move"](123, 456)
    assert listener.kwargs["darwin_intercept"](
        quartz.kCGEventMouseMoved, raw
    ) is raw
    assert events == [("move", 123, 456)]


def test_remote_lifecycle_decouples_and_reassociates_on_every_exit(monkeypatch):
    capture, listener, _, calls, _ = make_capture(monkeypatch)
    capture.start_remote((1280, 720), 300)
    assert calls == [("warp", (1280, 720)), ("associate", False)]
    assert capture._controller.warps == []

    capture.stop_remote()
    capture.stop_remote()
    assert calls[-2:] == [("associate", True), ("associate", True)]

    listener.running = True
    capture.stop()
    assert calls[-1] == ("associate", True)


def test_watchdog_reassociates_when_capture_is_lost(monkeypatch):
    capture, listener, _, calls, _ = make_capture(monkeypatch)
    capture._key_listener = DarwinListener()
    capture._key_listener.start()
    capture._start_watchdog(interval=0.01)
    capture.suppressing = True
    listener.stop()
    deadline = time.time() + 1
    while ("associate", True) not in calls and time.time() < deadline:
        time.sleep(0.01)
    capture.stop()
    assert ("associate", True) in calls


def test_disabled_tap_is_reenabled_with_the_created_tap(monkeypatch):
    capture, listener, _, calls, _ = make_capture(monkeypatch)
    tap = listener._create_event_tap()
    raw = {}
    capture.suppressing = True
    assert listener.kwargs["darwin_intercept"](0xFFFFFFFE, raw) is raw
    assert calls == [("tap_enable", tap, True)]


def test_disabled_keyboard_tap_is_reenabled_with_the_created_tap(monkeypatch):
    capture, _, _, calls, _ = make_capture(monkeypatch)
    capture._start_keyboard(types.SimpleNamespace(Listener=DarwinListener))
    listener = capture._key_listener
    tap = listener._create_event_tap()
    raw = {}
    capture.suppressing = True
    assert listener.kwargs["darwin_intercept"](0xFFFFFFFF, raw) is raw
    assert calls == [("tap_enable", tap, True)]
