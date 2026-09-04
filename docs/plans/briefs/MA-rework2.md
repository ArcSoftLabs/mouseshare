# Task M-A — rework round 2 (final; exact remaining items)

Repository: current working directory. Your M-A changes are in the uncommitted working tree. Fix exactly the three items below; nothing else. Do NOT commit. Reference for restoration: `git show HEAD:mouseshare/app.py` (line numbers below are HEAD's).

## 1. Restore documentation (the review measured docstrings 82→28 lines and comments 68→12)
Restore, with HEAD's wording adapted only where the per-peer structure requires it:
- module `POSITION_INTERVAL` rationale (HEAD 27-34)
- `__init__` field comments (HEAD 50-51, 70, 77, 95)
- `start` bound-port comment (HEAD 108-109)
- `open_permissions` docstring (HEAD 346)
- `_publish_peers` reachable/ordering rationale (HEAD 395-401)
- `_send` (HEAD 438) and `_refuse` (HEAD 456) docstrings
- `_on_message` security comments (HEAD 506-509, 525-527, 537-539)
- `_on_pair_err` retryable comment (HEAD 599-600)
- `_begin_target_pairing` docstring (HEAD 607)
- `_tick_code` re-check comment (HEAD 648-650)
- `_on_challenge` manual-connect comment (HEAD 667-668)
- `_on_pair_ok` address-preservation comment (HEAD 764-767) and a docstring
- `_become_host` outbox comment (HEAD 832)
- `_on_disconnect` refused-link comment (HEAD 1002)
- all `# -- section --` dividers
- `connect`, `submit_code`, `_on_challenge`, `permissions`, `_inject`, `_injection_cost`: replace the rewritten one-liners with HEAD's docstring text.
- New methods that need a docstring: `disconnect`, `_publish_peers_locked`, `_session_view`, `_publish_session`, `_on_inbound_message`, `_on_inbound_disconnect`, `_set_caps`, `_send_peer`, `_queue`, `_broadcast`.
Report the resulting comment-line and docstring-line counts against HEAD's 68 and 82.

## 2. Unlocked reads/writes in the challenge path
`_on_challenge` (current L500-512) iterates `self._peers.values()` and reads `_peers` with no lock, then sets `device_id`/`secret`/`phase` unlocked; a concurrent `del` from a heartbeat thread's teardown can raise in the reader. `_begin_target_pairing` (L458) also reads `_peers`/`_handshakes` outside the lock. Take `self._lock` around those reads and the peer-field writes (keep sends and closes outside it). Add a test that races a heartbeat-driven teardown against an incoming challenge and shows no exception in the reader (use a fake link, deterministic ordering via events).

## 3. Capacity gate location (minor)
The host-side `full` still fires in `_on_challenge` (L500) after the target has shown its code, and `_connect_to` (L186-205) has no `MAX_CLIENTS` gate; the target-side gate (L460-477) is unreachable behind the `busy` refusal. Move the host's check into `_connect_to` (refuse locally before dialling when `len(session-phase peers) >= MAX_CLIENTS`), keep a defensive check in `_on_challenge`, and adjust the nine-connection test so it asserts the ninth target never enters pairing.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
Per item: what changed (file:line). Then the comment/docstring counts and the check tail (2 lines).
