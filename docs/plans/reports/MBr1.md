Implemented all review findings without committing or touching `tools/dragdrop/` or `docs/file-transfer.md`.

Per finding:

- Default offsets: sequential placement now uses the same cached/live peer map, so offline geometry participates and unsaved peers no longer overlap ([app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:799), [test_app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_app.py:681)).
- Empty placeholder geometry: snapping safely returns early; geometryless offline blocks are marked non-draggable in the UI ([layout.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/layout.py:86), [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:830), [app.js](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/ui/web/app.js:172), [test_layout.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_layout.py:167)).
- Four-device coverage: added non-adjacent-hop assertions and changed active-peer death to a silent heartbeat timeout ([test_multi.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_multi.py:147)).
- Corner fallback: edge probing now tries horizontal first, then vertical ([session.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/session.py:72), [test_session.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_session.py:239)).
- Held-button behavior: documented release-without-replay during peer hops ([session.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/session.py:156)).
- Live-ring tradeoff: recorded in the M4 progress entry ([capability-expansion.md](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/docs/plans/2026-09-03-capability-expansion.md:327)).
- Empty layout updates: retain the previous host layout when rebuilding returns `None` ([app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:489)).
- Forgotten peers: cached monitor geometry is now pruned ([app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:283)).

Line counts, HEAD → working tree:

| Module | Before | After |
|---|---:|---:|
| `mouseshare/app.py` | 930 | 961 |
| `mouseshare/layout.py` | 178 | 199 |
| `mouseshare/session.py` | 295 | 330 |
| `mouseshare/ui/web/app.js` | 341 | 353 |
| `mouseshare/ui/web/style.css` | 304 | 306 |
| `tests/test_app.py` | 821 | 885 |
| `tests/test_layout.py` | 144 | 173 |
| `tests/test_multi.py` | 130 | 233 |
| `tests/test_session.py` | 319 | 431 |

Check tail:

```text
.............................                                            [100%]
245 passed in 17.20s
```

The diagnosing-bugs workflow guided the regression-first verification. `git diff --check` is also clean.