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


def test_a_suppressed_move_reports_its_offset_from_the_previous_one(rig):
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    for x in (960, 970, 980):
        with pytest.raises(Suppressed):
            filt(WM_MOUSEMOVE, FakeData(x, 540))
    assert events == [("delta", 10, 0), ("delta", 10, 0)]


def test_the_first_move_after_going_remote_only_sets_the_baseline(rig):
    """It can still carry the position from before the park -- most of a
    screen away -- and reporting that as movement flings the peer cursor
    into a corner before the user has moved at all."""
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(2559, 341))
    assert events == []


def test_moving_inside_the_radius_does_not_warp_the_real_cursor(rig):
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


def test_straying_past_the_radius_warps_the_cursor_back(rig):
    """Left to wander it reaches the screen edge, where the OS clamp eats
    every further move in that direction."""
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    capture._controller.warps.clear()
    for x in (960, 1261):
        with pytest.raises(Suppressed):
            filt(WM_MOUSEMOVE, FakeData(x, 540))
    assert events[-1] == ("delta", 301, 0)
    assert capture._controller.warps == [(960, 540)]


def test_an_injected_move_rebases_instead_of_being_reported(rig):
    """Our own warp comes back as an injected event, and other tools move
    the cursor for real too. Neither is the user's hand, but both change
    where the next real event starts from."""
    from mouseshare.capture import MOUSE_INJECTED_MASK

    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    events.clear()
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(960, 540))
    assert filt(WM_MOUSEMOVE, FakeData(1100, 600, flags=MOUSE_INJECTED_MASK)) is True
    with pytest.raises(Suppressed):
        filt(WM_MOUSEMOVE, FakeData(1105, 600))
    assert events == [("delta", 5, 0)]


def test_starting_remote_parks_the_real_cursor_on_the_anchor(rig):
    capture, filt, events = rig
    capture.start_remote((960, 540), 300)
    assert capture._anchor == (960, 540)
    assert capture._controller.warps[-1] == (960, 540)
    assert capture.suppressing is True
