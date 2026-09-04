# Task M-A — multi-device transport and app state (plan tasks M1 + M2)

Repository: current working directory. Read `docs/plans/2026-09-03-capability-expansion.md` §2.1 (star topology, decided), §4 "Block M" (M1, M2), then `mouseshare/network.py`, `mouseshare/app.py` (all of it — this task restructures its peer state), `mouseshare/session.py` (`HostSession` constructor and `peer_id`), `mouseshare/config.py`, `mouseshare/state.py`, `mouseshare/ui/web/app.js` (how `session`/`peers` state is rendered), and tests `tests/test_app.py`, `tests/test_network.py`, `tests/test_security.py`, `tests/test_robustness.py`. Test-first. This task is transport + state only; routing across N screens (session/layout/UI) is the next task, M-B.

## Model (decided; do not redesign)
- Star: one **host** (the machine whose keyboard/mouse is shared) holds up to `MAX_CLIENTS = 8` outbound links, one per client. A **client** holds exactly one inbound link from its host. A device is host or client, never both, never two hosts.
- Roles stay initiator-based: pressing Connect on device A to device B makes A host (or keeps it host if it already is) and B client. Connecting from a device that is currently a *client* is refused locally with a clear error ("Disconnect from <host> first").
- Each client connection is a separate pairing/auth handshake exactly as today (per link, not per app).

## Requirements
1. **Per-link handshake state.** Replace the app-global `_phase`, `_active`, `_nonce`, `_pending`, `_peer_id`, `_peer_name`, `_peer_monitors`, `_peer_caps`, `_last_received`, heartbeat stop event, `_outbox`, `_client_session`… with a `_Peer` (dataclass) per link that owns: `device_id`, `name`, `link`, `phase`, `nonce`, `pending`, `monitors`, `caps`, `version`, `outbox`, inbound queue (from P2), heartbeat state, `last_received`. `App` keeps `self._peers: dict[str, _Peer]` keyed by device_id plus a `self._handshakes: list[_Peer]` for links whose device_id is not yet known. Keep the public js_api method names (`connect`, `connect_manually`, `disconnect`, `submit_code`, `rename`, `set_offset`, `forget`, …) working; `disconnect(device_id=None)` disconnects one peer or all.
2. **Host side, N outbound links.** `_connect_to` may be called while already host with other clients connected; it adds a peer. `MessageClient` stays one-link; the host holds one per peer. `_send(peer_id, msg)` targets a peer; `_broadcast(msg)` sends to all session-phase peers (used later for clipboard).
3. **Client side, one inbound link.** `MessageServer` keeps accepting one *host*; a second inbound connection while a host link is active is refused with `pair_err{reason:"busy"}` then closed (today it is silently dropped). While acting as host, inbound connection attempts are also refused with `busy` (a host cannot become someone's client).
4. **Duplicate identity.** A `pair_request`/`auth` whose `device_id` equals a peer already connected (or equals our own id) is refused with `pair_err{reason:"duplicate"}` and the new link is closed; the existing session is untouched. Two handshakes racing for the same id: the first to complete wins, the other gets `duplicate`.
5. **Simultaneous connect arbitration** (`pick_winner`) is now per pair of devices and must not disturb other peers: the loser drops only its outbound attempt to that device.
6. **Disconnect of one peer** tears down only that peer: its outbox/heartbeat/queue stop, `HostSession` is told (`on_peer_lost(device_id)` — add to `HostSession`; if the cursor is on that peer, release exactly as `on_disconnect` does today; otherwise just forget it). Other peers stay connected. `_do_teardown` becomes per-peer; `stop()`/window close tears down all.
7. **Reconnect of a paired peer** while others stay connected works through the same path (`connect` → new `_Peer`).
8. **State published to the UI**: `state["peers"]` entries gain `connected: bool` per device (already computed for one); `state["session"]` becomes `{"role": "host"|"client"|None, "remote": bool, "active_peer": device_id|None, "clients": [device_id...]}`. Update `app.js` minimally so the existing UI keeps rendering (show "N connected" and list names); the layout canvas for N devices is task M-B.
9. **HostSession** constructor accepts the set of peer ids and monitors per peer (`peers: dict[device_id, monitors]`) and `add_peer`/`remove_peer` methods; internal routing stays as today for the single-peer case (M-B generalises the crossing). Keep every existing `HostSession` test passing.
10. `config.Config.peers` already is a dict per device — no change needed except that `last_address`/`last_port` update per peer.

### Tests (all must be added; use the existing loopback harness)
- `tests/test_multi.py` (new): one host + three clients over loopback (4 Apps): all pair, `state["session"]["clients"]` lists three ids; disconnecting client 2 leaves 1 and 3 connected; client 2 reconnects; a fourth connection attempt from a device id equal to a connected client is refused with `duplicate` and the original stays connected; a connect attempt *from* a client to a third device is refused locally; a host receiving an inbound connect gets `busy`; `MAX_CLIENTS` enforced (9th refused with `pair_err{reason:"full"}`).
- `tests/test_security.py`: the existing attacker cases still hold per link (an unauthenticated inbound socket cannot affect an existing session's peer), plus: an attacker's `pair_request` with a connected peer's device_id is rejected as `duplicate` without disturbing that peer.
- Every existing test keeps passing; where a test reaches into renamed private attributes, update the test to the new `_Peer` structure rather than keeping compatibility shims.

### Constraints
- Headless importability; no new deps; ruff clean; no unrelated refactors beyond what the restructuring needs.
- Do NOT commit; leave changes in the working tree; tick M1+M2 in the plan's Progress list.
- Also required check: the suite must pass under Windows Python too — do not depend on real monitor geometry in tests (use the deterministic monitors fixture from P2).

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
End with: files changed, the `_Peer` fields, the refusal reasons and where each is emitted, per-peer teardown order, check tail (2 lines), anything left out with reason.

## Carried over from the P3 review (required here)
- [important, security] `app.py` (P1 code) recomputes `_negotiated_version` from every frame (`min(VERSION, msg["v"])`), so a v3 peer can send one `v:2` frame to reach the v2 "pair_ok without hmac" exception (downgrade). With per-link `_Peer` state, pin `version` on the link's **first** frame and treat any later frame whose `v` differs from the pinned version as a `ProtocolError` (link closed). Test: a peer that opens with `v:3` and then sends `pair_ok` with `v:2` and no `hmac` is torn down, and no token is saved.
- [minor, UI] `app.js`: the "peer does not authenticate" warning is hidden while `remote` is true (the escape hint replaces it). Show both.
