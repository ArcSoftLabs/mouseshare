# Task C — clipboard synchronisation (plan tasks C1 + C2)

Repository: current working directory. Read `docs/plans/2026-09-03-capability-expansion.md` §2.6 and §4 "Block C", then `mouseshare/protocol.py` (chunk helpers, OPTIONAL_TYPES registry, caps), `mouseshare/app.py` (per-peer `_Peer`, `_broadcast`, `_dispatch`, settings methods such as `set_escape_key`, `_do_teardown`), `mouseshare/config.py`, `mouseshare/macos.py` (main-thread constraints), `mouseshare/state.py`, `mouseshare/ui/web/index.html` + `app.js` (settings panel pattern), and tests `tests/test_app.py`, `tests/test_multi.py`, `tests/test_config.py`. Test-first.

## Requirements

### C1 — `mouseshare/clipboard.py`: backends
1. A backend protocol with `read_text() -> str | None`, `write_text(text: str) -> None`, `sequence() -> int` (a cheap change counter that increments on every clipboard change, including changes we did not make). Implementations selected by `sys.platform`, all lazily imported:
   - Windows: ctypes `user32.GetClipboardSequenceNumber` for `sequence`; `OpenClipboard`/`GetClipboardData(CF_UNICODETEXT)`/`SetClipboardData` with `GlobalAlloc/GlobalLock` for read/write; retry `OpenClipboard` up to 5× with 10 ms sleeps (another app may hold it); always `CloseClipboard` in `finally`. Empty clipboard → `read_text()` returns `""`; non-text clipboard → `None`.
   - macOS: `AppKit.NSPasteboard.generalPasteboard()`, `changeCount()` for `sequence`, `stringForType_(NSPasteboardTypeString)`, `clearContents()` + `setString_forType_`. **Open problem from the plan (resolve it, document the answer in the module docstring):** determine from pyobjc/AppKit documentation or a defensive design whether these calls are safe off the main thread. If not certain, marshal them to the main thread via `NSObject.performSelectorOnMainThread_withObject_waitUntilDone_` on a small helper class, or via pywebview's main-loop hook; do not add a new pynput Controller or keyboard-layout read off-main (see `macos.py`).
   - Linux (X11): via `Xlib` (already a pynput dependency): `sequence` derived from the CLIPBOARD selection owner window id plus a polled `XGetSelectionOwner` change counter; read via `XConvertSelection` UTF8_STRING on a hidden window with a 200 ms timeout; write by owning the selection from a daemon thread that serves `SelectionRequest` for UTF8_STRING/STRING/TARGETS. Keep it simple; document limits. If the display is unavailable, the backend reports `available = False` and the feature is off.
   - `FakeClipboard` (in `tests/`) with settable text and a sequence counter.
2. `Clipboard.create()` factory returning the platform backend or `None` if unavailable; `available` property.

### C2 — sync engine (`mouseshare/clipboard.py` `ClipboardSync`, wired in `app.py`)
3. A daemon watcher thread polls `sequence()` every `CLIPBOARD_POLL = 0.25` s **only while at least one session-phase peer with cap `"clipboard"` is connected**; on change it reads the text and publishes `clip` to all such peers (host → every client; client → its host, and the host **relays** to its other clients so all N stay in sync).
4. Wire: cap string `"clipboard"`; message `clip{seq, text}` for payloads ≤ 32 KiB, otherwise chunked with `protocol.chunk_payload("clip_chunk", id, utf8)` and reassembled with `ChunkAssembler` (cap 1 MiB). Above `CLIPBOARD_MAX = 1 MiB` the text is not sent and a notice "Clipboard too large to share" is shown once. Register `clip` and `clip_chunk` as optional types. Unicode and multiline round-trip byte-exact; empty string is a valid payload and clears the peer clipboard.
5. Loop prevention: when applying remote text, record `(sha256(text), sequence_after_write)`; the watcher ignores a change whose sequence equals the recorded one or whose content hash equals the last applied/sent hash. Include a test that two fake clipboards linked through two Apps settle after exactly one exchange.
6. Conflict resolution: last writer wins by arrival order at each device; each `clip` carries the source `device_id` (set by the sender; the host relays unchanged) and the UI shows "Clipboard from <name>" as a transient notice. Do not echo a payload back to its source.
7. Setting `share_clipboard: bool` (default `True`) in `config.Config`, persisted, exposed as `App.set_share_clipboard(bool)`, a checkbox in the settings panel, applied immediately (watcher stops; inbound `clip` ignored while off — and the cap is *not* advertised when off at connect time; if toggled off later, inbound is dropped silently).
8. Robustness: malformed `clip` (missing/non-string `text`, bad `seq`) → dropped with a type-only debug log, link stays up; backend exceptions (clipboard locked, encoding) are caught and logged **without the payload**; disconnect mid-chunk discards the assembler for that peer.
9. **Never log clipboard content.** Add a test that captures logging at DEBUG during a sync and asserts the payload string does not appear in any record; also assert it never appears in `state` snapshots (only the source name does).

### Tests (`tests/test_clipboard.py`, plus `tests/test_multi.py` for relay)
- Backend protocol contract tests against `FakeClipboard`; the Windows ctypes path exercised with a fake `windll` (like `tests/test_capture_windows.py`) for the open/retry/close and empty/non-text cases; the macOS and Linux backends at least import-checked under a fake module (no live display in CI).
- Engine: change → publish; remote apply → no echo; loop settles; empty payload; unicode/multiline; large payload chunked and reassembled; over-cap refused with notice; setting off stops watcher and drops inbound; malformed dropped; content never logged.
- Multi: host relays a client's clipboard to the other two clients; a v2 peer (no cap) never receives `clip`.

### Constraints
- Headless importability; no new dependencies; ruff clean; no unrelated refactors.
- Do NOT commit; tick C1/C2 in the plan's Progress list. Suite must also pass under Windows Python.

## Check command (must exit 0)
```
ruff check mouseshare tests && python3 -m pytest -q tests
```

## Report
End with: files changed, the loop-prevention rule in 3 lines, the macOS threading answer with its source, chunk/cap numbers, check tail (2 lines), anything left out with reason.

## Preservation rules (non-negotiable; a violation fails review outright)
- Edit incrementally; never rewrite a module wholesale. Every comment, docstring and type annotation that exists today survives unless the code it describes is removed; new code carries the same standard.
- Every `with self._lock` (and any other lock discipline) stays exactly as it is unless the brief names a specific change. Joins, socket closes and `stop_inbound()` stay outside the app lock.
- Never delete, skip, or weaken an existing test or assertion; if a test must change because an interface changed, keep its assertion's meaning and say so in the report.
- Report the before/after line count of every module you touch; a net shrink on a feature task must be explained line by line.
