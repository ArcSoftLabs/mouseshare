"""The macOS main-thread shim.

pynput's darwin backend reads the keyboard layout through Carbon's Text
Input Source APIs. On current macOS those assert they are running on the
main dispatch queue and abort the process with SIGTRAP when they are not.
Both of our uses run off the main thread: `Injector` is built on the
socket reader thread when a session starts, and pynput's own `Listener`
calls `keycode_context()` on its listener thread.

So the layout is read once on the real main thread at startup and every
later reader is served from that cache.
"""
import contextlib
import sys
import types

import pytest

from mouseshare import macos


@pytest.fixture
def fake_pynput(monkeypatch):
    """Stands in for the two darwin modules, which cannot be imported here."""
    calls = []

    @contextlib.contextmanager
    def keycode_context():
        calls.append("context")
        yield ("kbd-type", b"layout-bytes")

    def get_unicode_to_keycode_map():
        calls.append("map")
        return {"a": 0}

    util = types.ModuleType("pynput._util.darwin")
    util.keycode_context = keycode_context
    util.get_unicode_to_keycode_map = get_unicode_to_keycode_map

    # The keyboard backend binds both names at import time, so patching
    # only the module they came from would leave the real ones in place.
    backend = types.ModuleType("pynput.keyboard._darwin")
    backend.keycode_context = keycode_context
    backend.get_unicode_to_keycode_map = get_unicode_to_keycode_map

    monkeypatch.setitem(sys.modules, "pynput._util.darwin", util)
    monkeypatch.setitem(sys.modules, "pynput.keyboard._darwin", backend)
    monkeypatch.setattr(macos, "_done", False)
    return util, backend, calls


def test_prewarm_does_nothing_off_darwin(monkeypatch, fake_pynput):
    util, _, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "win32")
    macos.prewarm()
    assert calls == [], "Carbon was touched on a platform that has none"


def test_the_layout_is_read_once_on_the_calling_thread(monkeypatch, fake_pynput):
    util, backend, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "darwin")
    macos.prewarm()
    assert calls == ["context", "map"]


def test_later_readers_are_served_from_the_cache(monkeypatch, fake_pynput):
    """This is the whole point: the reader that would crash must not reach
    Carbon at all."""
    util, backend, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "darwin")
    macos.prewarm()
    calls.clear()

    with backend.keycode_context() as ctx:
        assert ctx == ("kbd-type", b"layout-bytes")
    assert backend.get_unicode_to_keycode_map() == {"a": 0}
    assert calls == [], "a cached reader called through to Carbon"


def test_the_name_bound_inside_the_keyboard_backend_is_replaced(
    monkeypatch, fake_pynput
):
    """`pynput.keyboard._darwin` does `from pynput._util.darwin import ...`,
    so patching only the source module is invisible to it."""
    util, backend, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "darwin")
    original = backend.keycode_context
    macos.prewarm()
    assert backend.keycode_context is not original
    assert util.keycode_context is not original


def test_prewarm_twice_does_not_read_the_layout_again(monkeypatch, fake_pynput):
    """Calling it a second time must not re-enter Carbon -- by then we may
    no longer be on the main thread."""
    util, backend, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "darwin")
    macos.prewarm()
    calls.clear()
    macos.prewarm()
    assert calls == []


def test_a_failure_to_prewarm_is_not_fatal(monkeypatch, fake_pynput):
    """A missing Carbon symbol must not stop the app from opening; the
    crash it guards against only happens if a session is started."""
    util, backend, calls = fake_pynput
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom():
        raise OSError("no Carbon here")

    util.get_unicode_to_keycode_map = boom
    backend.get_unicode_to_keycode_map = boom
    macos.prewarm()
    assert backend.get_unicode_to_keycode_map is boom
