# MouseShare — Design Spec (2026-08-30)

## Goal

Let a Logitech Hero Bluetooth mouse that is paired to **one** computer (macOS or
Windows) control **both** computers, moving seamlessly between their screens,
with a GUI to configure where each screen sits relative to the other.

**Key clarification:** the mouse stays Bluetooth-paired to one machine (the
*host*). MouseShare forwards mouse input over the local network to the other
machine (the *client*) — the Synergy / Mouse Without Borders model. No
Bluetooth re-pairing or HID proxying is involved; the hardware brand is
irrelevant to the software.

Scope: mouse only (movement, buttons, scroll). No keyboard sharing, no
clipboard, no more than two machines. YAGNI.

## Architecture

Two roles, one codebase:

- **Host** (mouse plugged in): captures global mouse events with `pynput`.
  While the cursor is on the host screen, events pass through untouched. When
  the cursor crosses the edge into the client's configured region, the host
  enters *remote mode*: it suppresses local mouse events
  (`win32_event_filter` on Windows, `darwin_intercept` on macOS), pins the
  physical cursor to the screen center to harvest movement deltas, and streams
  events to the client over TCP. Crossing back returns control locally.
- **Client**: connects to the host, receives events, injects them with
  `pynput.mouse.Controller`.

### Virtual plane / layout model

Each screen is a rectangle `(x, y, w, h)` on a shared virtual plane. The
layout editor lets the user drag the two rectangles; adjacency defines which
edge hands off. Crossing logic: when the host cursor would leave its own
rectangle, project the point just beyond the edge; if it lands inside the
client rectangle, hand off with the mapped entry coordinate. Same test in
reverse (client edge back onto host rect) ends remote mode.

### Wire protocol

Newline-delimited JSON over one TCP connection, default port **39471**.
Messages: `hello` (role + screen size; the host adopts the client's real
resolution), `enter {x,y}`, `pos {x,y}` (absolute client-screen position —
absolute rather than deltas so tracked and injected cursors cannot drift),
`click {button,pressed}`, `scroll {dx,dy}`, `leave`. JSON keeps it
debuggable; volume (~125 events/s) is trivial for a LAN.

Suppression mechanics (verified against pynput 1.8 sources): on Windows,
`suppress_event()` raises before pynput dispatches callbacks, so in remote
mode the raw messages are decoded and forwarded from inside
`win32_event_filter`; the frozen cursor makes `data.pt` = anchor + delta.
On macOS callbacks fire before `darwin_intercept` decides suppression, and
the intercept passes injected events through so the host's own re-anchoring
warp survives.

### Modules

| Module | Purpose | Testable in CI |
|---|---|---|
| `layout.py` | Screen rects, crossing detection, coordinate mapping | yes (pure) |
| `protocol.py` | Event dataclasses, encode/decode | yes (pure) |
| `config.py` | Load/save `~/.mouseshare/config.json` | yes (pure) |
| `network.py` | TCP server/client, line framing | yes (loopback) |
| `capture.py` | pynput capture + suppression (host) | no — thin adapter |
| `inject.py` | pynput injection (client) | no — thin adapter |
| `app.py` | Role orchestration, remote-mode state machine | partially |
| `layout_editor.py` | tkinter drag-and-drop screen arrangement | no — thin |
| `__main__.py` | CLI: `mouseshare host`, `mouseshare client <ip>`, `mouseshare layout` | — |

All platform-specific imports are lazy so the pure core imports cleanly on any
platform (including CI on Linux).

## Packaging

- Python ≥ 3.10, sole runtime dep: `pynput` (tkinter ships with Python).
- PyInstaller one-file spec (`packaging/mouseshare.spec`).
- GitHub Actions workflow builds artifacts on `macos-latest` and
  `windows-latest` on tag push / manual dispatch, uploads
  `MouseShare-macos.zip` and `MouseShare-windows.zip`.

macOS requirement (documented in README): grant the app **Accessibility** and
**Input Monitoring** permissions in System Settings, or capture/injection is
silently blocked by the OS.

## Testing

TDD on the pure core: layout crossing/mapping in all four adjacency
directions, protocol round-trips, config defaults + round-trip, network
framing over loopback. Platform adapters stay thin and untested (cannot run
in WSL/CI); they only translate pynput callbacks to/from tested core calls.

## Repository

GitHub: `ArcSoftLabs/mouseshare` (account already authenticated via gh CLI).

## Process note

Classified as architectural under the brainstorming skill. The session runs
under an autonomous `/goal` directive that forbids pausing for user input, so
the interactive approval gates were replaced by this committed spec and
advisor review.
