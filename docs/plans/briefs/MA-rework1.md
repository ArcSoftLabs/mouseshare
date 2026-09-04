# Task M-A — rework round 1 (review findings to fix)

Repository: current working directory. Your M-A changes are in the uncommitted working tree. An independent reviewer FAILED both gates. Fix everything below; nothing else. Do NOT commit. The reference for what must be restored is `git show HEAD:mouseshare/app.py`.

## Out-of-scope destruction to reverse (gate 1, item 3)
`mouseshare/app.py` went from 1072 to 660 lines: all 68 comment lines and every docstring were deleted, type annotations were stripped from every method, the `_inject`/`_injection_cost` debug instrumentation was removed, and all 12 `with self._lock` blocks were removed. **Restore** the comments, docstrings, type annotations and the injection instrumentation for every piece of code that survives (adapting wording to the per-peer structure where needed); new code gets the same standard. The brief said "no unrelated refactors beyond what the restructuring needs" — stripping documentation and locks is not restructuring.

## [critical] No locking at all
`self._lock` is created at `app.py:60` and never acquired. `_peers`/`_handshakes` are mutated from reader threads, heartbeat threads (`app.py:583-590`), `_tick_code` threads and the js_api thread. `_promote` (`app.py:499-506`) is check-then-set, so two racing handshakes for one id can both pass line 500.
**Fix:** reintroduce the lock discipline HEAD had: every read-modify-write of `_peers`, `_handshakes`, a peer's `phase`/`role`/`secret`, and the state publish that depends on them happens under `self._lock` (it is an `RLock`); `stop_inbound()`/thread joins/socket closes happen **outside** the lock exactly as HEAD's `_do_teardown` did after the P2 fix. `_promote` decides the duplicate winner under the lock.

## [critical] Duplicate refusal tears down the winner
`_teardown_peer` (`app.py:649`) does `self._peers.pop(peer.device_id, None)` unconditionally. A loser refused in `_promote` (`:501`) → `_refuse` → `_teardown_peer(loser)` pops the **winner** (same id) → `:655` `_release_input()` stops HostSession and capture. Same via `_on_auth` (`:454`).
**Fix:** `if self._peers.get(peer.device_id) is peer: del self._peers[peer.device_id]`, and only then consider "last peer left". Test: with client X connected and the cursor on X, an attacker (or a stale reconnect) presenting X's id is refused with `duplicate` and X's session, capture and `remote` state are untouched.

## [important] P1 heartbeat gate regressed
HEAD re-checked `stop.is_set() or self._heartbeat_stop is not stop` under the lock before tearing down; the new loop (`:586-587`) calls `_teardown_peer` directly, and `:654` resets `tearing_down=False`, so a stale thread can re-run teardown on a dead `_Peer` (and through the pop bug remove a reconnected peer).
**Fix:** before teardown, under the lock: `if stop.is_set() or peer.heartbeat_stop is not stop or self._peers.get(peer.device_id) is not peer: return`; never reset `tearing_down`. Restore the deterministic gate test's direct assertion (`assert calls == []`) that was weakened in `test_stopped_heartbeat_cannot_tear_down_a_later_pairing` (`test_app.py:549-550`), and restore the `caps == {"heartbeat"}` assertion in `test_the_right_code_pairs_both_machines` against `_Peer.caps`.

## [important] Role detection counts strangers
`_role()` (`:148-151`) counts any in-progress inbound handshake (role "client" is set at `:329` before identity is known) as "we are a client", so a stranger's `pair_request` makes `connect` fail with "Disconnect from host first". **Fix:** count only `phase == "session"` peers. Test it.

## Gate-1 corrections
1. Add the required `tests/test_security.py` inbound-duplicate test: an attacker's `pair_request` carrying a connected client's `device_id` → `pair_err{reason:"duplicate"}`, and that client's session is untouched (this is the inbound `_begin_target_pairing` path at `:390`, not the outbound `_on_challenge` path the multi test covers).
2. The `MAX_CLIENTS` test (`tests/test_multi.py:96-105`) stubs 8 `_Peer`s with a fake link. Make it open **9 real loopback connections** (8 accepted, the 9th refused with `full`). Keep it fast (shrink timeouts; reuse the fixtures).
3. Flaky test: `test_stopped_heartbeat_cannot_tear_down_a_later_pairing` failed once in a full run (target never emptied `_peers`) — the root cause is the pop/teardown race above; after fixing it, run the full suite 3 times and report all three tails.
4. Version pin: HEAD's `network.py:161-164` already pinned per link; state in the test docstring what the new test discriminates (the app-level downgrade of `_negotiated_version`), and make sure it fails if the app-level pin is removed.

## [minor] (fix if cheap)
- `_teardown_peer` never rebuilds `self._host.layout`; call the same rebuild `_become_host` uses when a peer leaves.
- `disconnect(device_id)` for an id still in `_handshakes` is a no-op (`:192-193`); refuse/close that handshake too.
- `full` fires only after the ninth target already showed its pairing screen (`:421`); refuse before sending the challenge if you can.

## Check command (must exit 0; run the full suite three times)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
For each finding: what changed (file:line). Then: line count of `mouseshare/app.py` before/after this rework and the count of `with self._lock` blocks, and the three check tails.
