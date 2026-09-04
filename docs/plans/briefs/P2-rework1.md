# Task P2 — rework round 1 (review findings to fix)

Repository: current working directory. Your P2 changes are in the uncommitted working tree. An independent reviewer returned these findings. Fix all of them; nothing else. Do NOT commit.

## GATE 1 correction
1. `tests/test_app.py:183-202` `test_nothing_is_injected_after_input_has_been_released` passes on the old code too (old `_inbox.stop()` drained then released, so `calls[-1]` was still `release`), so it does not show that queued messages are dropped. Make it discriminate: block the worker (a handler gate before the first inject), queue a `key`, tear down, then assert the key is never injected.

## [critical] Teardown holds `self._lock` while joining the inbound worker that needs it
`app.py:985-993`: `_do_teardown` holds `self._lock` while `_client.close()`/`_server.disconnect()` run `_Link._stop_handler` → `worker.join(timeout=2.0)` (`network.py:106-113`). The worker's handler is `App._on_message`, whose first statement is `with self._lock:` (`app.py:489`). Any message dequeued after teardown takes the lock blocks the worker, so the join always times out; `release_all` (`:994`) and `capture.stop()` (`:998`) sit behind it. Reproduced by the reviewer: teardown takes 2.00 s. Aftermath: the un-stopped in-flight message proceeds once the lock is released; `_active` is `None` by then (`:1013`), so it claims `"in"` (`:490-491`), lands in the "unexpected message" branch and runs a second `_teardown`, overwriting the user-visible reason.
**Fix:** split `_Link.stop_inbound()` (set stop flag, clear queue, join) out of `close()`; in `_do_teardown` capture the link references under the lock, release the lock, call `stop_inbound()` on them *outside* the lock, then re-acquire for the rest. Keep `close()` idempotent. Self-join stays guarded. Add a test that teardown with a worker blocked on the app lock completes in well under 0.5 s and that no second `_teardown` reason overwrites the first.

## [important] Unrequested teardown reorder loses the final outbox flush
`app.py:989-1002`: sockets now close before `_outbox.stop()` (previously the outbox was flushed first). Anything queued in the outbox at teardown (e.g. a final `leave` the host just emitted) now fails at `MessageClient.send`'s closed check instead of being flushed.
**Fix:** restore the order: stop inbound → release / `capture.stop()` → `outbox.stop()` (flush) → close sockets. Add a test that a `leave` put just before teardown reaches the peer.

## [important] Missing `task_done()` leaks the reader thread
`network.py:81-82`: the early `return` after `get()` skips `task_done()`; `finish()`'s untimed `join()` (`:97`) then never returns and the reader thread leaks blocked.
**Fix:** call `task_done()` in that branch (and/or bound the `join`). Test: after close, the reader thread ends within 1 s.

## [minor] Flaky flood assertion
`tests/test_robustness.py:106-110`: drop-oldest can remove `pos x==199` if the queue is full when a later `key` arrives. Make the assertion robust (assert the `key` arrived and that the last injected `pos` is some x ≤ 199 and monotonic, or drain before the key).

## [minor] Heartbeat gate test asserts nothing after the first call
`tests/test_app.py:330-339`: also assert `_heartbeat_stop is None` and the thread set unchanged after the first (idle) call.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
List each finding with what you changed (file:line) and the check-command tail (2 lines). Also report the measured teardown time from the new test.
