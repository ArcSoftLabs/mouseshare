# Task M-B — rework round 1 (review findings to fix)

Repository: current working directory. Your M-B changes are in the uncommitted working tree. An independent reviewer returned these findings. Fix all of them; nothing else. Do NOT commit. Do NOT touch `tools/dragdrop/` or `docs/file-transfer.md` (another task owns them).

## [critical] Default offsets overlap
`app.py:796-805` `_default_offset` reads `cfg.offsets.get(id, (0, 0))`, so a peer with no saved offset counts as x=0. Host + two peers that were never dragged → offsets `{a:(1920,0), b:(1920,0)}`, `can_place(b)` is False, the right edge routes to `a` only and `b` is unreachable. Offsets are only saved by `set_offset`, so this is the *default* configuration. The new test hides it by pre-saving the first peer's offset.
**Fix:** in `_build_layout`, assign defaults sequentially (each defaulted peer feeds the next `_default_offset`, i.e. place to the right of the bounding box of everything placed so far), and iterate the same `peer_monitors` map so offline peers with saved offsets count. Test: host + two undragged peers → non-overlapping offsets and both reachable from the host's right edge in order.

## [important] Placeholder block crashes `set_offset`
The placeholder block for a paired peer with no known monitors (`app.js:141-144`) is draggable; `set_offset("ghost", …)` → `snap_device` → `_extent` → `ValueError: min() arg is an empty sequence` (`app.py:296`, `layout.py:151-155`; reproduced). **Fix:** `snap_device` returns early when `_plane_rects(mobile)` is empty, and offline peers without geometry are rendered non-draggable. Test both.

## [important] Missing 4-device cases (gate 1 items 2 and 3)
- Non-adjacent hop: while on `right`, `on_delta` toward `below`'s plane area must leave `peer_id` unchanged and the last message must be `pos`, not `leave` (add to `tests/test_multi.py`).
- Peer death while remote: the current test calls `three.stop()`, a clean close; the heartbeat never fires. Shrink `HEARTBEAT_INTERVAL`/`HEARTBEAT_TIMEOUT` via monkeypatch and kill the link without a clean close (e.g. stop the peer's heartbeat/outbox and silence its `_send`, as `test_heartbeat_timeout_tears_down_and_releases_host` does), then assert release + warp home within the shrunk timeout.

## [minor] (fix all; they are small)
- `session.py:73-76` docstring says "probe horizontally first" but `_probe` never falls back; a corner with only a vertical neighbour stays local. Add the vertical fallback (horizontal first, then vertical) and a test.
- `session.py:166` — a button held during a hop is released on the old peer and not replayed on the new one; add one docstring line saying so.
- `style.css:259` `.block.live` ring changes two-device rendering; keep it but note the tradeoff in the plan's progress line.
- `app.py:489` — `update_layout(self._build_layout())` can receive `None` (no peer has monitors) → `AttributeError` on the next `on_move`; guard (keep the previous layout or an empty one that maps nothing).
- `_known_monitors` never pruned when a peer is unpaired (`forget`); prune it.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
Per finding: what changed (file:line) and the check tail (2 lines). Line counts before/after per module.
