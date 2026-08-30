"""Reads the keyboard layout on the main thread, once, at startup.

pynput's darwin backend resolves keycodes through Carbon's Text Input
Source APIs (`TISCopyCurrentKeyboardInputSource` and friends). Current
macOS asserts those are called on the main dispatch queue and aborts the
process with SIGTRAP when they are not -- the crash is native, so no
Python handler sees it.

Both of the places we reach them are off the main thread:

  * `Injector.create()` builds a `keyboard.Controller`, whose `__init__`
    calls `get_unicode_to_keycode_map()`. A session starts from the
    socket reader thread, so this aborts the moment a peer connects.
  * pynput's own `Listener._run` opens `keycode_context()` on its
    listener thread, so hosting aborts too.

`main()` calls `prewarm()` before `webview.start()`, which is the last
point at which we are certainly on the main thread. Every later reader is
served from that cache and never reaches Carbon.

The tradeoff is that switching keyboard layout while the app is running
has no effect until it restarts.
"""
import contextlib
import importlib
import logging
import sys

log = logging.getLogger("mouseshare")

_done = False


def prewarm() -> None:
    """Read the layout now and cache it for the threads that cannot."""
    global _done
    if _done or sys.platform != "darwin":
        return
    _done = True
    try:
        util = importlib.import_module("pynput._util.darwin")

        with util.keycode_context() as context:
            cached_context = context
        cached_map = util.get_unicode_to_keycode_map()
    except Exception:  # noqa: BLE001 - a window that opens beats a clean import
        log.exception("could not read the keyboard layout up front")
        return

    @contextlib.contextmanager
    def keycode_context():
        yield cached_context

    def get_unicode_to_keycode_map():
        return dict(cached_map)

    # The keyboard backend does `from pynput._util.darwin import ...`, so it
    # holds its own references; patching only the source module would leave
    # the Carbon-calling originals in place.
    backend = importlib.import_module("pynput.keyboard._darwin")

    for module in (util, backend):
        module.keycode_context = keycode_context
        module.get_unicode_to_keycode_map = get_unicode_to_keycode_map
