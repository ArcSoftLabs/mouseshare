# MouseShare capability expansion — implementation plan

Date: 2026-09-03. Status: **Confirmed 2026-09-03; Phase 2 in progress.** Nothing below is
implemented. Source prompt: `ImprovedPrompts/2026-09-03-mouseshare-capability-expansion.md`
(outside this repo).

Discovery was done by seven read-only subagents (networking/topology, Windows input,
macOS input, clipboard/file integration points, Linux feasibility, tests/CI, transition
core). Every `file:line` below comes from those reports against commit `0ca7e2c`.

---

## 0. Architecture as it stands (facts the plan depends on)

| Fact | Evidence |
|---|---|
| Role = initiator. Whoever presses Connect becomes **host** (captures, owns layout); the accepting side is **client** (injects only). No swap. | `app.py:182-184,399` / `app.py:546-551` |
| Only the host builds `InputCapture`/`HostSession`; the client has **no send path** and **no edge logic**. `leave` on the client only releases held keys. | `app.py:753-761,825-845`, `session.py:218-239` |
| Wire = newline JSON, `MAX_LINE` 64 KiB, `VERSION=2` exact-match, unknown type → teardown. | `protocol.py:14-15,110-127`, `app.py:451-481` |
| `enter`/`pos` carry **absolute pixels in the receiver's own OS space**; the receiver never clamps; no DPI/scale on the wire. | `layout.py:96-111`, `session.py:82,151`, `monitors.py:64-68` |
| Return crossing is computed only from the host's **modelled** peer position (`_peer_pos`), integrated from deltas measured against a parked anchor. | `session.py:60,136-152`, `capture.py:101-137` |
| Delta measurement assumes the OS **freezes the real cursor** when the event is suppressed — true for the Windows LL hook, *admitted untested* on macOS. | `capture.py:119-137,151-157`, commit `d1f186e` |
| macOS capture posts a warp (`CGEventPost`) **inside the tap callback**; pynput never re-enables a tap after `kCGEventTapDisabledByTimeout`. | `capture.py:113,135`, `pynput/_util/darwin.py:228` |
| No heartbeat, no socket timeout, no escape hotkey. A frozen peer leaves the host suppressing mouse *and* keyboard. | `network.py:182`, `session.py`, grep |
| Handshake state is app-global, one link per side, `pair_ok` is not authenticated (host trusts whoever answered). | `app.py:56-64,664-691`, `network.py:99,114-124` |
| Offsets never cross the wire; each side has its own `cfg.offsets`; only the host's routes. | `protocol.py:66`, `app.py:687-707` |
| `Layout` is already N-device; `App`, `network`, `session`, `app.js` hard-code two. | `layout.py:47-52,133-149` vs `app.py:703-732`, `network.py:99`, `app.js` |
| No clipboard or file code exists. Inbound handler runs inline on the reader thread. | grep; `network.py:60-78` |
| On Linux the app imports and *runs*, but `capture.py:168/220` leave listener kwargs empty → "suppressing" forwards input **and** leaks it locally. `--smoke` would pass vacuously. | `capture.py:168-242`, `__main__.py:72-77` |
| CI: pytest on ubuntu only (no pynput/pywebview installed), PyInstaller on mac/win, tag → release. No lint, no type-check. 167 tests. | `.github/workflows/build.yml` |
| `__pycache__` (9 files) is tracked in git. | `git ls-files` |

---

## 1. Root cause of the macOS-initiated trapped cursor

**Failing case = macOS is host, Windows is client.** This is the only configuration in
which the macOS capture path runs in remote mode, and it is the path the code itself
marks untested (`capture.py:151-157`).

**Primary mechanism (H1, high confidence, needs one Mac run to confirm):**
`InputCapture._moved` computes each event's delta as `position − anchor` and relies on
the suppressed event never moving the real cursor. On macOS, deleting a mouse-move in a
`kCGSessionEventTap` stops delivery to applications but does **not** stop the cursor
sprite (this is why Synergy/Barrier call `CGAssociateMouseAndMouseCursorPosition(false)`
and read `kCGMouseEventDeltaX/Y` instead). Consequently every event reports the
*cumulative* offset from the anchor, `_peer_pos` advances quadratically, the Windows
cursor is flung to the far edge and clamped (`session.py:151`), and the Mac's own cursor
visibly drifts. Because `_default_offset` (`app.py:695-698`) aligns both devices at
plane y=0 and the Windows screen is taller than the Mac's, amplified vertical error
parks `_peer_pos` on a Windows row that has **no plane neighbour**; `map_exit` then
returns `None` forever (`layout.py:109-111`, `session.py:142-143`) and no hand movement
can come back. That is the "trapped" state.

**Secondary mechanism (H2, plausible, may co-occur):** `start_remote` and `_moved` post
events from inside the tap callback; a slow callback gets the tap disabled by timeout
and pynput never re-enables it. Symptom: Windows cursor frozen exactly at the entry
edge, Mac cursor normal, `remote` still `True`.

**Why it is unrecoverable regardless of H1/H2:** the client has no `leave` path of its
own, there is no escape hotkey, no heartbeat, and nothing resets `suppressing` if a
listener thread dies. Any host-side fault is permanent until disconnect.

**Ruled out by measurement (2026-09-03, `py.exe` on Benjamin's PC):** Windows DPI
mismatch. `screeninfo.get_monitors()` flips the process from DPI-unaware (0) to
per-monitor-aware (2) as a side effect (`screeninfo/enumerators/windows.py:69`) and is
the first cursor-adjacent call in the process (`app.py:47`); afterwards `GetCursorPos`,
pynput's position and screeninfo's rects agree exactly (primary 2560×1440 at 100 %;
secondary 1920×1200 at 125 % stacked *below* it at y=1440, so the Windows plane is
2640 rows tall — which is exactly the geometry that makes H1's vertical runaway
unrecoverable). Also ruled out: `_park` limit fallback; client-side edge detection
(there is none). Caveat to preserve: any cursor API used *before* `get_monitors()` in a
process would see the secondary shrunk by 1.25 — keep the enumeration first.

**Confirmation step (task A0, zero code change in the app):** a standalone probe script
run on the Mac reports (a) whether the cursor moves while a deleting tap swallows
mouse-moved, (b) whether `kCGMouseEventDeltaX/Y` are populated, (c) whether posting from
inside the callback disables the tap. Plus `--debug` on the current build: `movement:
… biggest step` saturating near `limit` ⇒ H1; the line stopping ⇒ H2 (`session.py:154-173`).

---

## 2. Target design decisions

1. **Topology: star.** One host (the machine whose physical keyboard/mouse is shared)
   connects to N clients. Clients never talk to each other; a client→client transition
   (Mac→Windows→Linux) is the host switching which link it forwards to. This keeps the
   existing role model, needs no relaying, and the existing `Layout` already supports it.
   A device is host *or* client, never both; a connect attempt to a machine already
   acting as client is refused with `pair_err{reason:"busy"}`. Practical limit: 8
   clients (one reader thread + one outbox each; nothing else scales worse). Tested
   target: 4 devices in-process, 3 real (Windows, Mac, WSL Linux) if available.
2. **Protocol v3 with negotiation.** First message on a link carries `v`; both sides use
   `min(v)`. v2 peers keep working for input only; clipboard/file/heartbeat are gated by a
   `caps` list exchanged in `pair_ok`/`auth`-ok. Unknown *optional* types are ignored, not
   fatal. Oversized frames still tear down; large payloads are chunked (≤32 KiB).
3. **Client gets a send path** (an `Outbox` per link on both sides). Needed for
   clipboard, transfers, heartbeat acks, and a client-reported `edge` message.
4. **Recovery guarantees** (independent of the root-cause fix): heartbeat every 2 s,
   peer declared dead after 6 s → teardown → release; escape gesture (decided: tap Ctrl twice within
   0.5 s, Cmd on macOS; changeable in the settings panel) releases remote mode on the
   host and returns the cursor to the host screen;
   `stop_remote` warps the cursor back to a visible inset point; listener-thread death
   resets `suppressing`.
5. **macOS capture rewrite:** decouple cursor with `CGAssociateMouseAndMouseCursorPosition(False)`
   during remote mode, take deltas from the event's `kCGMouseEventDeltaX/Y` fields inside
   `_darwin_intercept` (the raw `CGEvent` is available there), never post from the
   callback, handle `kCGEventTapDisabledByTimeout/ByUserInput` by re-enabling
   `listener._tap`. Windows path unchanged (it works).
6. **Clipboard:** poll-based change detection (Windows `GetClipboardSequenceNumber`,
   macOS `NSPasteboard.changeCount`, Linux X11 via `xclip`-free Xlib selection owner
   polling) at 250 ms while a session is active; text only in v1; loop prevention by
   remembering `(hash, sequence)` of the last remotely applied content; last-writer-wins
   with source device recorded in state; 1 MiB cap; setting `share_clipboard` (default on).
   Rich text/images: separate follow-up tasks, not in this plan's DoD.
7. **File transfer: real cross-boundary drag and drop is the target** (Benjamin's
   decision, 2026-09-03). Mechanism (the Synergy 1.x approach): when the host detects a
   crossing with the left button held, it raises a transparent, borderless, topmost
   drop-target window under the cursor on the *source* screen before the warp, receives
   the OS drop (Windows: `IDropTarget`/`WM_DROPFILES` via ctypes; macOS: an `NSWindow`
   registered for file pasteboard types via pyobjc, created on the main thread), obtains
   the file list, then streams the files to the destination device, which places them in
   `~/Downloads/MouseShare/` and, on release there, moves them into the folder under the
   cursor when it is a Finder/Explorer window it can resolve, otherwise leaves them in
   the destination folder and shows a notice. Obstacles known in advance: `start_remote`
   warps the real cursor and macOS deletes the event (`capture.py:113,251-259`), so the
   drop-target window must exist *before* the warp and the warp must be deferred until
   the drop has landed; the drag source's file list is otherwise not observable
   cross-process. Task F0 is therefore a real implementation spike on both platforms
   with a hard deliverable: a working boundary drop, or written evidence of why it
   cannot work on that platform. Only where F0 produces that evidence does the fallback
   apply: dropping files onto the MouseShare window (pywebview delivers full paths via
   `pywebviewFullPath`) with a "send to <device>" picker. Either way: collisions →
   `name (2).ext`; streamed 32 KiB chunks; SHA-256 verified; cancel/interruption deletes
   the partial file; free-space precheck.
8. **Linux:** X11 first-class via pynput's xorg backend plus direct `XGrabPointer`/
   `XGrabKeyboard` for remote mode (pynput's xorg `suppress` is all-or-nothing at
   listener start). Wayland: **not claimed**; documented limitation with the
   `InputCapture`/`RemoteDesktop` portal path as future work. pywebview GTK backend.
   Artifacts: PyInstaller onefile → AppImage (appimagetool), `.deb` and `.rpm` (nfpm),
   `.tar.gz`. Built on `ubuntu-22.04` for glibc compatibility.

---

## 3. Verification reality

| Claim in the Definition of Done | Verifiable from this environment? |
|---|---|
| Unit/integration tests, 4 in-process devices | Yes (WSL `python3`, Windows `py.exe`) |
| Windows-specific behaviour, Windows→X direction | Yes, live, via `py.exe -m mouseshare` |
| Linux X11 runtime | Partially: WSLg (XWayland) on this machine after `pip install pywebview[gtk]` deps |
| macOS-initiated direction, macOS capture rewrite | **No.** Requires Benjamin's Mac. Task A0 and A3 have a "run on Mac" step. |
| Linux packaging + smoke | Yes, in CI (`xvfb-run`) |
| Wayland | Not claimed |
| Real 4-device session | No hardware. In-process only; real 3-device if Mac + PC + WSL |
| Pairings mac↔mac, linux↔linux, win↔win | Not testable here; marked "implementation-only" unless the user runs them |

The final support matrix will have three columns: **verified**, **implementation-only**,
**unsupported**, filled from actual runs, not from this table.

---

## 4. Tasks (ordered; S = serialized on shared core, P = parallelizable)

Each task: fresh implementer subagent, TDD, one commit, then reviewer gates. Check
commands are what the reviewer runs; `PYTEST="python3 -m pytest -q tests"`.

### Block H — housekeeping (S, first)
- **H1** Untrack `__pycache__`, add `*.pyc`/`__pycache__/` to `.gitignore`.
  Check: `git ls-files | grep -c __pycache__` prints `0`.
- **H2** Add `ruff` config to `pyproject.toml` and a `lint` step to CI; fix nothing beyond
  what ruff's default rules flag (small).
  Check: `ruff check mouseshare tests` exits 0.

### Block A — cursor bug (S)
- **A0** Mac probe script `tools/mac_tap_probe.py` (standalone, pyobjc via pynput's deps)
  answering H1/H2 questions in §1. Output pasted back by the user. *Gate for A3's design
  details only; A1/A2 proceed regardless.*
- **A1** Local recovery guarantees (no protocol change; runs after P1): escape chord in
  `capture.py`, `stop_remote` re-warps to an inset point, listener-thread death resets
  `suppressing`. Heartbeat lives in P1. Files: `capture.py`, `session.py`, `app.py`,
  `config.py`, `app.js`. Tests: `tests/test_session.py` (escape releases + sends
  `leave`), `tests/test_capture_windows.py`.
  Check: `$PYTEST -k "escape or release"`.
- **A2/A3 are gated on A0's output** — two branches, chosen by evidence, not guessed:
  - *H1 confirmed* (cursor moves under a deleting tap): **A2** regression test in
    `tests/test_capture_macos.py` — a fake darwin listener whose reported positions do
    *not* stay parked must still yield per-event deltas equal to the event's delta
    fields; fails on current code. **A3** macOS capture rewrite per §2.5 (`capture.py`,
    `macos.py`), Windows path untouched.
  - *H1 refuted* (cursor is frozen): A2 tests only that no `CGEventPost` happens inside
    the callback and that a disabled tap is re-enabled; A3 is limited to those two
    changes. §2.5's delta-field rewrite is then *not* done.
  - Either way, **A4** macOS multi-display origin: `monitors.py` converts screeninfo's
    AppKit bottom-left frames to the top-left space pynput uses (single display
    coincides; multi-display/negative-origin Macs currently map to wrong rows).
    Test with a two-display fake in `tests/test_monitors.py`.
  Check: `$PYTEST tests/test_capture_macos.py tests/test_capture_windows.py
  tests/test_monitors.py` + **run on Mac**: cross Mac→Win→Mac and Win→Mac→Win, all four
  edges, `--debug` shows bounded steps. Result recorded in `docs/support-matrix.md`.

### Block P — protocol v3 and client send path (S)
- **P1** Version negotiation + `caps`, optional-type tolerance, chunk helpers, and the
  heartbeat (`ping`/`pong` every 2 s, peer dead after 6 s → teardown → release).
  Files: `protocol.py`, `app.py:_on_message/_dispatch`, `network.py`.
  Tests: `tests/test_protocol.py`, `tests/test_network.py` (v2 peer still pairs and
  moves; v3-only type from v2 peer is dropped not fatal), `tests/test_app.py`
  (heartbeat timeout tears down and releases capture).
- **P2** Client-side `Outbox` and `ClientSession.send`; inbound handler moved off the
  reader thread onto a bounded queue (`app.py`, `session.py`, `outbox.py`). Test:
  handler exception no longer kills the link; `pos` flood still collapses.
- **P3** Fix `pair_ok` authentication asymmetry: target includes `hmac` over
  `nonce|target_id|initiator_id` with the same key; host verifies. Test in
  `tests/test_security.py` (forged `pair_ok` rejected). Required before exposing
  clipboard/files (constraint: never to unauthenticated peers).

### Block M — multi-device (S)
- **M1** `network.py`: host holds `{device_id: link}`; server accepts one *host* link per
  client; duplicate `device_id` → refuse new link with `pair_err{reason:"duplicate"}`.
- **M2** `app.py`: per-link handshake state (`_Peer` dataclass replaces the singular
  fields), `state["sessions"]` list, `_send(peer_id, msg)`, busy refusal, reconnect of a
  paired peer while others stay connected, disconnect of one peer releases only if the
  cursor is on it.
- **M3** `session.py`: `HostSession` routes by `map_exit` result across N peers,
  peer→peer hop = `leave` to old + `enter` to new; return only to `local_id`. Edge
  crossing already supports all four sides; add tests for top/bottom and diagonal.
- **M4** Layout/UI: `set_offset` for N devices, `snap_device` against all, `app.js`
  renders N blocks, overlap rejection. Offsets stay host-side (unchanged model).
- **M5** 4-device in-process test in `tests/test_multi.py`: one host, three clients over
  loopback, deterministic routing, cursor never trapped after each client's death,
  duplicate identity refused, topology change mid-session.
  Check: `$PYTEST tests/test_multi.py tests/test_session.py tests/test_app.py`.

### Block C — clipboard (P with L after M)
- **C1** `clipboard.py`: backend protocol (`read_text`, `write_text`, `sequence`) with
  Windows (ctypes), macOS (AppKit `NSPasteboard`), Linux X11 (Xlib) implementations; a
  `FakeClipboard` for tests. **Open problem for C1:** nothing in the repo can run code
  on the macOS main thread after `webview.start()` (`macos.prewarm` is a one-time
  pre-start hook). The implementer must verify from pyobjc/AppKit documentation whether
  `generalPasteboard` reads and `changeCount` are safe off-main; if not, add a main-thread
  dispatch (`performSelectorOnMainThread` or pywebview's main-loop hook) before C2.
- **C2** Sync engine: watcher thread, loop prevention, cap, setting, no logging of content
  (add a test that greps captured logs for the payload). Wire type `clip{seq,text}`,
  chunked. Both directions via P2.
  Check: `$PYTEST tests/test_clipboard.py`; live Windows↔Mac run.

### Block F — file transfer (P with L, after C)
- **F0** Native boundary drop spike, both platforms (Windows first, on the real PC via
  `py.exe`; macOS via SSH-deployed build + Benjamin's hands): transparent drop-target
  window raised on button-held crossing, drop received, file list printed. Deliverable
  is working code *or* documented evidence of impossibility per platform in
  `docs/file-transfer.md`. Decides whether F2 ships native drop or the drop-zone fallback.
- **F1** Transfer protocol: `xfer_offer{id,files:[{name,size,sha256}]}`, `xfer_accept`,
  `xfer_chunk{id,i,data(b64)}`, `xfer_done`, `xfer_cancel`, `xfer_error`. Path
  validation (basename only, reject `..`, separators, reserved names), collision
  renaming, streaming, temp file + rename on verified hash, disk-space precheck.
- **F2** UI: drop zone + device picker + progress list + success/failure banner using
  existing `#notice`/`#banner`/`.pulse` patterns. Drag-intent detection at crossing
  (button state now reported by capture in normal mode).
  Check: `$PYTEST tests/test_transfer.py` (validation, interruption, integrity,
  collision, cancel) + live run.

### Block L — Linux (P with C/F after A and P)
- **L1** `capture.py` xorg branch + `linux.py` (grabs, delta source, tap equivalents),
  `__main__.py` log path/XDG config dir, `_probe_backend` linux entry, permissions panel
  reporting X11 vs Wayland session and refusing to claim capture under pure Wayland.
- **L2** Packaging: `packaging/mouseshare.spec` linux hiddenimports, `packaging/linux/`
  (`mouseshare.desktop`, AppImage recipe, nfpm.yaml for deb/rpm), CI job on
  `ubuntu-22.04` with `xvfb-run ./dist/mouseshare --smoke`, artifacts added to release.
- **L3** Docs: `docs/linux-install.md` (deps, X11 vs Wayland, firewall, troubleshooting).
  Check: CI green with four Linux artifacts; smoke passes; WSLg live run of Win↔Linux.

### Block D — docs and release (S, last)
- **D1** `docs/support-matrix.md` (verified / implementation-only / unsupported),
  README, release notes, `docs/protocol.md` (v3). Documented limitation: **mixed DPI** —
  the plane treats a macOS point and a Windows physical pixel as equal, so cursor speed
  and edge alignment shift across a boundary between differently scaled screens. Not a
  trap. Fixing it would add a `scale` field to `monitors.to_wire`; deferred unless
  Benjamin asks for it in this release.
- **D2** Final integration review; full suites; tag `v0.3.0` only when all gates pass.

### Dependency graph
`H → A0 → P1 → A1 → P2 → P3 → M1..M5 → {C, L} → F → D`, strictly sequential. A2/A3/A4
slot in after A0's Mac result arrives (they touch `capture.py`/`macos.py`/`monitors.py`
only, so they can land anywhere before L1); A3's Mac verification run is a checkpoint
that can lag behind the sequence. C and L parallelize (no shared files once M is in). F
after C (shares the UI drop/picker plumbing).

---

## 5. Workflow (decided 2026-09-03)

**Sol implements, Fable plans and reviews.** Per task: Fable (fresh subagent) writes the
brief from this plan → Sol builds it via `codex exec -C <repo> --model gpt-5.6-sol
-s workspace-write` in a fresh Codex session with only the brief → the check command is
run by a neutral checker → gate 1 (spec compliance) and gate 2 (code quality) by fresh
Fable reviewer subagents that did not write the brief → Sol fixes findings → Fable
delta-reviews the fixes only. **Budget: at most two rework rounds per gate**; after that
the controller decides or escalates to Benjamin (his instruction: converge fast, don't
loop on small items; keep the controller session thin). One commit per task. Final:
integration review, full suite, packaging, support matrix.

Mac verification runs go through SSH (`bh@192.168.129.86`, key `~/.ssh/mouseshare_mac`,
GUI launch via `open`); Windows runs via `py.exe`; Linux via WSLg.

## 6. Decisions

| # | Question | Status |
|---|---|---|
| 1 | Implementer | **Decided:** Sol (Codex) implements, Fable plans and reviews |
| 2 | File transfer | **Decided:** real cross-boundary drag and drop is the target; drop-zone only as evidenced fallback |
| 3 | Linux: X11 only, Wayland documented as unsupported | **Decided:** yes |
| 4 | Device cap | **Decided:** 8 clients per host |
| 5 | Escape gesture | **Decided:** double-tap Ctrl (Cmd on Mac), configurable in settings |
