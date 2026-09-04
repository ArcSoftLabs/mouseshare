# Task C — rework round 1 (review findings to fix)

Repository: current working directory. Your clipboard changes are in the uncommitted working tree (`mouseshare/clipboard.py`, `tests/test_clipboard.py` are new, untracked). An independent reviewer FAILED gate 1 on two items and returned these gate-2 findings. Fix all of them; nothing else. Do NOT commit. Preservation rules apply (no rewrites; keep comments/docstrings/annotations/locks; never weaken a test).

## [critical] Windows backend cannot lock/free memory on x64
`clipboard.py:99,119,125,126,131`. Verified on the real PC: `GlobalAlloc` returns a handle > 2³²; `GlobalLock(h)` without `argtypes` raises `ctypes.ArgumentError: OverflowError: int too long to convert`, so `read_text`/`write_text` fail every call. Worse, in `write_text` the `finally` (129-132) calls `GlobalFree(handle)` first, which itself raises, so `CloseClipboard()` never runs and the process leaves the system clipboard open.
**Fix:** in `__init__` set `GlobalLock/GlobalUnlock/GlobalFree.argtypes = [c_void_p]`, `SetClipboardData.argtypes = [c_uint, c_void_p]`, `GlobalAlloc.argtypes = [c_uint, c_size_t]`, `OpenClipboard.argtypes = [c_void_p]`, `GetClipboardSequenceNumber.restype = c_uint32`, `IsClipboardFormatAvailable.argtypes = [c_uint]`; wrap `GlobalFree` in its own try so `CloseClipboard` always runs (and after a successful `SetClipboardData` the system owns the handle — do NOT free it). Add a fake-kernel32 write test whose `GlobalLock` rejects ints > 2³¹ unless `argtypes` was set, and a test that `CloseClipboard` is called even when `GlobalFree` raises.

## [critical] Inline `clip` can exceed `MAX_LINE` and tear down the session
`clipboard.py:342-345`. `protocol.encode` uses `json.dumps` with `ensure_ascii=True`; 32766 UTF-8 bytes of CJK become a 65596-byte line > 65536. The receiver raises `ProtocolError` → "Connection dropped". Copying ~11k non-ASCII characters kills the link.
**Fix:** build the inline message and chunk when `len(protocol.encode(msg)) > protocol.MAX_LINE - 256` (decide by encoded size, not UTF-8 length); test with `"世" * 10922` round-tripping through two loopback Apps without a disconnect.

## [important] X11 read races the serve thread
`clipboard.py:229-231` vs `249-251`. Both threads call `next_event()` on one `Display`; `_serve` discards every non-`SelectionRequest` event, including the `SelectionNotify` that `read_text` waits for, so foreign-owner reads mostly time out. **Fix:** perform reads on the serve thread via a request queue (one thread owns the Display), or have `_serve` stash `SelectionNotify` events for `read_text`. Test with a fake display that delivers `SelectionNotify` through the serve loop.

## [important] macOS marshalling path is untested and shutdown-hostile
`clipboard.py:155-159`. The test forces `isMainThread=True`, so line 158 never executes; the fake `NSObject` lacks `pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_`. Since `app.stop()` runs after `webview.start()` returns (no run loop), an in-flight call blocks forever (2 s join stall per peer plus a stuck daemon thread). **Fix:** test with `isMainThread=False` and a fake recording the selector name and arguments; make the marshalling bail (raise `RuntimeError`) once `stop()` has been called; verify PyObjC tolerates the helper class being defined once at module import rather than inside `__init__` (move the class body out of `__init__`, line 142).

## [minor]
- `receive()` echo race (`clipboard.py:390-392`): set `_last_hash = digest` *before* `write_text`, not after.
- Never-logged test is happy-path only (`test_clipboard.py:267-280`): feed a malformed message with a distinctive `text` and assert its absence from `caplog.text` and from any raised repr; also cover the backend-exception path.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
Per finding: what changed (file:line) and the check tail (2 lines). Line counts before/after per module.
