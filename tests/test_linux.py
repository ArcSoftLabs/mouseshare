import os
import sys
import types

import pytest

from mouseshare import __main__ as entrypoint
from mouseshare import config, linux
from mouseshare.app import App
from mouseshare.capture import InputCapture
from mouseshare.inject import button_from_wire, button_to_wire


def test_session_detection(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert linux.session_type() == "x11"

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert linux.session_type() == "x11"

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert linux.session_type() == "wayland"

    monkeypatch.delenv("XDG_SESSION_TYPE")
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert linux.session_type() == "wayland"

    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert linux.session_type() == "none"


def test_xdg_paths_and_legacy_config_migration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    old = home / ".mouseshare" / "config.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"device_id": "kept", "name": "Old install"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config, "LEGACY_PATH", old)

    expected = tmp_path / "cfg" / "mouseshare" / "config.json"
    loaded = config.load_or_create()

    assert linux.config_dir() == expected.parent
    assert linux.log_path() == tmp_path / "state" / "mouseshare" / "debug.log"
    assert loaded.device_id == "kept"
    assert config.load(expected).name == "Old install"
    assert old.exists()
    if os.name != "nt":  # Windows has no POSIX mode bits to check
        assert expected.parent.stat().st_mode & 0o777 == 0o700


class FakeRoot:
    def __init__(self, pointer=0, keyboard=0):
        self.pointer = pointer
        self.keyboard = keyboard
        self.calls = []

    def grab_pointer(self, *args, **kwargs):
        self.calls.append(("grab_pointer", args, kwargs))
        return self.pointer

    def grab_keyboard(self, *args, **kwargs):
        self.calls.append("grab_keyboard")
        return self.keyboard

    def warp_pointer(self, x, y):
        self.calls.append(("warp", x, y))


class FakeDisplay:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.events = []

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def ungrab_pointer(self, _time):
        self.calls.append("ungrab_pointer")

    def ungrab_keyboard(self, _time):
        self.calls.append("ungrab_keyboard")

    def sync(self):
        self.calls.append("sync")

    def flush(self):
        self.calls.append("flush")

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        return self.events.pop(0)


def fake_x():
    return types.SimpleNamespace(
        GrabSuccess=0, GrabModeAsync=1, NONE=0, CurrentTime=0,
    )


def test_xgrab_is_idempotent_and_releases_both_devices():
    display = FakeDisplay(FakeRoot())
    grab = linux.XGrab(display=display, x=fake_x())
    grab.grab()
    grab.grab()
    grab.ungrab()
    grab.ungrab()
    assert [call[0] if isinstance(call, tuple) else call
            for call in display.root.calls] == ["grab_pointer", "grab_keyboard"]
    assert display.root.calls[0][1][1] == 0
    assert display.calls.count("ungrab_pointer") == 1
    assert display.calls.count("ungrab_keyboard") == 1


def test_xgrab_releases_pointer_when_keyboard_grab_fails():
    display = FakeDisplay(FakeRoot(keyboard=1))
    grab = linux.XGrab(display=display, x=fake_x())
    with pytest.raises(RuntimeError, match="keyboard"):
        grab.grab()
    assert "ungrab_pointer" in display.calls


def test_xgrab_reports_pointer_grab_failure_without_grabbing_keyboard():
    display = FakeDisplay(FakeRoot(pointer=1))
    grab = linux.XGrab(display=display, x=fake_x())
    with pytest.raises(RuntimeError, match="pointer"):
        grab.grab()
    assert all(call != "grab_keyboard" for call in display.root.calls)


def test_warp_and_ungrab_drain_queued_events():
    display = FakeDisplay(FakeRoot())
    grab = linux.XGrab(display=display, x=fake_x())
    grab.grab()
    display.events.extend(object() for _ in range(100))
    grab.warp((10, 20))
    assert display.events == []
    display.events.extend(object() for _ in range(100))
    grab.ungrab()
    assert display.events == []
    assert "flush" in display.calls


@pytest.mark.parametrize("release", ["stop_remote", "stop"])
def test_capture_remote_lifecycle_grabs_and_ungrabs(release, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")  # the grab path is linux-only
    display = FakeDisplay(FakeRoot())
    grab = linux.XGrab(display=display, x=fake_x())
    capture = InputCapture(*([lambda *_: None] * 5))
    capture._linux_grab = grab
    capture._controller = types.SimpleNamespace(position=None)

    capture.start_remote((10, 20), 5)
    getattr(capture, release)()

    assert any(isinstance(call, tuple) and call[0] == "grab_pointer"
               for call in display.root.calls)
    assert "ungrab_pointer" in display.calls


def test_capture_stop_stops_listeners_when_ungrab_fails():
    stopped = []
    listener = types.SimpleNamespace(stop=lambda: stopped.append(True))
    capture = InputCapture(*([lambda *_: None] * 5))
    capture._linux_grab = types.SimpleNamespace(
        ungrab=lambda: (_ for _ in ()).throw(OSError("dead display"))
    )
    capture._mouse_listener = listener
    capture._key_listener = listener

    capture.stop()

    assert stopped == [True, True]


def test_linux_motion_warps_outside_callback_and_ignores_warp_event(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    deltas = []
    capture = InputCapture(
        lambda *_: None, lambda *d: deltas.append(d), lambda *_: None,
        lambda *_: None, lambda *_: None,
    )
    capture._controller = types.SimpleNamespace(position=(0, 0))
    capture._anchor = (100, 100)
    capture._limit = 50
    queued = []
    capture._queue_linux_warp = lambda: queued.append(True)
    capture.suppressing = True

    capture._moved(100, 100)
    capture._moved(104, 97)
    capture._moved(100, 100)

    assert deltas == [(4, -3)]
    assert queued == [True]


def test_linux_warp_queue_coalesces_pending_requests():
    capture = InputCapture(*([lambda *_: None] * 5))
    capture._anchor = (100, 100)
    capture._linux_warps = __import__("queue").Queue(maxsize=1)

    for _ in range(100):
        capture._queue_linux_warp()

    assert capture._linux_warps.qsize() == 1


def test_linux_listeners_do_not_request_pynput_suppression(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    made = []

    class Listener:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def start(self):
            pass

    capture = InputCapture(*([lambda *_: None] * 5))
    module = types.SimpleNamespace(Listener=Listener)
    capture._start_mouse(module)
    capture._start_keyboard(module)

    assert all("suppress" not in kwargs for kwargs in made)
    assert all("win32_event_filter" not in kwargs for kwargs in made)
    assert all("darwin_intercept" not in kwargs for kwargs in made)


def test_wayland_capture_refuses_before_importing_pynput(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delitem(sys.modules, "pynput", raising=False)
    capture = InputCapture(*([lambda *_: None] * 5))
    with pytest.raises(RuntimeError, match="X11 session"):
        capture.start()


def test_linux_permissions_explain_each_session_type(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux, "session_type", lambda: "x11")
    assert App.permissions(object()) == {"needed": False, "items": []}
    monkeypatch.setattr(linux, "session_type", lambda: "wayland")
    refused = App.permissions(object())
    assert refused == {"needed": True, "items": [{
        "key": "session", "label": "X11 session",
        "why": ("MouseShare needs an X11 session on Linux "
                "(log out and choose 'Ubuntu on Xorg')"),
        "granted": False,
    }]}
    assert "Ubuntu on Xorg" in App.open_permissions(object(), "wayland")
    monkeypatch.setattr(linux, "session_type", lambda: "none")
    no_display = App.permissions(object())
    assert no_display["items"][0]["key"] == "session"
    assert no_display["items"][0]["granted"] is False


def test_linux_backend_smoke_probe_imports_gtk(monkeypatch):
    imported = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("importlib.import_module", lambda name: imported.append(name))
    assert entrypoint._probe_backend() == "webview.platforms.gtk"
    assert imported == ["webview.platforms.gtk"]


def test_linux_backend_smoke_probe_propagates_a_missing_gtk(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("importlib.import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        entrypoint._probe_backend()


@pytest.mark.parametrize(
    ("name", "platform", "expected"),
    [("x1", "linux", "button8"), ("x2", "linux", "button9")],
)
def test_button_names_are_translated_at_the_wire_boundary(name, platform, expected):
    assert button_from_wire(name, platform) == expected
    assert button_to_wire(expected, platform) == name


@pytest.mark.parametrize(
    ("name", "platform", "expected"),
    [("x1", "win32", "x1"), ("x2", "darwin", "x2"),
     ("button8", "win32", "x1"), ("button9", "win32", "x2")],
)
def test_button_wire_contract_is_canonical_and_backward_compatible(
        name, platform, expected):
    assert button_to_wire(name, platform) == name
    assert button_from_wire(name, platform) == expected
