Implemented the requested rework without committing.

- Restored documentation, comments, method annotations, injection instrumentation, and lock discipline in [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:1).
- Session-only role detection: [app.py:175](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:175).
- Handshake-aware `disconnect(device_id)`: [app.py:224](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:224).
- Early inbound duplicate/capacity checks: [app.py:445](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:445).
- Atomic duplicate winner selection: [app.py:581](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:581).
- Restored stale-heartbeat gate: [app.py:682](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:682).
- Restored `_inject` and `_injection_cost`: [app.py:736](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:736).
- Identity-safe teardown, permanent teardown guard, and out-of-lock reader/socket shutdown: [app.py:775](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:775).
- Rebuilds host layout after peer departure: [app.py:816](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:816).
- Restored heartbeat and capability assertions: [test_app.py:533](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_app.py:533).
- Added stranger-role and nine-real-loopback tests: [test_multi.py:82](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_multi.py:82), [test_multi.py:114](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_multi.py:114).
- Added app-level version-pin discriminator and inbound duplicate attack test: [test_security.py:144](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_security.py:144), [test_security.py:173](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_security.py:173).

Metrics:

- `app.py` before: 660 lines
- `app.py` after: 828 lines
- `with self._lock` blocks: 30

Final three check tails:

```text
[ 96%]
......... [100%]
225 passed in 16.07s
```

```text
[ 96%]
......... [100%]
225 passed in 16.11s
```

```text
[ 96%]
......... [100%]
225 passed in 16.13s
```

`ruff check mouseshare tests` passed on all three runs. `git diff --check` also passed. No commit was created.