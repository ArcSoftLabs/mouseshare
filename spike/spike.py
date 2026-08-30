"""Packaging spike for the MouseShare v2 stack decision.

Answers one question: can a *packaged* pywebview app run a webview on the
main thread while pynput listeners and zeroconf run on background threads,
on both macOS and Windows?

A hello-world window would not answer that, so this exercises the whole
seam: a bundled HTML asset resolved through _MEIPASS, a JS->Python->JS
round-trip, a background thread pushing state into the UI, zeroconf up and
down, and both pynput listeners started alongside the GUI loop.

Two modes:

    spike --smoke   everything except the GUI loop; runs on a CI runner
    spike           the full thing; run on a real Mac and a real PC

--smoke exists because CI can build an artifact but cannot log into a
desktop. It proves the bundle is intact and the imports resolve. Only a
real machine proves the window opens, so both are required before the
stack is considered settled.
"""
import json
import os
import queue
import socket
import sys
import threading
import time

RESULTS = queue.Queue()


def asset_dir() -> str:
    """Where the bundled web assets live, frozen or not."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "web")


def check(name: str, fn) -> bool:
    """Run one probe, print a PASS/FAIL line, never raise."""
    try:
        detail = fn()
        print(f"PASS  {name}: {detail}")
        return True
    except Exception as exc:  # noqa: BLE001 - a spike reports, it does not crash
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        return False


# -- probes ----------------------------------------------------------------


def probe_asset() -> str:
    index = os.path.join(asset_dir(), "index.html")
    if not os.path.exists(index):
        raise FileNotFoundError(index)
    return index


def probe_webview_import() -> str:
    import webview

    return f"pywebview {webview.__version__}"


def probe_zeroconf() -> str:
    """Advertise and browse, then tear both down. Exercises the real
    multicast path, not just the import."""
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

    zc = Zeroconf()
    try:
        info = ServiceInfo(
            "_mouseshare._tcp.local.",
            "spike._mouseshare._tcp.local.",
            addresses=[socket.inet_aton("127.0.0.1")],
            port=39471,
            properties={"device_id": "spike"},
        )
        zc.register_service(info)
        browser = ServiceBrowser(zc, "_mouseshare._tcp.local.", handlers=[lambda **kw: None])
        time.sleep(0.5)
        browser.cancel()
        zc.unregister_service(info)
    finally:
        zc.close()
    return "advertised, browsed and torn down"


def probe_monitors() -> str:
    from screeninfo import get_monitors

    mons = get_monitors()
    return "; ".join(f"{m.width}x{m.height}+{m.x}+{m.y}" for m in mons) or "none reported"


def probe_listeners() -> str:
    """Start both pynput listeners. On a CI runner these may be denied --
    macOS needs Accessibility, and neither runner has a real session -- so
    a denial is reported distinctly from an import or bundle failure, which
    is what this spike is actually testing."""
    from pynput import keyboard, mouse

    started = []
    for name, cls in (("mouse", mouse.Listener), ("keyboard", keyboard.Listener)):
        try:
            listener = cls()
            listener.start()
            listener.wait()
            listener.stop()
            started.append(f"{name}=ok")
        except Exception as exc:  # noqa: BLE001
            started.append(f"{name}=denied({type(exc).__name__})")
    return ", ".join(started)


# -- the GUI half ----------------------------------------------------------


class Api:
    """js_api surface. `ping` is the JS->Python->JS round-trip."""

    def ping(self, payload):
        RESULTS.put(("bridge", payload))
        return {"pong": payload, "pid": os.getpid()}

    def report(self, ok, detail):
        RESULTS.put(("js", f"{ok} {detail}"))


def background_push(window):
    """The thing that most often breaks: a non-main thread updating the UI."""
    time.sleep(1.0)
    state = json.dumps({"from": "background", "thread": threading.current_thread().name})
    window.evaluate_js(f"window.onState({state})")
    RESULTS.put(("push", "sent"))


def run_gui() -> int:
    import webview

    api = Api()
    window = webview.create_window(
        "MouseShare spike",
        os.path.join(asset_dir(), "index.html"),
        js_api=api,
        width=640,
        height=480,
    )

    def on_start():
        threading.Thread(target=background_push, args=(window,), daemon=True).start()
        # Listeners run alongside the GUI loop -- the coexistence question.
        check("listeners alongside webview", probe_listeners)

    print("Opening window. Close it to finish the spike.")
    webview.start(on_start, debug=False)

    seen = {}
    while not RESULTS.empty():
        k, v = RESULTS.get()
        seen[k] = v
    print()
    print("bridge round-trip:", seen.get("bridge", "NOT SEEN"))
    print("background push:  ", seen.get("push", "NOT SEEN"))
    print("js confirmation:  ", seen.get("js", "NOT SEEN"))
    ok = {"bridge", "push", "js"} <= seen.keys()
    print("SPIKE GUI OK" if ok else "SPIKE GUI INCOMPLETE")
    return 0 if ok else 1


def run_smoke() -> int:
    print(f"platform={sys.platform} frozen={getattr(sys, 'frozen', False)}")
    results = [
        check("bundled asset", probe_asset),
        check("webview import", probe_webview_import),
        check("zeroconf", probe_zeroconf),
        check("monitors", probe_monitors),
        check("pynput listeners", probe_listeners),
    ]
    # Listener denial is expected on a headless runner and is not a failure
    # of the packaging question this mode exists to answer.
    blocking = results[:4]
    print()
    print("SPIKE SMOKE OK" if all(blocking) else "SPIKE SMOKE FAILED")
    return 0 if all(blocking) else 1


if __name__ == "__main__":
    raise SystemExit(run_smoke() if "--smoke" in sys.argv else run_gui())
