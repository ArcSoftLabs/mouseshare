"""The Windows suppression filter, driven directly.

The filter is the only place that turns raw hook data into deltas, and it
only exists on Windows, so it is built here against fake pynput objects
and called by hand.
"""
import sys
import types

import pytest

from mouseshare.capture import InputCapture


class FakePoint:
    def __init__(self, x, y):
        self.x, self.y = x, y


class FakeData:
    def __init__(self, x, y, flags=0, mouseData=0):
        self.pt = FakePoint(x, y)
        self.flags = flags
        self.mouseData = mouseData


class FakeController:
    def __init__(self):
        self.position = (0, 0)
        self.warps = []

    def __setattr__(self, name, value):
        if name == "position" and "warps" in self.__dict__:
            self.warps.append(value)
        object.__setattr__(self, name, value)


class Suppressed(Exception):
    pass


class FakeListener:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def suppress_event(self):
        raise Suppressed()


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    events = []
    capture = InputCapture(
        on_move=lambda x, y: events.append(("move", x, y)),
        on_delta=lambda dx, dy: events.append(("delta", dx, dy)),
        on_click=lambda b, p: events.append(("click", b, p)),
        on_scroll=lambda dx, dy: events.append(("scroll", dx, dy)),
        on_key=lambda k, v, p: events.append(("key", k, v, p)),
    )
    capture._controller = FakeController()
    fake_mouse = types.SimpleNamespace(Listener=FakeListener)
    capture._start_mouse(fake_mouse)
    return capture, capture._mouse_listener.kwargs["win32_event_filter"], events


WM_MOUSEMOVE = 0x0200


def test_a_move_passes_through_untouched_when_not_remote(rig):
    capture, filt, events = rig
    assert filt(WM_MOUSEMOVE, FakeData(10, 20)) is True
    assert events == []


def test_each_suppressed_move_reports_its_own_offset_from_the_anchor(rig):
    """A suppressed event never reaches the desktop, so the real cursor
    stays parked and every event carries the anchor plus that one event's
    movement. Measured from the previous event instead, what gets
    reported is the difference between two movements -- which oscillates
    around zero, and the peer cursor never travels anywhere."""
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    for x in (962, 961, 963):
        with pytest.raises(Suppressed):
            filt(WM_MOUSEMOVE, FakeData(x, 540))
    assert events == [("delta", 2, 0), ("delta", 1, 0), ("delta", 3, 0)]


def test_a_jump_bigger_than_the_limit_is_not_the_user_moving(rig):
    """The event already in the hook pipeline when suppression began
    still carries the pre-park position, most of a screen away. Reported,
    it flings the peer cursor into a corner before the user has moved."""
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    capture._controller.warps.clear()
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(2559, 341))
    assert events == []
    assert capture._controller.warps == [(960, 540)]


def test_ordinary_movement_does_not_warp_the_real_cursor(rig):
    """A warp per event is a syscall inside the hook that comes straight
    back as another hook event. At a thousand reports a second Windows
    stops calling a hook that slow, and everything dies with it."""
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    capture._controller.warps.clear()
    for x in range(960, 980):
        with pytest.raises(Suppressed):
            filt(WM_MOUSEMOVE, FakeData(x, 540))
    assert capture._controller.warps == []


def test_an_injected_event_is_let_through_without_being_reported(rig):
    """Other tools move the cursor for real. Swallowing that would break
    accessibility software, but it is not the user's hand either."""
    from mouseshare.capture import MOUSE_INJECTED_MASK

    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    assert filt(WM_MOUSEMOVE, FakeData(1100, 600, flags=MOUSE_INJECTED_MASK)) is True
    assert events == []


def test_starting_remote_parks_the_real_cursor_on_the_anchor(rig):
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    assert capture._anchor == (960, 540)
    assert capture._controller.warps[-1] == (960, 540)
    assert capture.suppressing is True
