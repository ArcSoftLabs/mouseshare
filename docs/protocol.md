# MouseShare wire protocol

MouseShare uses one TCP connection per peer. The machine that initiates the
connection is the **host**: it owns the layout and shares its keyboard and
pointer. The responder is the **client**: it injects input. A host can have up
to eight clients; a client cannot simultaneously be a host.

This document describes protocol versions 2 and 3 as implemented by MouseShare
0.3.0. The protocol authenticates peers, but it does **not encrypt** traffic.

## Framing and version negotiation

Each frame is a UTF-8 JSON object followed by `\n`. The sender adds an integer
`v` field to every message. A complete or unterminated frame may contain at
most 65,536 bytes before the newline; malformed JSON, a non-object frame, an
unsupported version, or an oversized frame is fatal to the connection. Empty
lines are ignored.

Supported versions are 2 and 3. A connector cannot know whether the responder
understands v3, so its first frame is always:

```json
{"t":"pair_request","device_id":"…","name":"…","max_v":3,"v":2}
```

The responder selects `min(its maximum, max_v)` and sends its first reply at
that version. If `max_v` is absent, the first frame's `v` is the connector's
maximum. The connector pins the version from the responder's first reply. The
responder pins `min(its maximum, max_v)` when it receives the request. Every
later frame must use that pinned version; changing `v` mid-stream is fatal.

This first-frame rule lets a strict v2 implementation parse the request while
a v3 responder can negotiate up. v3-only fields (`caps`, and the `pair_ok`
HMAC) are removed when sending to a v2 peer.

## Capabilities and optional messages

Capabilities are strings advertised in `pair_ok.caps` and `layout.caps`:

- `heartbeat` enables `ping` and `pong` liveness checks.
- `clipboard` enables text clipboard messages. It is advertised only when
  clipboard sharing is enabled and a platform backend is available.
- `files` enables file-transfer messages. It is advertised only when file
  sharing is enabled.

An extension is used only when the peer advertised its capability. A v2 peer
has no capabilities and receives input only.

The following types are optional extensions: `ping`, `pong`, `edge`, `clip`,
`clip_chunk`, and every `xfer_*` type. An optional message received in a phase
where it is not applicable is ignored, which permits forward compatibility.
An unexpected non-optional type causes teardown. Malformed content in a known
message may also cause teardown or an extension-specific rejection.

## Message catalogue

All messages also carry `v`; it is omitted below for readability. Coordinates
are integer OS coordinates in the receiving device's space. Monitor objects
have `id`, `x`, `y`, `w`, `h`, and `primary` fields.

| Type | Direction | Fields and meaning |
|---|---|---|
| `pair_request` | connector/host → responder/client | `device_id` (connector identity), `name`; first frame also has `max_v`. |
| `pair_challenge` | responder → connector | `nonce` (fresh 16-byte value encoded as 32 hex characters), `device_id` (responder identity). |
| `pair_proof` | connector → responder | `hmac`, proving the displayed code. |
| `auth` | connector → responder | `device_id`, `hmac`, proving a saved token. |
| `pair_ok` | responder → connector | `name`, `monitors`, `caps`; initial pairing also supplies `token`; v3 supplies `hmac` authenticating the responder. |
| `pair_err` | either during handshake | `reason`, a stable short refusal or failure reason. |
| `layout` | host → client after pairing; either side may refresh advertised capabilities | `monitors`, `caps`. It completes host session setup and can refresh capabilities. |
| `enter` | host → active client | `x`, `y`, initial absolute pointer position after crossing. |
| `pos` | host → active client | `x`, `y`, absolute pointer position. Consecutive queued positions may be collapsed. |
| `click` | host → active client | `button` (wire button name), `pressed` (boolean). |
| `scroll` | host → active client | `dx`, `dy`, integer scroll deltas. |
| `key` | host → active client | `kind` is `special` or `char`; `value` is the named key or character; `pressed` is boolean. |
| `leave` | host → former active client | No fields. Releases held input as the cursor leaves or the session is recovered. |
| `ping` | either → peer | `seq`, a connection-local integer sequence. Requires `heartbeat`. |
| `pong` | either → peer | `seq`, copied from the corresponding ping. Requires `heartbeat`. |
| `edge` | client → host | `x`, `y`, a client-reported edge position. Optional input-routing extension. |
| `clip` | either → peer; host may relay between clients | `seq` (non-negative source sequence), `text`, `device_id` (original source), `name` (source display name). Requires `clipboard`. |
| `clip_chunk` | either → peer; host may relay | `id`, `i`, `n`, base64 `data`, plus the same `seq`, `device_id`, and `name` metadata. Requires `clipboard`. |
| `xfer_offer` | sender → recipient | `id`; `files`, a non-empty list of `{name,size,sha256}`. Requires `files`. |
| `xfer_accept` | recipient → sender | `id`. |
| `xfer_reject` | recipient → sender | `id`, `reason`. |
| `xfer_chunk` | sender → recipient | `id`, zero-based `i`, total `n`, base64 `data`. Chunks describe the current file in offer order. |
| `xfer_ack` | recipient → sender | `id`, `i`, acknowledging through that chunk index. |
| `xfer_done` | sender → recipient | `id`, zero-based `file_index`; recipient verifies and installs that file. |
| `xfer_cancel` | either → peer | `id`. Partial receive files are removed. |
| `xfer_error` | either → peer | `id`, `reason`. |

## Pairing and authentication

For a new pair, the responder creates a six-digit code and a fresh nonce. The
connector sends `pair_proof.hmac` as HMAC-SHA-256 keyed by the ASCII six-digit
code over:

```text
nonce|initiator_id|target_id
```

The code is valid for 120 seconds and at most three failed attempts. After a
valid proof, the responder creates a random 32-byte token, stores it locally,
and returns its hex encoding once in `pair_ok.token`. On later connections the
connector sends `auth.hmac`, using the token bytes as the HMAC key over the same
transcript. The code itself and an existing token are never sent.

In v3, `pair_ok.hmac` mutually authenticates the responder. It is
HMAC-SHA-256, keyed by the current secret (the code bytes for a new pair or
token bytes for a saved pair), over:

```text
nonce|target_id|initiator_id|ok
```

The inputs are identifiers and a nonce, not secrets. A missing or invalid v3
`pair_ok.hmac` is fatal. A v2 peer cannot provide this proof; MouseShare accepts
it for compatibility, marks the session `unauthenticated_peer`, logs a warning,
and permits input only.

Known `pair_err.reason` values include:

- `busy`: the responder already has a session or another handshake in
  progress, or loses a simultaneous-connect tie-break.
- `duplicate`: the identity is empty, local, or already connected.
- `full`: the host has reached eight clients.
- `wrong code`: the proof did not match; this is retryable until the attempt
  limit.
- `not pairing`, `unknown device`, and `authentication failed`: handshake state
  or saved credentials did not permit authentication.

Pairing expiry and too many attempts close the connection. `disabled` is not a
pairing refusal: it is an `xfer_reject` reason when receiving files is off.
Other `xfer_reject` reasons are invalid metadata (`files`, `name`, `size`, or
`sha256`) and insufficient `space`; `xfer_error` reasons are `write`, `read`,
`timeout`, `malformed`, `overrun`, `integrity`, and `protocol`.

## Heartbeat

After session setup, peers that both use the `heartbeat` capability send a
`ping` every 2 seconds. Any valid received frame refreshes liveness; `ping`
gets a matching `pong`. If nothing is received for more than 6 seconds, the
link is torn down and captured or held input is released. v2 peers do not
negotiate heartbeat.

## Clipboard chunking and limits

Clipboard sharing is UTF-8 text only. The clipboard is polled every 250 ms.
Payloads up to 32 KiB are normally sent inline as `clip`, provided the encoded
frame remains safely below the 64 KiB frame cap. Larger text is divided into
base64 `clip_chunk` frames with raw chunks of at most 32 KiB. The reassembled
UTF-8 payload cap is 1 MiB. `id`, `i`, and `n` identify one ordered chunk set;
duplicate indexes, inconsistent metadata, invalid base64, or excess data are
discarded. Source sequence and content hashes prevent feedback loops.

## File-transfer limits and integrity

An offer contains 1–200 files. Each file is capped at 4 GiB and its
basename at 255 UTF-8 bytes. Paths, `.`/`..`, control characters, Windows
reserved names, and other unsafe platform names are rejected. Files stream as
base64 frames containing at most 32 KiB raw data. The receiver acknowledges
every four chunks and the final chunk; the sender waits up to 30 seconds for
acceptance and required acknowledgements.

Before accepting, the receiver requires the offered total plus a 64 MiB free
space margin. Data is written to `.part` files in `~/Downloads/MouseShare`,
then size and SHA-256 are checked before an exclusive final install. Name
collisions become `name (2).ext`, and cancellation, disconnection, or failure
removes partial files.

