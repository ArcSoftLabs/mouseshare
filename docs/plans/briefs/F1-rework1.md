# Task F1 — rework round 1 (review findings to fix)

Repository: current working directory. Your F1 changes are in the uncommitted working tree (`mouseshare/transfer.py`, `tests/test_transfer.py` new). An independent reviewer FAILED gate 1 (eleven required tests missing) and returned gate-2 findings. Fix everything below; nothing else. Do NOT commit. Preservation rules apply (edit incrementally; keep comments/docstrings/annotations/locks; never weaken a test).

## Gate 1 — missing tests (all required; use two loopback Apps like `test_two_apps_transfer_multiple_empty_and_nonempty_files`)
1. 1 MiB file streamed with a `tracemalloc` peak assertion well under the file size (e.g. < 256 KiB attributable growth) on both sender and receiver.
2. Integrity mismatch (corrupt a chunk in flight via a monkeypatched send) → `.part` removed, `xfer_error{reason:"integrity"}`, state `failed`, bare filename only in the error.
3. Cancel by sender mid-stream → receiver's `.part` removed, both states `cancelled`.
4. Cancel by receiver mid-stream → sender stops sending (no further `xfer_chunk` after the cancel is seen), `.part` removed.
5. Disconnect mid-stream (kill the link) → `.part` removed, state `failed`, no thread left waiting (sender thread exits within 1 s).
6. Free-space precheck: monkeypatch `shutil.disk_usage` → `xfer_reject{reason:"space"}`, nothing written.
7. Permission error: read-only destination dir (skip on Windows if `chmod` is ineffective, using `os.name` guard) → `xfer_error`, nothing left behind.
8. Setting off → the offer is answered with `xfer_reject{reason:"disabled"}` (assert the reject, not just "no disk").
9. Offer from a non-session peer (use the `attacker` fixture pattern from `tests/test_security.py`) is ignored/refused and nothing is written.
10. `pos` traffic keeps flowing during a transfer: interleave 200 `pos` with a multi-chunk transfer and assert every `pos` arrives in order and the last one within 1 s.
11. Never-logged: `caplog` at DEBUG during a full transfer and an integrity failure; assert file content bytes and the destination path do not appear.
12. Client→host direction; host→client while a second client is connected (in `tests/test_multi.py`), asserting the second client receives nothing.
13. Name table: add trailing-dot and trailing-space cases (nt), and `:` / `<>"|?*` (see minor below).

## [critical] Unbounded bytes past the declared size
`transfer.py:219-226` — cumulative bytes are never bounded by `size`, and `n` is neither checked against `ceil(size/32K)` nor required to be constant, so a session peer can offer `size: 0` (passes the free-space check) and stream until `xfer_done`. **Fix:** track `written`; reject the transfer (`xfer_error{reason:"overrun"}`, `.part` removed) when `written + len(data) > size`; reject `n != max(1, ceil(size / TRANSFER_CHUNK))` and any change of `n` mid-file. Test both.

## [important] Rename can overwrite on POSIX
`transfer.py:245-247` — `exists()` then `os.rename` is a TOCTOU; POSIX rename overwrites. **Fix:** `os.link(part, target)` then `os.unlink(part)` (falls back to `os.rename` only when `os.link` raises `OSError` with `errno` in `{EPERM, EXDEV, ENOTSUP}` AND a fresh `O_EXCL` create of `target` succeeded just before). Test: pre-create `target` between accept and done → transfer fails, original file untouched.

## [important] Multi-file offers race on later names and cleanup unlinks the winner's part
`transfer.py:189,255,271-275` — only `parts[0]` is opened exclusively at accept; files 2..N are opened lazily; `_cleanup_receive` unlinks every path in `parts` including another transfer's live `.part`. **Fix:** open every `.part` with `xb` at accept time (reserve all names up front); unlink only parts this transfer created. Test: two offers sharing a later filename.

## [important] Toggling the setting off mid-transfer strands both sides
`app.py:606-610` drops every non-offer `xfer_*` when `share_files` is off, so a receiver's `.part` stays until disconnect and the sender blocks forever in `acked.wait_for` (`transfer.py:294`, no timeout; likewise `accepted.wait()` at `:278`). **Fix:** gate only `xfer_offer` on the setting, and have `set_share_files(False)` cancel in-flight transfers; add timeouts (30 s) to both waits that fail the transfer cleanly. Test.

## [minor]
- `validate_name`: also reject `:` everywhere (NTFS alternate data streams) and `<>"|?*` on Windows.
- `transfer.py:231`: catch `TypeError`/`binascii.Error` from `b64decode` of a non-string → `xfer_error{reason:"malformed"}`, link stays up.
- Guard `_receive_done`/`_receive_error` against ids belonging to `_sends`; take `_lock` in `discard_peer`; reset `ack_index` under `send.acked`; prune finished entries from `_sends`/`_views` (keep the last 20 for the UI).
- Route success/failure through the existing `#notice`/`#banner` in `app.js` as the brief asked (keep the transfer list too).

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
Per finding: what changed (file:line); the list of test names added; line counts before/after; the check tail (2 lines).
