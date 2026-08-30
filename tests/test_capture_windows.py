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


def test_a_suppressed_move_reports_its_offset_from_the_anchor(rig):
    capture, filt, events = rig
    capture.start_remote((960, 540))
    events.clear()
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(970, 545))
    assert events == [("delta", 10, 5)]


def test_each_move_is_measured_from_the_anchor_not_the_previous_event(rig):
    """Suppressed events never move the real cursor, so every hook event
    carries anchor+delta. Measuring from the previous event would halve
    the second move."""
    capture, filt, events = rig
    capture.start_remote((960, 540))
    events.clear()
    for x in (970, 980, 990):
        with pytest.raises(Suppressed):
            filt(WM_MOUSEMOVE, FakeData(x, 540))
    assert events == [("delta", 10, 0), ("delta", 20, 0), ("delta", 30, 0)]


def test_the_cursor_is_put_back_on_the_anchor_after_every_delta(rig):
    """The real cursor drifts off the anchor -- injected events move it,
    and events already in flight when suppression began carry the old
    position. Nothing else closes that gap, so every later event would
    repeat the same bias and pin the peer cursor in a corner."""
    capture, filt, events = rig
    capture.start_remote((960, 540))
    capture._controller.warps.clear()
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(970, 545))
    assert capture._controller.warps == [(960, 540)]


def test_an_injected_event_is_let_through_without_being_reported(rig):
    """Our own warp comes back as an injected event. Reporting it would
    double every movement."""
    from mouseshare.capture import MOUSE_INJECTED_MASK

    capture, filt, events = rig
    capture.start_remote((960, 540))
    events.clear()
    assert filt(WM_MOUSEMOVE, FakeData(960, 540, flags=MOUSE_INJECTED_MASK)) is True
    assert events == []


def test_starting_remote_parks_the_real_cursor_on_the_anchor(rig):
    capture, filt, events = rig
    capture.start_remote((960, 540))
    assert capture._anchor == (960, 540)
    assert capture._controller.warps[-1] == (960, 540)
    assert capture.suppressing is True
