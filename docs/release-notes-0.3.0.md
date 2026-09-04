# MouseShare 0.3.0

MouseShare 0.3.0 expands the original two-machine macOS/Windows input sharing
into a multi-device, three-platform release. One host can now drive up to eight
clients, with a shared layout that routes the cursor directly between peers.

## Highlights since 0.2.0

- Added protocol v3 negotiation, capability discovery, two-way message paths,
  authenticated responder proofs, and heartbeats that release input after a
  dead connection.
- Added multi-device sessions for up to eight clients, duplicate/busy/full
  refusal handling, deterministic corner routing, per-peer teardown, and
  four-device loopback coverage.
- Added the emergency escape gesture: double-tap Ctrl within 0.5 seconds, or
  Cmd on macOS. The modifier is configurable.
- Added bidirectional text clipboard sharing with loop prevention, platform
  backends, chunking, and a 1 MiB limit.
- Added streamed file transfer with progress, cancellation, safe filenames,
  collision renaming, free-space checks, and SHA-256 verification. Files are
  sent through the MouseShare drop zone or **Send files** picker and arrive in
  `~/Downloads/MouseShare`.
- Added first-class Linux X11 capture/injection, XDG configuration and logging,
  Linux install guidance, and build definitions for AppImage, deb, rpm, and
  tarball artifacts. Native Wayland sessions remain unsupported.
- Added Windows and macOS native edge-drop feasibility probes. Native
  cross-boundary drag-and-drop remains under evaluation and is not the shipped
  0.3.0 trigger.
- Added linting, removed tracked Python cache files, and expanded automated
  coverage across protocol security, transport, sessions, clipboard, transfer,
  and platform backends.

## Compatibility

MouseShare 0.3.0 can pair with MouseShare 0.2.0 by opening the connection at
protocol v2 and negotiating the highest shared version. A v0.2.0 peer receives
input only: clipboard, file transfer, and heartbeat require v3 capabilities.
Because v2 does not authenticate the responder's `pair_ok`, 0.3.0 marks and
warns about that peer as unauthenticated.

## Upgrading

Install 0.3.0 over the previous release. Existing configuration and pairings
migrate automatically. On Linux, configuration moves from
`~/.mouseshare/config.json` to
`${XDG_CONFIG_HOME:-~/.config}/mouseshare/config.json`; the legacy file is read
and migrated when the XDG file does not yet exist. Linux debug logs use
`${XDG_STATE_HOME:-~/.local/state}/mouseshare/debug.log`.

Review the [support matrix](support-matrix.md) before relying on a platform
combination: several features have automated coverage but are not yet verified
on real paired devices.

