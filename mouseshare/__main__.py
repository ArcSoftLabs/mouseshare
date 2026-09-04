"""Entry point.

The webview owns the main thread -- pywebview requires it, and on macOS
Cocoa does too. The application's own threads (discovery, the socket
reader, the input listeners) are started from the GUI loop's `on_start`
callback and never touch the window; they publish state, and this module
is the only place that turns a snapshot into JavaScript.
"""
import argparse
import json
import logging
import os
import sys

from . import __version__


def web_dir() -> str:
    """The bundled UI, frozen or not."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    frozen = os.path.join(base, "ui", "web")
    return frozen if os.path.exists(frozen) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ui", "web"
    )


def smoke() -> int:
    """Prove a packaged build actually works, without a desktop.

    A CI job that only checks the artifact exists cannot catch a missing
    hidden import, a web asset that did not get bundled, or a platform
    backend that fails to load once frozen -- which are exactly the ways
    PyInstaller builds break. This runs inside the frozen app and exercises
    each of them. Only the window itself needs a real machine.
    """
    checks, failed = [], False
    index = os.path.join(web_dir(), "index.html")
    for name, probe in (
        ("bundled UI", lambda: index if os.path.exists(index) else _missing(index)),
        # No `importlib.metadata` version check: a frozen app ships modules
        # without package metadata, so asking for a version there fails on
        # a perfectly good build. Importing the backend is the real test.
        ("webview backend", _probe_backend),
        ("zeroconf", _probe_zeroconf),
        ("monitors", _probe_monitors),
        ("pynput", _probe_pynput),
    ):
        try:
            checks.append(f"PASS  {name}: {probe()}")
        except Exception as exc:  # noqa: BLE001
            checks.append(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failed = True
    print(f"platform={sys.platform} frozen={getattr(sys, 'frozen', False)}")
    print("\n".join(checks))
    print("SMOKE FAILED" if failed else "SMOKE OK")
    return 1 if failed else 0


def _missing(path: str):
    raise FileNotFoundError(path)


def _probe_backend() -> str:
    """Import this platform's webview backend directly.

    That import is what fails when PyInstaller misses the platform module,
    and unlike `guilib.initialize()` it needs no display and no guessing at
    an internal API. Linux's GTK backend requires the system packages
    python3-gi, gir1.2-webkit2-4.1 (or 4.0), and gir1.2-gtk-3.0.
    """
    import importlib

    name = {
        "win32": "webview.platforms.edgechromium",
        "darwin": "webview.platforms.cocoa",
        "linux": "webview.platforms.gtk",
    }.get(sys.platform)
    if name is None:
        return f"no backend expected on {sys.platform}"
    importlib.import_module(name)
    return name


def _probe_zeroconf() -> str:
    from zeroconf import Zeroconf

    zc = Zeroconf()
    zc.close()
    return "started and closed"


def _probe_monitors() -> str:
    from . import config, monitors

    found = monitors.enumerate_local(config.load(config.default_path()).device_id)
    return ", ".join(f"{m.w}x{m.h}+{m.x}+{m.y}" for m in found)


def _probe_pynput() -> str:
    try:
        from pynput import keyboard, mouse
    except ImportError as exc:
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            return f"unavailable without DISPLAY: {exc}"
        raise

    # A CI runner has no input session, so a denial here is expected and is
    # not what this probe is for -- it is checking that the platform
    # backends were bundled and import.
    return f"{mouse.Listener.__module__}, {keyboard.Listener.__module__}"


def log_path() -> str:
    """Where --debug writes its log.

    A packaged app is launched by the desktop, not from a shell, so its
    standard output goes nowhere. Redirecting it by launching the binary
    by hand is not a workaround either: macOS then treats the launching
    process as responsible for the app, and the accessibility grants that
    let it inject anything at all stop applying.
    """
    if sys.platform.startswith("linux"):
        from . import linux

        path = linux.log_path()
    else:
        base = (
            os.path.expanduser("~/Library/Logs/MouseShare")
            if sys.platform == "darwin"
            else os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MouseShare"
            )
        )
        path = os.path.join(base, "debug.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mouseshare", description=(
        "Share one keyboard and mouse between this machine and another."
    ))
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument(
        "--smoke", action="store_true",
        help="check the install without opening a window, then exit",
    )
    args = parser.parse_args()

    if args.smoke:
        return smoke()

    handlers = [logging.StreamHandler()]
    if args.debug:
        handlers.append(logging.FileHandler(log_path(), mode="w"))
        print(f"debug log: {log_path()}", flush=True)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    import webview

    from . import macos
    from .app import App

    # Must happen here: webview.start() runs `boot` on another thread, and
    # every thread that later needs the keyboard layout would abort the
    # process reading it. This is the last certain main thread.
    macos.prewarm()

    window = None

    def deliver(snapshot: dict) -> None:
        if window is not None:
            window.evaluate_js(f"window.onState({json.dumps(snapshot)})")

    app = App(deliver)
    window = webview.create_window(
        "MouseShare",
        os.path.join(web_dir(), "index.html"),
        js_api=app,
        width=1020,
        height=720,
        min_size=(860, 580),
        background_color="#0d0e12",
    )

    def boot() -> None:
        # pywebview swallows exceptions from this callback, which would
        # leave a window rendering over a dead application. Anything that
        # escapes start() is reported into the UI instead.
        try:
            app.start()
        except Exception as exc:  # noqa: BLE001
            logging.exception("startup failed")
            app.state.set(error=f"MouseShare could not start: {exc}")

    try:
        kwargs = {"debug": args.debug}
        if sys.platform.startswith("linux"):
            kwargs["gui"] = "gtk"
        webview.start(boot, **kwargs)
    finally:
        # Closing the window quits, so this is the one shutdown path, and
        # it must release input before anything else can go wrong.
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
