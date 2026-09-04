# ruff: noqa: E701, E702, E741  -- throwaway diagnostic script, kept as-is
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, "/mnt/c/Users/A508639/Documents/Claude Projects/mouseshare")
threading.Timer(10, lambda: os._exit(3)).start()

def r(k, v): print(f"RESULT {k}={v}", flush=True)

cap = None
try:
    from mouseshare import linux
    r("session_type", linux.session_type())
    from unittest import mock

    from mouseshare import monitors
    from mouseshare.app import App
    with mock.patch.object(monitors, "enumerate_local", return_value=[]):
        app = App(lambda s: None, cfg_path="/tmp/claude-1000/linux-smoke/cfg.json")
        r("permissions", app.permissions())

    from mouseshare.capture import InputCapture
    events = []
    cap = InputCapture(
        on_move=lambda x, y: events.append(("move", x, y)),
        on_delta=lambda dx, dy: events.append(("delta", dx, dy)),
        on_click=lambda b, p: events.append(("click", b, p)),
        on_scroll=lambda dx, dy: events.append(("scroll", dx, dy)),
        on_key=lambda k, n, p: events.append(("key", k, n, p)),
    )
    cap.start()
    time.sleep(1)
    alive = [bool(l and l.is_alive()) for l in (cap._mouse_listener, cap._key_listener)]
    r("listeners_started", all(alive) and len(alive) == 2)
    r("listener_alive", alive)
    r("warper_alive", cap._linux_warp_thread is not None and cap._linux_warp_thread.is_alive())
    grab_ok = ungrab_ok = False
    try:
        cap.start_remote((100, 100), 50)
        grab_ok = cap._linux_grab._grabbed
        r("suppressing", cap.suppressing)
        time.sleep(0.5)
        r("pointer_after_warp", cap._controller.position)
    finally:
        try:
            cap.stop_remote()
            ungrab_ok = not cap._linux_grab._grabbed
        except Exception as e:
            r("error_ungrab", f"{type(e).__name__}: {e}")
        cap.stop()
    r("grab_ok", grab_ok); r("ungrab_ok", ungrab_ok)
    r("events_seen", len(events))
except Exception as e:
    r("error", f"{type(e).__name__}: {e}")
    traceback.print_exc()
    if cap is not None:
        try: cap.stop()
        except Exception: pass
os._exit(0)
