# Task L1 — rework round 1 (review findings to fix)

Repository: current working directory. Your L1 changes are in the uncommitted working tree (`mouseshare/linux.py`, `tests/test_linux.py` new). An independent reviewer returned these findings. Fix all of them; nothing else. Do NOT commit. Preservation rules apply. Live evidence available to you: on this machine's WSLg (XWayland) server the current code grabbed, warped and ungrabbed successfully (`/tmp/claude-1000/linux-smoke/smoke.py`, read-only reference); keep that working.

## [critical] Unbounded event-queue growth on the XGrab connection
`linux.py:49-57` grabs with `PointerMotion|ButtonPress|ButtonRelease`, and a keyboard grab always delivers keys to the grabbing client; nothing ever calls `next_event()`, and every `sync()` (`linux.py:72,80,85`) parses incoming events into python-xlib's `event_queue` — one Python object per input event for the whole remote session.
**Fix:** pass event mask `0` to `grab_pointer` (XRecord records device events at the server core regardless of delivery, so pynput still sees everything), and drain in `warp()` and `ungrab()`: `while self._display.pending_events(): self._display.next_event()`. Test with the FakeDisplay: after N synthetic events, the queue is empty after a warp.

## [important] A failed grab kills the host
`AlreadyGrabbed` is routine on X11 (any open menu/drag). `RuntimeError` from `linux.py:59/71` escapes `session.on_move` (`session.py:100`) on the listener thread after `remote=True`, `_on_remote_change(True)` and the park warp already happened; pynput stops the listener, the watchdog fires `on_capture_lost`, and listeners are never restarted.
**Fix at `session.py:100`:** `try/except RuntimeError` → log (type only), `self.remote = False; self._on_remote_change(False); return` (and ungrab whatever was grabbed). Test: a capture whose `start_remote` raises leaves the session local with listeners running.

## [important] Wayland refusal never renders
`app.py:355` returns no `items`; `ui/web/app.js:279` does `for (const item of perms.items)` → TypeError; the panel unhides empty under the hard-coded "macOS permissions" label (`index.html:132`).
**Fix:** return `items: [{"key": "session", "label": "X11 session", "why": "<the Ubuntu on Xorg message>", "granted": False}]` for wayland and no-display cases, and generalise the panel label (e.g. "Permissions"). Test the dict shape.

## [important] Wire spelling changed for Windows mouse buttons
`handle_click` now sends `button_to_wire(name)`; on win32 that maps `x1→button8` (`inject.py:34-35`), so a new Windows host sends `button8` where it sent `x1`, and a previous-release Windows client drops it.
**Fix:** wire canonical is `x1`/`x2`; `button_to_wire` is the identity on win32/darwin and maps `button8→x1`, `button9→x2` on linux; `button_from_wire` maps `x1→button8`, `x2→button9` on linux and is tolerant of `button8/9` inbound on win32 (→ `x1/x2`). Fix the parametrize rows that encode the wrong contract (`("button8","win32","x1")`).

## [important] Warp queue never coalesces
`capture.py:234` `queue.Queue()` — every entry is the same anchor and each warp does a `sync()` round-trip; when the worker lags, events arriving before the warp lands carry cumulative offsets and `_moved` double-counts.
**Fix:** `Queue(maxsize=1)`, `put_nowait` swallowing `queue.Full`, `flush()` instead of `sync()` in `warp()` (drain per the critical fix). Test that 100 queued warps coalesce to at most one pending.

## Gate-1 corrections
1. Restore a return annotation on `open_permissions` (`-> Union[bool, str]`) and use `Optional[Path]` for the three config signatures (`load`/`save`/`load_or_create`).
2. Add a test that `start_remote` grabs and `stop_remote`/`stop()` ungrab, using the existing `FakeDisplay`.
3. Expand the `linux.py` module docstring: X11 supported (including XWayland for X clients); Wayland-native clients invisible to XRecord/XTest, so Wayland sessions are refused; WSLg counts as X11.

## [minor] (fix all; small)
- `stop()` ungrabs (which syncs) before stopping listeners (`capture.py:230`); a dead X connection raises and skips listener shutdown — wrap in try/except.
- `LEGACY_PATH` (`config.py:18`) unused; use it in `load_or_create` instead of recomputing.
- Create the XDG config dir with mode 0700.
- Pin the WSLg detection with a test: `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, no `XDG_SESSION_TYPE` → `x11`.
- Test the pointer-grab failure branch (`linux.py:58`).

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
Per finding: what changed (file:line) and the check tail (2 lines). Line counts before/after per module.
