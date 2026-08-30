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


def main() -> int:
    parser = argparse.ArgumentParser(prog="mouseshare", description=(
        "Share one keyboard and mouse between this machine and another."
    ))
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    import webview

    from .app import App

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

    try:
        webview.start(lambda: app.start(), debug=args.debug)
    finally:
        # Closing the window quits, so this is the one shutdown path, and
        # it must release input before anything else can go wrong.
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
