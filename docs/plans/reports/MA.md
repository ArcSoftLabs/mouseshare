Implemented M1 + M2 using test-first development. No commits created.

Files changed:

- `mouseshare/app.py`
- `mouseshare/network.py`
- `mouseshare/session.py`
- `mouseshare/ui/web/app.js`
- `tests/test_app.py`
- `tests/test_network.py`
- `tests/test_security.py`
- `tests/test_session.py`
- `tests/test_multi.py`
- `docs/plans/2026-09-03-capability-expansion.md`

`_Peer` fields:

- Required: `device_id`, `name`, `link`, `phase`, `nonce`, `pending`, `monitors`, `caps`, `version`, `outbox`, `inbound`, `heartbeat_stop`, `last_received`
- Internal lifecycle fields: `role`, `secret`, `unauthenticated`, `tearing_down`

Refusals:

- `busy`: server transport for a second inbound socket; App when a host/client cannot accept an inbound role.
- `duplicate`: self identity, connected identity, conflicting handshake/auth, or duplicate promotion.
- `full`: host rejects the ninth client after receiving its challenge.
- Existing `unknown device`, `authentication failed`, `wrong code`, and `not pairing` refusals remain.

Per-peer teardown order:

1. Stop heartbeat.
2. Stop and drain that link’s inbound queue.
3. Notify `HostSession.on_peer_lost()` or release the client injector.
4. Flush and stop that peer’s outbox.
5. Close only that peer’s socket.
6. Remove it from `_peers`/`_handshakes`.
7. Release shared input resources only after the final peer leaves.
8. Republish session, layout, and connected-peer state.

The protocol version is pinned from the first frame. A later version change raises `ProtocolError`, closes the link, and cannot save a downgraded token. The UI now shows connection count and displays the remote escape hint and unauthenticated-v2 warning together.

M1 and M2 are checked in the plan. M-B routing/layout generalization was intentionally left out because this task retains single-active-peer cursor routing.

Check tail:

```text
.......                                                                  [100%]
223 passed in 15.29s
```

Windows Python also passed: `223 passed in 17.07s`.