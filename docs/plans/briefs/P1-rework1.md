# Task P1 — rework round 1 (review findings to fix)

Repository: current working directory. Your P1 changes are in the uncommitted working tree (`git diff HEAD`). An independent reviewer passed gate 1 and returned these gate-2 findings. Fix the [important] ones and the first [minor]; add the second [minor] as a note in the plan doc. Do not change anything else. Do NOT commit.

## [important] Heartbeat start/stop race can leave an orphan thread that repeatedly tears down future sessions
`_start_heartbeat` (`app.py:883`) is called outside `self._lock` from both sites (`app.py:554`, `app.py:799`) and does not check `_phase == "session"`. If `_do_teardown` runs between the caps check and the thread start, teardown clears `_heartbeat_stop` to `None` (`app.py:949-951`) and the new thread is never stoppable. It then calls `_teardown("heartbeat")` every 2 s whenever idle, which runs `self._server.disconnect(reason)` and `_active = None` (`app.py:967-982`) — hanging up any subsequent pairing. Same failure in miniature at `app.py:898-900`: the loop does not confirm `stop` is still current before tearing down, so a thread that woke just before teardown can fire a second `_teardown` on an already-idle app.
**Fix:** take `self._lock` in `_start_heartbeat`, return unless `_phase == "session"`; in the loop, before `_teardown`, do `with self._lock: if stop.is_set() or self._heartbeat_stop is not stop: return`. Add a test that starts a heartbeat, tears down, and asserts no further `_teardown` call happens (e.g. count calls via monkeypatch over 3× the shrunk interval) and that a fresh pairing afterwards is not disconnected.

## [important] Timeout test does not exercise the release path the brief specified
`test_app.py:314-331` stubs `_host` with a `FakeHost`; the actual `HostSession.on_disconnect` → capture `stop_remote`/release chain is untested.
**Fix:** build the host through the existing `pair`/loopback fixtures with shrunk constants, use a real link, let the peer go silent (stop its heartbeat / pause its reader), and assert on the fixture's fake capture (`stop_remote` called / `suppressing` false) and `RecordingInjector`/held keys released. Remove the `FakeHost` stub test or keep it as a supplement.

## [minor] `ChunkAssembler` bounds bytes but not chunk count
`n` may be any positive int and zero-length `data` parts never exceed the cap, so `_parts` grows per message (`protocol.py:141-172`). **Fix:** reject `total > max(1, byte_cap)` with `ProtocolError`; add a test.

## [minor] Real-v2 interop caveat (doc note only)
The first outbound message (`pair_request`) is encoded with `VERSION=3` (`network.py:156`, `peer_version` still `None`); a peer running the old exact-match v2 decoder rejects it. Compat holds only for a v2 peer that tolerates `v:3` inbound. Add two sentences to the plan doc's P1 progress line stating this; no code change.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
List each finding with what you changed (file:line) and the check-command tail (2 lines).
