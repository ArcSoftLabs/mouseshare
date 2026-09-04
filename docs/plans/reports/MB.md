Implemented M3–M5 test-first. Both Linux/WSL and Windows Python suites pass.

Files changed, with before → after line counts:

- `mouseshare/session.py`: 295 → 323
- `mouseshare/layout.py`: 178 → 193
- `mouseshare/app.py`: 930 → 948
- `mouseshare/ui/web/app.js`: 341 → 350
- `mouseshare/ui/web/index.html`: 142 → 143
- `mouseshare/ui/web/style.css`: 304 → 305
- `tests/test_session.py`: 319 → 418
- `tests/test_layout.py`: 144 → 164
- `tests/test_multi.py`: 130 → 217
- `tests/test_app.py`: 821 → 852
- `docs/plans/2026-09-03-capability-expansion.md`: 325 → 328; M3–M5 checked off

Hop rule:
Local → peer sends `enter` to the mapped peer.
Peer → peer sends `leave` to the old peer, then `enter` to the new peer.
Peer → host sends `leave`, releases suppression, and warps home.

Corner rule:
A corner can expose both horizontal and vertical neighbours.
The horizontal probe is evaluated first.
Overlapping targets remain deterministic by `(device_id, monitor id)` ordering.

Stale layouts replace the live geometry immediately. Active coordinates are re-clamped and corrected only if needed; removing or losing the active screen releases suppression and warps home. Losing another peer leaves the active session unchanged.

Disconnected paired devices remain represented in the editor, connected devices are highlighted, and clients see “Arrangement is set on the host.” Unknown saved geometry receives a cosmetic placeholder until the peer reconnects.

Check tail:

```text
........................                                                 [100%]
240 passed in 17.11s
```

Windows Python: `240 passed in 19.29s`. Nothing left out. The TDD skill guided the red/green slices and public-seam tests. No commits were created.