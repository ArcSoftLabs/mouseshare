Implemented exactly the three requested items without committing.

1. Documentation restored and adapted for per-peer state in [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:26), including all requested comments, section dividers, restored docstrings, and new-method docstrings.

2. Challenge-path access is now locked in [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:500) and [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:562). Added the deterministic heartbeat-teardown race test in [test_app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_app.py:131).

3. The host capacity gate now runs before dialing in [app.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/app.py:198), with the defensive challenge check retained. The ninth-client test now verifies the target never starts pairing in [test_multi.py](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_multi.py:114).

Counts for `mouseshare/app.py`:

- Full-line comments: 56 versus HEAD’s 68
- Docstring lines: 64 versus HEAD’s 82

Check tail:

```text
.                                                               [100%]
226 passed in 16.32s
```