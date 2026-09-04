# Task F1 — rework round 2 (final; delta-review findings to fix)

Repository: current working directory. Your F1 rework-1 changes are in the uncommitted working tree. The delta reviewer confirmed the critical overrun fix, the rename fix, the setting-toggle fix and all minors, but FAILED the gate: one required test fails under load, two required tests assert less than required, and the cancel path has two new bugs. Fix exactly the items below; nothing else. Do NOT commit. Preservation rules apply (edit incrementally; keep comments/docstrings/annotations/locks; never weaken a test; do not restructure `transfer.py`).

## 1. [important, NEW] Sender reports `done` after the receiver cancels
`transfer.py:355-364` — when `acked.wait_for` returns because `send.cancelled` was set, the loop falls through, sends `xfer_done` and on the last file calls `_set_status("done")`. Reproduced: receiver cancels instead of acking the last chunk → host status `done`, client status `failed` (the stray `xfer_done` reaches `_receive_done` with no receive, which calls `_receive_error("protocol")` and flips the receiver's `cancelled` to `failed`). **Fix:** after every wait (accept and ack), check `send.cancelled` and `send.failed`/link-down before sending anything further; never send `xfer_done` for a cancelled send. **Test:** receiver cancels on the last chunk (monkeypatch the receiver's chunk handler to cancel instead of acking) → sender `cancelled`, receiver `cancelled`, no `xfer_done` on the wire, no `.part`, no file.

## 2. [important, NEW] Receiver cancel racing a chunk drops the whole link
`transfer.py:237-258` — `_receives.get` and the file write happen outside `_lock`; a UI-thread `cancel()` / `set_share_files(False)` between them closes the handle → `ValueError: write to closed file`, not caught by the `except OSError` at `:265` → `app.py:578-584` tears the peer down as "bad message". **Fix:** hold `_lock` from the lookup through the write (and through the done/verify path in `_receive_done`), keeping every `send` call outside the lock as they already are; additionally catch `ValueError` on the write and treat it as the transfer having been cancelled (no error frame, no teardown). **Test:** inject a `cancel()` inside a monkeypatched `base64.b64decode` (or equivalent hook) during a multi-chunk receive → receiver `cancelled`, `.part` removed, link still up (a subsequent `pos` still arrives).

## 3. Required test item 10 fails under load (`test_position_traffic_remains_ordered_during_multichunk_transfer`)
Fails in the full suite (`len(seen) == 200` within 1 s), passes isolated. Root cause is design, not a bug: `outbox.py:62-67` coalesces consecutive `pos` frames. **Fix the assertion, not the timeout:** assert that the received `x` values are strictly increasing (order preserved, no reordering), that the *last* `pos` (x=199 or a sentinel) arrives within 1 s, and that at least one `pos` arrives while a transfer is in flight. Do not assert every one of the 200 arrives.

## 4. Required test item 1 asserts less than required (`test_one_mib_transfer_streams_with_bounded_sender_and_receiver_memory`)
`tests/test_transfer.py:155-169` measures residual growth *after* completion, so a receiver that buffers the whole 1 MiB until done still passes (verified with a probe). **Fix:** use `tracemalloc.reset_peak()` right before the transfer starts and assert `tracemalloc.get_traced_memory()[1]` (the peak) minus the baseline is `< 256 KiB` on both the sender App's process side and the receiver's; since both Apps are in one process, run the sender and receiver measurements as two sub-cases (or one combined peak bound < 512 KiB total) — the bound must be violated by the whole-file-buffered probe. Keep the file at 1 MiB.

## 5. Multi-file test never exercises cleanup (`test_two_offers_sharing_later_name_do_not_unlink_winners_part`)
`tests/test_transfer.py:405-419` — the loser is never cancelled or failed, so the fix (unlink only owned parts) is not tested. **Fix:** after both offers are in flight, cancel the loser (`client._transfers.cancel(<loser id>)` or via the public cancel path) and assert the winner's `shared.part` still exists, the winner is still in `_receives`, and the winner then completes with the correct bytes.

## 6. [minor] Dead code / Windows note
`transfer.py:246` — the `chunk_count` change check is unreachable because `n != expected` fires first. Leave the fix as is but make the change-check the primary condition on files after the first chunk (so both messages are reachable), or delete the unreachable branch and its test expectation if it cannot be reached — do NOT delete the test; adjust it to assert the observable `failed`. `transfer.py:305-308` — on Windows `os.rename` onto the O_EXCL-created target raises `FileExistsError`; document in a one-line comment that the fallback is POSIX-only and that Windows relies on `os.link` (NTFS). No behaviour change.

## Check command (must exit 0, run it three times in a row to catch flakiness)
```
ruff check mouseshare tests && python3 -m pytest -q tests && python3 -m pytest -q tests && python3 -m pytest -q tests
```

## Report
Per item: what changed (file:line), test names added/changed, line counts before/after for `transfer.py` and `test_transfer.py`, and the check tail (2 lines) of the third run.
