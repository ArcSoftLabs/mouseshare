# Task M-B — routing across N screens, layout editor for N devices, 4-device tests (plan tasks M3 + M4 + M5)

Repository: current working directory. Read `docs/plans/2026-09-03-capability-expansion.md` §2.1 and §4 "Block M" (M3–M5), then `mouseshare/session.py`, `mouseshare/layout.py`, `mouseshare/app.py` (`set_offset`, `_build_layout`, `_layout_view`, `_default_offset`, `_host_remote_changed`), `mouseshare/ui/web/app.js` + `index.html` (layout canvas), and tests `tests/test_session.py`, `tests/test_layout.py`, `tests/test_multi.py`, `tests/test_app.py`. Test-first. M-A (per-peer transport/state) is already in the tree.

## Requirements

### Routing (session.py)
1. `HostSession` routes by the `map_exit` result across all peers: an edge hit on the local screen that maps to peer X → `enter` to X (as today). While remote on peer X, the modelled position mapping to peer Y → send `leave` to X, then `enter` to Y at the mapped point (peer→peer hop; the host never returns local in between); mapping back to the local screen → `leave` to X and warp home (as today). `remote_peer` (which peer the cursor is on) is part of the published session state (`active_peer`).
2. All four edges: the existing point-in-rect probe already handles left/right/top/bottom; add explicit tests for top and bottom crossings and for a corner probe that must pick a deterministic target (define: horizontal neighbour wins over vertical; document in the docstring).
3. Determinism: when two peers' rects both contain the probe point (overlap should be prevented by `can_place`, but the host's layout can go stale when a peer's monitors change), pick by `(device_id, monitor id)` sort order as `map_exit` does today, and add a test pinning that.
4. Topology changes mid-session: `add_peer`/`remove_peer` (from M-A) update the layout live. Removing the peer the cursor is on → release and warp home (M-A's `on_peer_lost`). Removing another peer → nothing visible. A peer's `layout`/monitor update while the cursor is on it → re-clamp the modelled position into the new rects; if it no longer maps anywhere, warp home. Never leave `remote=True` with no valid peer.
5. Keys/clicks/scroll go to the active peer only.

### Layout model and editor (layout.py, app.py, app.js)
6. `snap_device` and `can_place` already take N devices; make `set_offset(device_id, x, y)` snap against **all** other devices and reject overlaps with any of them. `_default_offset` for a new peer: place it to the right of the current bounding box of all placed devices (so the third device does not overlap the second).
7. `_layout_view` emits one block per device (self + every paired peer, connected or not — connected ones highlighted), and `app.js` renders N draggable blocks with the existing drag/snap interaction. Keep the two-device visual behaviour identical when there are two.
8. Offsets remain host-side only (unchanged model); a client's editor edits stay cosmetic as today — but show a hint "Arrangement is set on the host" on a client.

### 4-device tests (tests/test_multi.py)
9. One host + three clients over loopback with deterministic monitors (from P2's fixture), arranged left / right / below the host: cross into each and back; hop host→right→(host)→below; direct hop right→below is *not* possible when they don't share an edge (must return via host) and *is* possible when they do (arrange two clients adjacent and prove the `leave`/`enter` pair goes to the right peers in order).
10. Cursor never trapped: while on client 3, kill client 3's App (simulate peer death) → host releases within the heartbeat timeout (shrunk constants) and the local cursor is warped home; while on client 1, kill client 2 → nothing changes for client 1.
11. Duplicate identity and reconnect mid-session (from M-A) do not disturb the active remote session on another peer.
12. Existing two-device tests all keep passing unchanged in meaning.

### Constraints
- Headless importability; no new deps; ruff clean; no unrelated refactors.
- Do NOT commit; tick M3–M5 in the plan's Progress list. Suite must pass under Windows Python too (no real-geometry dependence in tests).

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
End with: files changed, the hop rule and corner rule in 3 lines each, how stale layouts are handled, check tail (2 lines), anything left out with reason.

## Preservation rules (non-negotiable; a violation fails review outright)
- Edit incrementally; never rewrite a module wholesale. Every comment, docstring and type annotation that exists today survives unless the code it describes is removed; new code carries the same standard.
- Every `with self._lock` (and any other lock discipline) stays exactly as it is unless the brief names a specific change. Joins, socket closes and `stop_inbound()` stay outside the app lock.
- Never delete, skip, or weaken an existing test or assertion; if a test must change because an interface changed, keep its assertion's meaning and say so in the report.
- Report the before/after line count of every module you touch; a net shrink on a feature task must be explained line by line.
