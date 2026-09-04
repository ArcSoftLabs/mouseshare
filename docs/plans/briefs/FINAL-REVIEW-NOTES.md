# Notes for the final integration review

## Hunks edited by the controller (Fable), reviewer-prescribed but not themselves reviewed
1. `mouseshare/state.py` `deliver()`: the revision check and `_deliver` now both run under `_deliver_lock` (A1 delta finding). Commit 1aebba3.
2. `mouseshare/app.py` `_default_offset` / `_build_layout`: defaults consider only already-placed devices; saved offsets seeded first (M-B delta finding). Commit 4b559a4. Test `test_default_offsets_use_only_placed_devices` (its first version had a wrong `can_place` signature, fixed before commit).
3. `tools/dragdrop/win_edge_drop.py`: ctypes argtypes for 64-bit handles (found by running on the PC). Commit ab6b1be.
4. `pyproject.toml` / `.github/workflows/build.yml`: explicit ruff rule selection and version pin (H2). Commit 1ee4877.
5. `tests/test_transfer.py` `test_one_mib_transfer_streams_with_bounded_sender_and_receiver_memory`: the peak bound is `512 KiB + 2 * io.DEFAULT_BUFFER_SIZE` (F1 rework 2). Python 3.14 on Windows opens files with a 128 KiB buffer (8 KiB on Linux 3.10) and the transfer holds two handles; Sol's flat 512 KiB failed on `py.exe` at ~675 KB. A hoarding-receiver probe still trips the new bound on both platforms. Also note: Sol lowered `ACK_EVERY` 8→4 in `transfer.py` for the memory bound without being asked; reviewed in the round-2 delta review and ratified (the F1 brief said "e.g. 8"; LAN throughput is encode-bound). Controller also added `or send.failed` to the chunk-loop guard in `_run_send` (round-2 delta finding: after a remote `xfer_error` the sender kept emitting up to ACK_EVERY-1 chunks).

6. `mouseshare/app.py` `_default_offset`: the no-arg form (used by the pairing persist path) now counts offline peers with a saved offset, mirroring `_build_layout` (D2 final review, 2026-09-04: a first pairing was persisted on top of an offline placed peer). Test extended: `test_default_offsets_use_only_placed_devices`.

## D2 final integration review (2026-09-04, ~12:50)
Hunks 1, 3, 4, 5 confirmed present and OK; hunk 2 fixed as item 6. No deadlock found across `App._lock`, `TransferManager._lock`, `send.acked` and the state locks; teardown cleans clipboard, transfers and routing. A v0.2.0 peer never receives v3-only frames (all gated on caps it never sends). Minor hardening left open: gate `_set_caps` on `peer.version >= 3` (currently caps-gated only); `TransferManager.receive` does not check that the message's peer owns the transfer id (ids are uuid4 and never relayed); no test asserts a peer's caps change after the `layout` re-broadcast when a share setting is toggled; nothing asserts the sender stops after a remote `xfer_error`.

## Known caveats carried in the plan doc
- First outbound `pair_request` is encoded v3; a strict v2 decoder rejects it (P1 progress note). Live test against the Mac's v0.2.0 planned/performed — see LIVE-V2 notes.
- Mixed DPI documented as a limitation (D1).
- macOS multi-display origin (A4) pending the probe.

## Live-run notes (2026-09-04, early morning, Benjamin absent)
- Windows clipboard backend: `OpenClipboard` returned ERROR_ACCESS_DENIED (5) for every WSL-launched process, and `powershell Get-Clipboard` failed identically ("Requested Clipboard operation did not succeed"); input desktop = Default, no clipboard owner/open window. Environmental (session state), not the backend. **Re-run the clipboard probe with Benjamin at the PC** before marking Windows clipboard verified. Probe script: `/tmp/claude-1000/clip_probe.py` (copy to a Windows-visible path, run with `py.exe`).
- v2 interop: new Windows → v0.2.0 Mac fails at the first frame (v:3); fixed by task P4. Re-run `/tmp/claude-1000/ms-live/scratch/live_v2.py` after P4 lands (from a worktree at the P4 commit).
- **v2 interop VERIFIED after P4 (2026-09-04 03:5x):** `py.exe scratch/live_v2.py` from the P4 tree → `phase=session role=host connected=True negotiated_version=2`, SENT pair_request/auth/layout at v2, RECEIVED pair_challenge/pair_ok at v2, `unauthenticated_peer: True`. Mac side: packaged v0.2.0, token auth, no hands. Support-matrix entry: "new Windows host ↔ v0.2.0 macOS client: pairing verified live; input untested".
- **Linux X11 backend smoke VERIFIED on WSLg (XWayland) 2026-09-04:** session_type=x11, permissions needed=False, xorg listeners + warper alive, start_remote grabbed pointer+keyboard (GrabSuccess), warp to anchor confirmed by query_pointer, stop_remote ungrabbed cleanly. `--smoke` passes zeroconf/monitors/pynput(_xorg); webview backend absent in WSL (expected). Untested: whether an XWayland grab confines a real human's input; no human input exercised. Script: /tmp/claude-1000/linux-smoke/smoke.py.
