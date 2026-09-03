import sys
import types

from mouseshare.capture import InputCapture, key_to_wire


class FakeKey:
    """Stands in for a `pynput.keyboard.Key` enum member."""

    def __init__(self, name):
        self.name = name


class FakeKeyCode:
    """Stands in for a `pynput.keyboard.KeyCode`."""

    def __init__(self, char=None):
        self.char = char


def test_a_named_key_travels_as_a_special():
    assert key_to_wire(FakeKey("shift_l"), FakeKey) == ("special", "shift_l")


def test_a_printable_key_travels_as_a_character():
    assert key_to_wire(FakeKeyCode("a"), FakeKey) == ("char", "a")


def test_an_unresolvable_key_is_dropped_rather_than_sent_as_junk():
    """Sending a key the other machine cannot resolve would leave the
    injector unable to release it later."""
    assert key_to_wire(FakeKeyCode(None), FakeKey) is None
    assert key_to_wire(None, FakeKey) is None


def test_a_dead_key_with_no_character_is_dropped():
    assert key_to_wire(FakeKeyCode(""), FakeKey) is None


class DarwinListener:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


def test_mouse_intercept_cannot_consume_pending_keyboard_event(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_quartz = types.SimpleNamespace(
        CGEventGetIntegerValueField=lambda event, field: 0,
        kCGEventSourceUnixProcessID=1,
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)
    capture = InputCapture(
        on_move=lambda *_: None,
        on_delta=lambda *_: None,
        on_click=lambda *_: None,
        on_scroll=lambda *_: None,
        on_key=lambda *_: None,
        on_escape=lambda: capture.stop_remote(),
    )
    capture._key_enum = FakeKey
    listeners = types.SimpleNamespace(Listener=DarwinListener)
    capture._start_mouse(listeners)
    capture._start_keyboard(listeners)
    capture.suppressing = True
    keyboard = capture._key_listener.kwargs
    key = FakeKey("ctrl_l")
    for callback in (keyboard["on_press"], keyboard["on_release"], keyboard["on_press"]):
        callback(key)
        assert keyboard["darwin_intercept"](0, object()) is None

    # The completing release callback stops suppression, but its matching
    # intercept still has to swallow that release from the local OS.
    keyboard["on_release"](key)
    assert capture._consume_current is True

    mouse_event = object()
    assert capture._mouse_listener.kwargs["darwin_intercept"](0, mouse_event) is mouse_event
    assert keyboard["darwin_intercept"](0, object()) is None
    assert capture._consume_current is False
