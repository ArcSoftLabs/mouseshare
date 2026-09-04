Implemented all requested findings without committing.

- Windows x64: added explicit ctypes signatures and guaranteed clipboard closure if `GlobalFree` raises; ownership transfers correctly after `SetClipboardData` ([clipboard.py:100](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/clipboard.py:100), [test_clipboard.py:231](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_clipboard.py:231)).
- Inline size: inline messages now use encoded protocol size; oversized JSON is chunked. Added a two-App loopback test for `"世" * 10922` with connection assertions ([clipboard.py:390](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/clipboard.py:390), [test_multi.py:104](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_multi.py:104)).
- X11 race: the serve thread now forwards `SelectionNotify` events through a queue to readers ([clipboard.py:275](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/clipboard.py:275), [test_clipboard.py:375](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_clipboard.py:375)).
- macOS: moved helper implementation out of `__init__`, added worker-thread marshalling coverage, and made pending calls terminate with `RuntimeError` after shutdown ([clipboard.py:34](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/clipboard.py:34), [test_clipboard.py:326](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_clipboard.py:326)).
- Echo race: `_last_hash` is assigned before backend writes ([clipboard.py:442](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/mouseshare/clipboard.py:442)).
- Logging: malformed payload and backend-exception paths now verify clipboard contents never appear in logs or raised representations ([test_clipboard.py:430](/mnt/c/Users/A508639/Documents/Claude%20Projects/mouseshare/tests/test_clipboard.py:430)).

Line counts:

- `mouseshare/clipboard.py`: 406 → 457
- `tests/test_clipboard.py`: 280 → 456
- `tests/test_multi.py`: 258 → 292

Check tail:

```text
...................................................                      [100%]
267 passed in 18.87s
```