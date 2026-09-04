# Support matrix for MouseShare 0.3.0

“Verified” means the exact behavior had a real run. “Implementation-only”
means code and automated tests exist but the behavior did not have the required
real-device run. “Unsupported” is a deliberate product boundary. Evidence is
from controller Fable and is dated **2026-09-04**; no automated-test result has
been promoted to a live verification claim.

For directional pairs, the left platform is the host (the keyboard/pointer
source) and the right platform is the client. Linux → Windows and Linux → macOS
had no run of any kind and fall under the same implementation-only claim as the
listed direction. Each row has exactly one classified cell.

| Pairing / feature | Verified | Implementation-only | Unsupported |
|---|---|---|---|
| Windows → Windows — connection/reconnection | — | Code and tests; no two-device run | — |
| Windows → Windows — keyboard | — | Code and tests; no human-input run | — |
| Windows → Windows — pointer | — | Code and tests; no human-input run | — |
| Windows → Windows — boundary crossing both ways | — | Code and tests; no crossing run | — |
| Windows → Windows — clipboard | — | Backend and tests; no live clipboard run | — |
| Windows → Windows — file transfer | — | Loopback tests only | — |
| Windows → Windows — mixed DPI | — | Local Windows coordinate consistency measured; no cross-machine run | — |
| Windows → Windows — recovery after peer termination | — | Loopback/unit tests only | — |
| Windows → macOS — connection/reconnection | — | v0.3.0 code and tests; only v2 pairing was run | — |
| Windows → macOS — keyboard | — | Code and tests; no input exercised | — |
| Windows → macOS — pointer | — | Code and tests; no input exercised | — |
| Windows → macOS — boundary crossing both ways | — | Code and tests; no crossing run | — |
| Windows → macOS — clipboard | — | Backends and tests; no live clipboard run | — |
| Windows → macOS — file transfer | — | Loopback tests only | — |
| Windows → macOS — mixed DPI | — | Code and tests; no cross-machine run | — |
| Windows → macOS — recovery after peer termination | — | Loopback/unit tests only | — |
| macOS → Windows — connection/reconnection | — | v0.3.0 code and tests; macOS 0.3.0 never run | — |
| macOS → Windows — keyboard | — | Pre-existing macOS backend plus escape gesture; no 0.3.0 run | — |
| macOS → Windows — pointer | — | Pre-existing macOS backend plus escape gesture; no 0.3.0 run | — |
| macOS → Windows — boundary crossing both ways | — | Code and tests; no crossing run | — |
| macOS → Windows — clipboard | — | Backends and tests; no live clipboard run | — |
| macOS → Windows — file transfer | — | Loopback tests only | — |
| macOS → Windows — mixed DPI | — | Code and tests; no cross-machine run | — |
| macOS → Windows — recovery after peer termination | — | Loopback/unit tests only | — |
| Windows → Linux — connection/reconnection | — | Code and tests; WSLg run had no peer | — |
| Windows → Linux — keyboard | — | Only the host-side X11 grab was run (WSLg); no client run, human input or peer | — |
| Windows → Linux — pointer | — | Only the host-side X11 warp was run (WSLg); no client run, human input or peer | — |
| Windows → Linux — boundary crossing both ways | — | Code and tests; no crossing run | — |
| Windows → Linux — clipboard | — | Backends and tests; no live clipboard run | — |
| Windows → Linux — file transfer | — | Loopback tests only | — |
| Windows → Linux — mixed DPI | — | Code and tests; no cross-machine run | — |
| Windows → Linux — recovery after peer termination | — | Loopback/unit tests only | — |
| macOS → macOS — connection/reconnection | — | Code and tests; no two-device run | — |
| macOS → macOS — keyboard | — | Pre-existing backend; no 0.3.0 run | — |
| macOS → macOS — pointer | — | Pre-existing backend; no 0.3.0 run | — |
| macOS → macOS — boundary crossing both ways | — | Code and tests; no crossing run | — |
| macOS → macOS — clipboard | — | Backend and tests; no live clipboard run | — |
| macOS → macOS — file transfer | — | Loopback tests only | — |
| macOS → macOS — mixed DPI | — | Code and tests; no cross-machine run | — |
| macOS → macOS — recovery after peer termination | — | Loopback/unit tests only | — |
| macOS → Linux — connection/reconnection | — | Code and tests; no two-device run | — |
| macOS → Linux — keyboard | — | Code and tests; no human-input run | — |
| macOS → Linux — pointer | — | Code and tests; no human-input run | — |
| macOS → Linux — boundary crossing both ways | — | Code and tests; no crossing run | — |
| macOS → Linux — clipboard | — | Backends and tests; no live clipboard run | — |
| macOS → Linux — file transfer | — | Loopback tests only | — |
| macOS → Linux — mixed DPI | — | Code and tests; no cross-machine run | — |
| macOS → Linux — recovery after peer termination | — | Loopback/unit tests only | — |
| Linux → Linux — connection/reconnection | — | Code and tests; WSLg run had no peer | — |
| Linux → Linux — keyboard | — | Host-side X11 grab/ungrab verified on WSLg; no peer or human input | — |
| Linux → Linux — pointer | — | Host-side X11 warp verified on WSLg; no peer or human input | — |
| Linux → Linux — boundary crossing both ways | — | Code and tests; no crossing run | — |
| Linux → Linux — clipboard | — | Backend and tests; no live clipboard run | — |
| Linux → Linux — file transfer | — | Loopback tests only | — |
| Linux → Linux — mixed DPI | — | Code and tests; no cross-machine run | — |
| Linux → Linux — recovery after peer termination | — | Loopback/unit tests only | — |
| 4-device sessions | — | Four-device in-process loopback tests only; no real hardware session | — |
| Protocol v2 interoperability | 2026-09-04: Windows host at `26e0f36` ↔ packaged macOS v0.2.0 client via `tools/live_v2_probe.py`; pairing verified live, input untested; negotiated v2 and client was flagged unauthenticated | — | — |
| Wayland | — | — | X11 only: Wayland provides no global input grab/warp without a compositor-specific portal |

## Additional evidence and gaps

The complete suite passed on real Windows Python 3.14 (355 passed, 1 skipped),
and local DPI coordinate spaces matched the hook coordinates. The Windows
native drop-target probe registered successfully, but no real drag occurred.
These facts support implementation confidence only; they do not verify a
pairing/feature row above.

The standalone Linux X11 backend did have a limited live run on 2026-09-04:
`tools/linux_x11_smoke.py` on WSLg/XWayland verified session detection,
successful pointer/keyboard grabs, an anchor warp confirmed through
`query_pointer`, and clean ungrabbing. There was no peer and no human input, so
the precise claim is “grab/warp/ungrab verified on WSLg; no peer, no human
input,” and the pairing rows remain implementation-only.

The Windows clipboard probe and PowerShell `Get-Clipboard` both failed with
`ERROR_ACCESS_DENIED` because the interactive session was disconnected. This
is an environmental result, not a pass or product failure, so clipboard stays
implementation-only everywhere. File transfer likewise has loopback tests
only. Native cross-boundary drag-and-drop is under evaluation; the Windows and
macOS hands-on runs in [file-transfer.md](file-transfer.md) are still pending.

The macOS 0.3.0 build has never been run. Its input backend is the pre-existing
implementation plus the escape gesture; the planned capture rework and
multi-display-origin correction were not completed. Linux packages and the
GitHub Actions workflow exist but CI has never run because the changes have not
been pushed.
