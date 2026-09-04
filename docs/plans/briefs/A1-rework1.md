# Task A1 — rework round 1 (review findings to fix)

Repository: current working directory. Your A1 changes are in the uncommitted working tree. An independent reviewer passed gate 1 and returned these gate-2 findings. Fix all of them; nothing else. Do NOT commit.

## [critical] Escape modifier is swallowed while remote, breaking Ctrl/Cmd shortcuts on the peer
`capture.py:305-311` — every press/release of the escape modifier is swallowed while remote (`return` before `_on_key`). With the default `ctrl` (or `cmd` on macOS) the peer never receives the modifier: Ctrl+C/Cmd+C on the remote becomes plain `c`.
**Fix:** forward the modifier normally (`self._on_key(wire[0], wire[1], pressed)`) unless the detector reports the gesture completed; on the second tap's release only, skip forwarding and let the existing `leave` release the held key on the peer. Add tests: (1) ctrl-down, `c`, ctrl-up while remote reaches `on_key` with all three events in order; (2) a single ctrl tap while remote is forwarded (press and release) and does not escape; (3) the double tap escapes and the second release is not forwarded.

## [important] UI state delivery runs synchronously inside the Windows hook callback
`session.py:87,237` → `app.py:833-836` → `StateOwner.set` → `window.evaluate_js` runs synchronously inside the Windows low-level mouse/keyboard hook callback (`on_move` crossing and `on_escape` both happen there). pywebview's `evaluate_js` blocks on the WebView2 UI thread; a slow hook is silently unhooked by Windows — the exact failure this task exists to recover from.
**Fix:** `_host_remote_changed` (and any other state publish reachable from a capture callback) must hand off to another thread rather than delivering inline — e.g. a single daemon "state delivery" thread with a queue in `StateOwner`, or `threading.Thread(target=..., daemon=True).start()` for that publish. Preserve revision ordering (the existing `_last_delivered` guard). Add a test asserting the delivery callback is not invoked on the calling thread for that path.

## [important] macOS `_consume_current` flag is shared between the keyboard and mouse intercepts
`capture.py:355-357` — `_consume_current` is consumed by whichever intercept runs next. After `_on_escape` the injector's warp (`CGEventPost`) reaches the mouse tap on the mouse thread and can eat the flag, dropping our own warp and letting the Cmd key-up through to the OS.
**Fix:** scope the flag to the keyboard listener: give the keyboard listener its own intercept (`lambda t, e: None if consume else self._darwin_intercept(t, e)` or a dedicated method) so mouse events never observe it. Add a unit test with the fake listeners showing a mouse intercept call between the keyboard callback and the keyboard intercept does not clear the flag.

## [minor] Watchdog callback unguarded
`capture.py:226-230` — `_on_capture_lost()` is not wrapped; an exception (e.g. injector already `None` on a late race) kills the watchdog with a traceback. Wrap in `try/except Exception: log.exception("capture-lost handler failed")`.

## [minor] Repeated `start()` leaks a watchdog
`capture.py:208` — a second `start()` without `stop()` leaves the previous watchdog thread alive on the same cleared event. Guard (`if self._watchdog and self._watchdog.is_alive(): return` or a fresh `Event` per start).

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
List each finding with what you changed (file:line) and the check-command tail (2 lines).
