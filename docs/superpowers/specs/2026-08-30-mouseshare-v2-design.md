# MouseShare v2 — Design Spec (2026-08-30)

Supersedes `2026-08-30-mouseshare-design.md`, which described the v0.1 CLI
prototype. That prototype's core (capture, injection, protocol, transport,
layout) survives; its presentation and its host/client split do not.

Reviewed by Sol (2026-08-30); findings folded in. Two recommendations were
rejected — see *Rejected review findings* at the end.

## Goal

One installable desktop app for macOS and Windows that shares a single
keyboard and mouse between them. The two machines find each other on the
local network. Connecting requires entering a code the other machine
displays. All monitors of both machines are arranged in a visual editor.

Personal use, **exactly two machines**, trusted home LAN. Not a product.

## Scope: two machines, many monitors

One host, one client, one connection. A third machine is out of scope: with
a single peer connection there is no route from the client's monitor onward
to a third device, and inventing a relay for hardware the owner does not
have is the definition of speculative.

Multi-*monitor* is in scope and required — both machines report every
monitor they have.

## What changes from v0.1

| v0.1 | v2 |
|---|---|
| `mouseshare host` / `mouseshare client` CLI roles | One app; roles decided per connection |
| Peer IP typed into a config file | mDNS discovery, peers listed in the UI |
| No authentication | Pairing code, then a stored token |
| Mouse only | Mouse and keyboard |
| One screen per machine | All monitors of both machines |
| Tkinter layout editor | pywebview UI |

## Architecture

```
              main thread                    background threads
        ┌──────────────────────┐      ┌─────────────────────────────┐
        │ webview.start()      │      │ discovery (zeroconf)        │
        │  └ ui/web/*.js       │      │ network  (TCP reader)       │
        └──────────┬───────────┘      │ capture  (pynput listeners) │
                   │ js_api           └──────────────┬──────────────┘
        ┌──────────┴───────────┐                     │
        │ ui/api.py            │◄────────────────────┘
        │  state owner (lock)  │   publish(snapshot) — coalesced,
        └──────────┬───────────┘   dispatched onto the UI thread
                   │
        ┌──────────┴─────────────────────────────────┐
        │ session.py — roles, crossing, safety       │
        └───┬─────────┬──────────┬─────────┬─────────┘
            │         │          │         │
         pairing    layout    monitors   network ── capture / inject
```

Platform-touching units (`capture`, `inject`, `monitors`) keep their
pynput/OS imports lazy so the core test suite runs on Linux CI.

### Threading contract

Non-negotiable, and the reason it is stated first:

- `webview.start()` **must** run on the main thread — pywebview's own
  docstring says so, and on macOS Cocoa requires it. The main thread does
  nothing else.
- zeroconf, the TCP reader, and both pynput listeners run on background
  threads they own.
- **`ui/api.py` is the single state owner.** All mutation goes through one
  lock. It holds a monotonic `revision` counter.
- Background threads never touch the webview. They call
  `api.publish()`, which coalesces and marshals onto the UI thread via
  `window.evaluate_js`. Snapshots carrying a lower revision than the last
  delivered one are dropped, so out-of-order publishes cannot rewind the UI.
- The JS side calls `get_state()` once on load; `publish()` is a no-op
  until that ready handshake, so state changing before the DOM exists is
  not lost — it is simply read at handshake time.
- Shutdown order: stop capture (un-suppressing), close connection, stop
  discovery, then quit the GUI loop.

### Window lifecycle

Closing the window **quits the app**. Minimizing keeps sharing alive.

The alternative — a hidden window with sharing still running — requires a
tray/menu-bar item to get back, and would strand an incoming pair request
with no way to display its code. One tray icon per platform is more code
than this buys, so v2 does not have one.

### Pairing

1. On machine A the user picks machine B from the discovered list and hits
   Connect. A opens TCP and sends `pair_request {device_id, name}`.
2. B generates a random 6-digit code (`secrets.randbelow(10**6)`, rendered
   zero-padded to exactly 6 ASCII digits), displays it, and sends
   `pair_challenge {nonce}` — 16 random bytes, lowercase hex.
3. The user types the code into A.
4. A sends `pair_proof {hmac}` where

   ```
   transcript = nonce_hex || "|" || a_device_id || "|" || b_device_id
   hmac       = HMAC-SHA256(key=code.encode("ascii"), msg=transcript.encode("ascii")).hexdigest()
   ```

   Binding both device ids stops a proof being replayed against a
   different pair of machines.
5. B recomputes and compares with `hmac.compare_digest`. On success B
   generates a **random** 32-byte token (`secrets.token_bytes(32)`, stored
   and sent as hex) and replies `pair_ok {name, token, monitors[]}`. Both
   sides persist it keyed by the peer's device id.
6. Later connections: A sends `pair_request`, B replies `pair_challenge`,
   A sends `auth {device_id, hmac}` where the hmac is keyed by the **token
   bytes** over the same transcript. The token itself never crosses the
   wire again.

The token is random, not derived from the code — a derived token would be
computable by anyone who watched the pairing exchange. Each connection gets
a fresh nonce, so a captured `auth` cannot be replayed.

Three wrong codes, or 120 seconds, drops the connection and burns the code.

**Security scope, stated plainly.** This authenticates the peer; it does
not encrypt the stream. Keystrokes — passwords included — cross the LAN in
clear JSON, and anyone who can sniff the network can read them. On a home
LAN with two personal machines that is an accepted trade, not an oversight.
Adding TLS would mean certificate management for a two-machine setup.

### Role arbitration

If both machines hit Connect simultaneously, each has one outbound and one
inbound session and each could believe it is host. Deterministic
tie-break: **the connection whose initiator has the lexicographically
smaller device id survives**; the other is closed. This is settled before
either side enables suppression, so no window exists where both machines
suppress input at once.

`network.MessageServer` currently replaces `_conn` on any new inbound
connection. It must instead refuse a second connection while one is live.

### Monitor model and the shared plane

`monitors.enumerate()` returns, for the local machine:

```python
Monitor(id: str, x: int, y: int, w: int, h: int, primary: bool)
```

**Invariant, and the thing to verify first:** these coordinates round-trip
through pynput. Setting `Controller.position = (m.x, m.y)` must put the
cursor at that monitor's top-left, and reading it back must return the same
pair. Windows mixes physical pixels with per-monitor DPI-aware logical
coordinates; macOS reports points, not pixels. Until this round-trip is
demonstrated on a machine, these numbers are not called pixels.

Verification matrix, run manually on both machines: 100% and >100% scaling,
Retina, negative origins (a monitor left of or above the primary), and
mixed resolutions.

The shared plane places each *device* at an offset:

```
virtual_x = device.offset_x + (monitor.x - device_min_x)
virtual_y = device.offset_y + (monitor.y - device_min_y)
```

Monitors keep their OS-reported arrangement within a device; only whole
devices move. Rearranging monitors *inside* one machine is the OS's job,
and pretending otherwise would let the UI show a layout the OS contradicts.

**Plane invariants:**

- Monitors of different devices never overlap on the plane. Placement is
  validated against the **union of monitor rectangles**, not the bounding
  box — an L-shaped arrangement has a bounding box far larger than the
  monitors in it, and snapping boxes could overlap real screens.
- Hit resolution is deterministic: candidates are sorted by
  `(device_id, monitor_id)` and the first containing rectangle wins. Given
  the no-overlap invariant there is at most one, but the sort means a bug
  in that invariant produces a repeatable result rather than a coin flip.
- Transitions between two monitors of the *same* device are ignored. The
  OS already handles those; the layout only answers "did the cursor leave
  this machine."

`layout.Layout` holds monitors keyed by `(device_id, monitor_id)`, with
`map_exit`, `clamp` and `contains` generalised over that key.
`snap(mobile, anchor)` becomes `snap_device`, operating on device extents.

Crossing detection is unchanged from v0.1: the OS clamps the cursor at a
screen edge, so the host probes one pixel beyond whichever edge is being
touched and asks the layout what lives there.

### Wire protocol

Newline-delimited JSON. Every message carries `"v": 2`; a mismatched
version is a disconnect. Lines over 64 KB are a disconnect. Malformed JSON
is a disconnect — **not** a silent skip as in v0.1, because a stream that
has desynchronised around authentication and global input suppression is
not a stream to keep guessing at.

| Message | Direction | Payload |
|---|---|---|
| `pair_request` | A→B | `device_id`, `name` |
| `pair_challenge` | B→A | `nonce` |
| `pair_proof` | A→B | `hmac` |
| `auth` | A→B | `device_id`, `hmac` |
| `pair_ok` | B→A | `name`, `token` (pairing only), `monitors[]` |
| `pair_err` | B→A | `reason` |
| `layout` | host→client | `monitors[]` — the host's own, for display |
| `enter` | host→client | `x`, `y` |
| `pos` | host→client | `x`, `y` |
| `click` | host→client | `button`, `pressed` |
| `scroll` | host→client | `dx`, `dy` |
| `key` | host→client | `kind`, `value`, `pressed` |
| `leave` | host→client | — |

**One coordinate rule:** `enter` and `pos` both carry absolute
client-OS coordinates, already resolved by the host. There is no `mon`
field. The client is a dumb injector that never sees the layout — which is
what makes it testable without a second machine.

**Key format** is tagged, because a bare character cannot represent every
key-up/key-down event and `"Key.shift"` is not resolvable through
`KeyCode`:

```json
{"t": "key", "kind": "special", "value": "shift_l", "pressed": true}
{"t": "key", "kind": "char",    "value": "a",       "pressed": true}
```

`special` resolves through `pynput.keyboard.Key[value]`, `char` through
`KeyCode.from_char(value)`. A key that resolves to neither is dropped and
logged; the sender never retries it.

The host owns the layout for sessions it drives and sends its own monitor
list in `layout` at session start, so the client's UI can draw the
arrangement without needing layout authority of its own.

### Keyboard capture

This is the largest new piece of platform code and the **only part neither
CI nor Linux tests can validate**. It is prototyped on the real Windows
machine before any UI work.

It is *not* a free ride on the working mouse path. The shared mechanic is
that `suppress_event()` raises before pynput dispatches, so the raw event
must be decoded in the filter — but decoding a keystroke is much harder
than decoding a mouse move.

- **Windows** — `win32_event_filter` on `keyboard.Listener`, decoding
  `KBDLLHOOKSTRUCT`. Must handle: `WM_KEYDOWN`/`WM_KEYUP` *and*
  `WM_SYSKEYDOWN`/`WM_SYSKEYUP` (Alt combinations arrive as the latter and
  are lost otherwise); the `LLKHF_INJECTED` flag, to ignore our own
  synthetic events; left/right modifier identity via the extended-key flag
  and vk codes `VK_LSHIFT`/`VK_RSHIFT` etc.; autorepeat, forwarded as
  repeated key-downs. The vk→`Key` table is built by reversing pynput's own
  `Key` enum rather than hand-written, and the printable path is checked
  against pynput 1.8's `KeyTranslator` — the same way the mouse path was
  verified against pynput sources.
- **macOS** — `darwin_intercept` returns `None` while remote. Callbacks
  fire before the intercept, so the forwarded event is read there, exactly
  as the mouse path does. Dead keys are not composed; the raw key is
  forwarded and the client's OS composes.

### Held-input safety

The client keeps a registry of **every** key and mouse button it has
pressed and not released. Everything in it is released on: `leave`,
disconnect, EOF, protocol error, auth failure, role change, and shutdown.

The host un-suppresses input *before* anything else on any transport
failure. A dropped connection must never leave a machine with a dead
keyboard — it is the one failure the user cannot recover from inside the
app, because they cannot type to fix it.

`network` therefore emits a disconnect event **exactly once**, on each of:
EOF, read error, write error, and local shutdown. v0.1 has no such
callback — `_read_loop` returns silently and `has_connection()` keeps
reporting true until a send happens to fail. That gap is a prototype-era
bug that becomes safety-critical the moment keyboard suppression exists.

### UI

Four screens in one window, no framework, no build step — plain
HTML/CSS/JS in `mouseshare/ui/web/`, bundled by PyInstaller as data.

1. **Devices** — discovered peers as cards: name, platform, state, Connect
   / Disconnect. Paired peers first. A manual "Add by IP" field covers the
   case where mDNS is blocked by a firewall or the two machines land on
   different subnets.
2. **Pairing** — as connector, a six-box code entry; as target, the code
   shown large with a countdown.
3. **Layout** — the centrepiece. Two device blocks on a pan/zoom canvas,
   each drawing its monitors to scale with labels. Drag a block; it snaps
   flush to the nearest neighbour edge on release, rejecting placements
   that would overlap. Saves on drop.
4. **Settings** — device name, port, paired peers with Forget, and on
   macOS a permissions panel for Accessibility and Input Monitoring: live
   status for each, a button opening the relevant System Settings pane, and
   a note that a rebuilt app may need re-approval.

Dark by default, light via `prefers-color-scheme`. System font stack.

### Modules

| File | Responsibility |
|---|---|
| `protocol.py` | Messages, versioning, `LineBuffer` with size cap |
| `network.py` | TCP, single-peer enforcement, disconnect events |
| `layout.py` | Plane, crossing, snapping, invariants |
| `capture.py` | Mouse **and keyboard** capture + suppression |
| `inject.py` | Mouse + key injection, held-input registry |
| `monitors.py` | **new** — per-platform monitor enumeration |
| `discovery.py` | **new** — zeroconf advertise + browse |
| `pairing.py` | **new** — codes, HMAC, token store |
| `session.py` | **new** — replaces `app.py`; roles, safety |
| `ui/api.py` | **new** — state owner, js_api facade |
| `ui/web/` | **new** — index.html, app.js, style.css |
| `layout_editor.py`, `app.py` | **deleted** |

### Identity and persistence

`~/.mouseshare/config.json` gains a `device_id` (a UUID4 hex, generated
once) and a `peers` map of `device_id → {name, token_hex, last_address}`.

Writes are atomic — temp file in the same directory, then `os.replace` —
and the file is `chmod 0600` where the platform supports it, because it now
holds bearer tokens. A missing or corrupt file is replaced with defaults
and a fresh identity rather than crashing the app.

### Discovery

Service type `_mouseshare._tcp.local.`, TXT carrying `device_id` and
`name`. **IPv4 only.** Peers whose `device_id` matches ours are filtered
out — a machine discovers itself otherwise. Peers are removed on zeroconf's
own removal events, not on a timer. The advertised port is used to
connect, not the local setting, so a peer on a non-default port still
works.

### Error handling

Surfaced in the UI, not logged and swallowed: peer unreachable, wrong code,
code expired, peer went away, port already bound, missing macOS
permissions, refused second connection, layout overlap rejected. Transient
command failures ride in the state snapshot so the UI renders them from the
same source as everything else.

## Testing

Pure units, on Linux CI, no display and no peer:

- `layout` — crossing between monitors of different devices, multi-monitor
  blocks, same-device transitions ignored, union-based overlap rejection,
  deterministic hit order, device snapping, clamping.
- `protocol` — round-trips, version mismatch, oversize line, malformed JSON
  raising rather than skipping, `LineBuffer` split handling.
- `pairing` — proof verification, wrong code, expiry, attempt limit,
  transcript binding (a proof for one device pair fails for another),
  random-token round-trip, reconnect HMAC.
- `network` — disconnect fires exactly once per cause; second connection
  refused.
- `session` — held-input registry drains on every terminal path; host
  un-suppresses before cleanup.
- `discovery` — advertise and browse against a local zeroconf; self-filter.
- `config` — v0.1 files load; atomic write; corrupt file recovers.
- `ui/api` — snapshot shape, revision monotonicity, stale drop, publish
  before ready.

Platform code (`capture`, `inject`, `monitors`) is verified by running the
app on both machines. Mocking pynput's suppression would test the mock.

**Release checklist, manual, both machines:** keyboard and mouse
suppression engage and release; disconnect mid-remote restores local input;
kill the peer process mid-remote; macOS permission prompts; mixed-DPI
coordinate round-trip; packaged app launches from its install location.

## Packaging

- macOS — real `BUNDLE` `.app`, `console=False`, stable bundle id
  `com.arcsoftlabs.mouseshare`, ad-hoc signed so TCC identity is stable
  across rebuilds. `NSAccessibilityUsageDescription` and
  `NSInputMonitoringUsageDescription` in the plist. Unsigned by a real
  certificate, so Gatekeeper warns on first launch and a rebuild may
  require re-approving permissions — documented, not fixed.
- Windows — one-file `.exe`, `console=False`. README notes the WebView2
  runtime requirement (present on Windows 11 and on updated Windows 10).

`mouseshare/ui/web/` ships as `datas`, resolved via `sys._MEIPASS` when
frozen.

### Stack gate

The pywebview choice is **provisional until a spike proves it**. The spike
is a packaged skeleton, not a hello-world window — a hello-world window
validates none of the risk. It must, as a packaged artifact on both
platforms:

1. Load a bundled HTML asset from `_MEIPASS`.
2. Complete one JS→Python→JS `js_api` round-trip.
3. Push one state update from a background thread to the UI.
4. Start and stop a zeroconf advertiser and browser.
5. Start pynput mouse **and** keyboard listeners alongside the webview.
6. Build `console=False`, as a real `.app` on macOS.
7. Launch on the owner's actual Mac and Windows machines.

CI build success proves packaging, not launch. If the spike fails, the
stack flips to PySide6 and only the spike is lost — which is why it comes
before the UI, not after.

## Non-goals

Clipboard sharing, file drag-and-drop, a third machine, keyboard
remapping, TLS, notarised code signing, auto-update, Linux support, tray
icon.

## Rejected review findings

Two of Sol's recommendations were declined, recorded here so the decision
is visible rather than lost:

- **"Drop mDNS; saved peer addresses are more YAGNI."** Declined. Network
  discovery is an explicit requirement from the owner, not an inference. A
  stated requirement outranks a code-size argument. The manual "Add by IP"
  field covers the failure modes that motivated the objection.
- **"Merge `pairing.py` and `session.py`."** Partially declined.
  `session.py` and `ui/api.py` are the natural merge if either turns out
  thin, and they may be merged during implementation. `pairing` stays
  separate: it is the one new unit with real cryptographic behaviour to
  test, and a testable seam earns its file.
