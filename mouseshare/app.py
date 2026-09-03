"""The application: everything except the window itself.

Wiring, in one place, because the pieces are only interesting together.
The methods without a leading underscore are the surface the page calls
through `pywebview`'s js_api -- there is no separate facade, since it would
have been a pass-through of exactly these names.

Threading, which the rest of the design depends on: the webview owns the
main thread, and nothing here touches it. Discovery, the socket reader and
the input listeners run on their own threads and mutate `self.state`, which
serialises them and pushes one snapshot at the UI.
"""
import logging
import threading
import time
from typing import Optional

from . import config, discovery, monitors, pairing, protocol, session
from .capture import InputCapture
from .inject import Injector
from .layout import Layout
from .network import MessageClient, MessageServer
from .outbox import Outbox
from .session import ClientSession, HostSession
from .state import StateOwner

# How often the peer is told where the cursor is. A mouse reports about a
# thousand times a second and a screen redraws sixty; everything between
# those two numbers is work the far end does for nothing, and it does not
# have it to spare. Measured on a Mac mini: at a thousand messages a
# second the receiving process spent so long parsing them that injection
# was starved down to eighty a second, and the surplus piled up in its
# socket buffer -- the one queue in this system that cannot collapse --
# so the cursor carried on moving after the hand had stopped.
POSITION_INTERVAL = 1 / 120
HEARTBEAT_INTERVAL = 2.0
HEARTBEAT_TIMEOUT = 6.0

log = logging.getLogger("mouseshare")


class App:
    def __init__(self, deliver, cfg_path=config.DEFAULT_PATH):
        self.cfg = config.load_or_create(cfg_path)
        self._cfg_path = cfg_path
        self._lock = threading.RLock()

        self.monitors = monitors.enumerate_local(self.cfg.device_id)

        # Session fields first: the initial snapshot renders the layout,
        # which reads them.
        self._server: Optional[MessageServer] = None
        self._client: Optional[MessageClient] = None
        self._advertiser: Optional[discovery.Advertiser] = None
        self._browser: Optional[discovery.Browser] = None
        self._found = {}
        self._pending: Optional[pairing.PendingPairing] = None
        self._peer_id = ""
        self._peer_name = ""
        self._peer_monitors = []
        self._peer_caps = frozenset()
        self._negotiated_version = protocol.VERSION
        self._last_received = time.monotonic()
        self._heartbeat_stop = None
        self._nonce = ""
        self._pair_secret = b""
        self._unauthenticated_peer = False
        self._role = ""
        self._phase = "idle"
        # Which link owns the handshake: "in", "out", or nobody yet.
        self._active: Optional[str] = None
        self._tearing_down = False
        self._host: Optional[HostSession] = None
        self._client_session: Optional[ClientSession] = None
        self._capture: Optional[InputCapture] = None
        self._outbox: Optional[Outbox] = None
        # Temporary; see _injection_cost.
        self._inj_since = time.monotonic()
        self._inj_n = self._inj_pos = 0
        self._inj_total = self._inj_max = self._inj_pos_total = 0.0
        self._injector: Optional[Injector] = None

        self.state = StateOwner(deliver, initial={
            "screen": "devices",
            "device": {
                "id": self.cfg.device_id,
                "name": self.cfg.name,
                "port": self.cfg.port,
            },
            "peers": [],
            "pairing": None,
            "session": None,
            "layout": self._layout_view(None),
            "error": "",
            # Something worth saying that is not something going wrong.
            "notice": "",
            "settings": {"escape_key": self.cfg.escape_key},
        })

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        notice = self._listen()
        if self._server is None:
            self.state.set(error=notice, notice="")
            return

        # Advertise the port actually bound, not the one we asked for --
        # they differ whenever the preferred port was unavailable.
        self._advertiser = discovery.Advertiser(
            self.cfg.device_id, self.cfg.name, self._server.port
        )
        self._browser = discovery.Browser(self.cfg.device_id, self._on_peers)
        try:
            self._advertiser.start()
            self._browser.start()
        except OSError as exc:
            notice = notice or f"Network discovery unavailable: {exc}"
        self.state.set(
            notice=notice,
            device={
                "id": self.cfg.device_id,
                "name": self.cfg.name,
                "port": self._server.port,
            },
        )
        self._publish_peers()

    def _listen(self) -> str:
        """Bind the listener, falling back to any free port.

        Windows reserves blocks of ports for Hyper-V and WSL, and a machine
        running either can refuse the default outright (WinError 10013).
        Peers learn the real port from the mDNS advertisement, so falling
        back costs nothing except a line in the UI for anyone who has to
        type an address by hand.
        """
        for port, fallback in ((self.cfg.port, False), (0, True)):
            try:
                self._server = MessageServer(
                    "0.0.0.0", port,
                    lambda m: self._on_message(m, "in"),
                    lambda r: self._on_disconnect(r, "in"),
                )
            except OSError as exc:
                if not fallback:
                    log.warning("port %d unavailable (%s); trying any", port, exc)
                    continue
                log.error("could not open a listening port: %s", exc)
                return f"Could not open a network port: {exc}"
            self._server.start()
            if fallback:
                return (
                    f"Port {self.cfg.port} was unavailable, so MouseShare is "
                    f"listening on {self._server.port} instead."
                )
            return ""
        return "Could not open a network port."

    def stop(self) -> None:
        """Order matters: input first, so a failure later cannot leave a
        machine suppressing its own keyboard."""
        self._release_input()
        for part in (self._client, self._server, self._browser, self._advertiser):
            try:
                if part is not None:
                    part.stop() if hasattr(part, "stop") else part.close()
            except Exception:  # noqa: BLE001
                pass

    def _release_input(self) -> None:
        if self._host is not None:
            self._host.stop()
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._injector is not None:
            self._injector.release_all()

    # -- the surface the page calls -----------------------------------------

    def ready(self) -> dict:
        return self.state.mark_ready()

    def show(self, screen: str) -> dict:
        self.state.set(screen=screen, error="")
        return self.state.snapshot()

    def connect(self, peer_id: str) -> dict:
        """Start a session against a discovered peer. We initiated, so we
        become the host: this machine's keyboard and mouse do the driving."""
        peer = self._found.get(peer_id)
        address = peer.address if peer else ""
        port = peer.port if peer else self.cfg.port
        saved = self.cfg.peers.get(peer_id)
        if not address and saved:
            address, port = saved.last_address, saved.last_port or self.cfg.port
        if not address:
            self.state.set(error="No address for that machine yet. "
                                 "Connect to it once by typing its address.")
            return self.state.snapshot()
        return self._connect_to(peer_id, address, port)

    def connect_manually(self, address: str, port: int = 0) -> dict:
        """Firewalls block mDNS often enough that typing an address has to
        stay possible."""
        return self._connect_to("", address.strip(), int(port or self.cfg.port))

    def submit_code(self, code: str) -> dict:
        """The connecting machine sends its proof of the displayed code."""
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) != 6 or not self._nonce:
            self.state.set(error="Enter the six digits shown on the other machine.")
            return self.state.snapshot()
        mac = pairing.proof(
            code.encode("ascii"), self._nonce, self.cfg.device_id, self._peer_id
        )
        self._pair_secret = code.encode("ascii")
        self._send(protocol.pair_proof(mac))
        self._phase = "proved"
        return self.state.snapshot()

    def cancel(self) -> dict:
        self._teardown("cancelled")
        return self.state.snapshot()

    def set_offset(self, device_id: str, x: int, y: int) -> dict:
        """Drop a device at a position: snap it flush, check it does not
        overlap, and only then keep it.

        One call, not a set-then-snap pair. Overlapping screens make cursor
        ownership ambiguous, so a half-applied drop would route input to the
        wrong machine until the second call landed -- or forever, if it
        never did.
        """
        layout = self._build_layout()
        if layout is None:
            self.cfg.offsets[device_id] = (int(x), int(y))
            config.save(self.cfg, self._cfg_path)
            self.state.set(layout=self._layout_view(None))
            return self.state.snapshot()

        previous = self.cfg.offsets.get(device_id, (0, 0))
        layout.set_offset(device_id, (int(x), int(y)))
        anchor = next(
            (d for d in layout.device_ids() if d != device_id), None
        )
        if anchor is not None:
            layout.snap_device(device_id, anchor)
        placed = layout.offsets[device_id]
        if not layout.can_place(device_id, placed):
            layout.set_offset(device_id, previous)
            self.state.set(
                error="Those screens would overlap, so the move was undone.",
                layout=self._layout_view(layout),
            )
            return self.state.snapshot()

        self.cfg.offsets[device_id] = placed
        config.save(self.cfg, self._cfg_path)
        if self._host is not None:
            self._host.layout.set_offset(device_id, placed)
        self.state.set(error="", layout=self._layout_view(layout))
        return self.state.snapshot()

    def rename(self, name: str) -> dict:
        self.cfg.name = name.strip() or self.cfg.name
        config.save(self.cfg, self._cfg_path)
        self.state.set(device={
            "id": self.cfg.device_id, "name": self.cfg.name, "port": self.cfg.port
        })
        return self.state.snapshot()

    def set_escape_key(self, value: str) -> dict:
        if value not in config.ESCAPE_KEYS:
            raise ValueError("escape key must be ctrl, cmd, alt, or shift")
        self.cfg.escape_key = value
        config.save(self.cfg, self._cfg_path)
        if self._capture is not None:
            self._capture.set_escape_key(value)
        changes = {"settings": {"escape_key": value}}
        current = self.state.snapshot().get("session")
        if current and current.get("role") == "host":
            changes["session"] = {**current, "escape_key": value}
        self.state.set(**changes)
        return self.state.snapshot()

    def forget(self, peer_id: str) -> dict:
        self.cfg.peers.pop(peer_id, None)
        self.cfg.offsets.pop(peer_id, None)
        config.save(self.cfg, self._cfg_path)
        self._publish_peers()
        return self.state.snapshot()

    def permissions(self) -> dict:
        """macOS gates input behind two separate switches.

        They are granted independently and mean different things --
        Accessibility lets us inject, Input Monitoring lets us read -- so
        reporting one number would tell the user the app is broken without
        telling them which switch to flip.
        """
        import sys

        if sys.platform != "darwin":
            return {"needed": False, "items": []}

        def accessibility() -> bool:
            try:
                from ApplicationServices import AXIsProcessTrusted

                return bool(AXIsProcessTrusted())
            except Exception:  # noqa: BLE001
                return False

        def input_monitoring() -> bool:
            try:
                import Quartz

                return bool(Quartz.CGPreflightListenEventAccess())
            except Exception:  # noqa: BLE001
                # Older pyobjc has no preflight call. Claiming "denied"
                # would nag forever, so treat unknown as granted and let
                # the real failure speak for itself.
                return True

        return {
            "needed": True,
            "items": [
                {
                    "key": "accessibility",
                    "label": "Accessibility",
                    "why": "lets MouseShare move the cursor and type here",
                    "granted": accessibility(),
                },
                {
                    "key": "input",
                    "label": "Input Monitoring",
                    "why": "lets MouseShare read your keyboard and mouse",
                    "granted": input_monitoring(),
                },
            ],
        }

    def open_permissions(self, key: str) -> bool:
        """Open the System Settings pane for one permission."""
        import subprocess
        import sys

        if sys.platform != "darwin":
            return False
        pane = {
            "accessibility": "Privacy_Accessibility",
            "input": "Privacy_ListenEvent",
        }.get(key)
        if pane is None:
            return False
        try:
            subprocess.Popen(
                ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"]
            )
            return True
        except OSError as exc:
            log.warning("could not open System Settings: %s", exc)
            return False

    # -- discovery -----------------------------------------------------------

    def _on_peers(self, peers) -> None:
        self._found = peers
        self._publish_peers()

    def _publish_peers(self) -> None:
        seen = {}
        for device_id, peer in self._found.items():
            seen[device_id] = {
                "device_id": device_id,
                "name": peer.name or device_id[:8],
                "address": peer.address,
                "port": peer.port,
                "online": True,
            }
        for device_id, saved in self.cfg.peers.items():
            if device_id not in seen:
                seen[device_id] = {
                    "device_id": device_id,
                    "name": saved.name or device_id[:8],
                    "address": saved.last_address,
                    "port": saved.last_port or self.cfg.port,
                    "online": False,
                }
        for device_id, entry in seen.items():
            entry["paired"] = device_id in self.cfg.peers
            entry["connected"] = device_id == self._peer_id and self._role != ""
            # Separate from `online` on purpose. Being heard from and being
            # reachable are different facts: one blocked multicast packet
            # ends the first without touching the second, and gating the
            # button on the wrong one strands the user with no way back in.
            entry["reachable"] = bool(entry["address"])
        # Paired first, then online, then by name -- the machine you use is
        # the one you want at the top.
        self.state.set(peers=sorted(
            seen.values(),
            key=lambda p: (not p["paired"], not p["online"], p["name"].lower()),
        ))

    # -- connecting ----------------------------------------------------------

    def _connect_to(self, peer_id: str, address: str, port: int) -> dict:
        self._teardown("reconnecting")
        try:
            client = MessageClient(address, port)
            client.connect(timeout=5.0)
        except OSError as exc:
            self.state.set(error=f"Could not reach {address}: {exc}")
            return self.state.snapshot()

        self._client = client
        self._active = "out"
        self._peer_id = peer_id
        self._role = "host"
        self._phase = "offered"
        self._pair_secret = b""
        self._unauthenticated_peer = False
        client.start_reader(
            lambda m: self._on_message(m, "out"),
            lambda r: self._on_disconnect(r, "out"),
        )
        client.send(protocol.pair_request(self.cfg.device_id, self.cfg.name))
        self.state.set(
            screen="pairing",
            pairing={"role": "connector", "code": "", "remaining": 0, "peer": peer_id},
            error="",
        )
        return self.state.snapshot()

    def _send(self, msg: dict) -> None:
        """Answer on the link that owns the handshake, never the other one."""
        link = self._client if self._active == "out" else self._server
        if link is not None:
            if self._negotiated_version < 3 and "caps" in msg:
                msg = {key: value for key, value in msg.items() if key != "caps"}
            if (
                self._negotiated_version < 3
                and msg.get("t") == "pair_ok"
                and "hmac" in msg
            ):
                msg = {key: value for key, value in msg.items() if key != "hmac"}
            link.send(msg)

    @property
    def negotiated_version(self) -> int:
        return self._negotiated_version

    def _refuse(self, source: str, reason: str) -> None:
        """Hang up on one link without disturbing the live one."""
        log.info("refusing %s link: %s", source, reason)
        if source == "out" and self._client is not None:
            self._client.close()
        elif source == "in" and self._server is not None:
            self._server.disconnect(reason)

    def _drop_outbound(self) -> None:
        """Give up our own outbound attempt in favour of an inbound one.

        `_active` moves first: closing the client fires a disconnect, and
        that must be recognised as the losing link's, not the session's.
        """
        self._active = "in"
        if self._client is not None:
            self._client.close()
            self._client = None
        self._role = ""
        self._phase = "idle"
        self._nonce = ""
        self._pair_secret = b""
        self._unauthenticated_peer = False

    # -- protocol ------------------------------------------------------------

    # Which messages are legal in which handshake phase. Anything else is
    # a peer that is confused or lying, and either way the link goes.
    #
    #   idle       -> a fresh inbound link, nothing agreed yet
    #   challenged -> we asked for a code and are waiting to be proved to
    #   offered    -> we connected out and are waiting for the challenge
    #   proved     -> we answered the challenge, waiting to be let in
    #   session    -> authenticated; input may flow
    _ALLOWED = {
        "idle": {"pair_request"},
        "challenged": {"pair_proof", "auth"},
        "offered": {"pair_challenge", "pair_err"},
        "proved": {"pair_ok", "pair_err"},
        "session": {
            "layout", "enter", "pos", "click", "scroll", "key", "leave",
            "ping", "pong", "edge",
        },
    }

    def _on_message(self, msg: dict, source: str = "in") -> None:
        t = msg.get("t")
        with self._lock:
            if self._active is None and source == "in":
                self._active = "in"  # a fresh caller claims the handshake
            if source != self._active:
                # The phase belongs to a socket, not to this app. While we
                # are dialling out, our phase legitimately accepts pair_ok
                # -- and an unauthenticated inbound socket must not be able
                # to spend that on our behalf.
                if t == "pair_request" and self._active == "out":
                    self._arbitrate(msg)
                    return
                self._refuse(source, f"{t!r} on a link that owns nothing")
                return

            version = msg.get("v")
            if isinstance(version, int):
                self._negotiated_version = min(protocol.VERSION, version)
            self._last_received = time.monotonic()

        if t not in self._ALLOWED.get(self._phase, set()):
            if t in protocol.OPTIONAL_TYPES:
                log.debug("ignoring optional message type %s", t)
                return
            # Without this, an unsolicited pair_ok would start a session on
            # a socket that never proved anything, and the whole handshake
            # would be decoration.
            log.warning("unexpected %r in phase %r; dropping link", t, self._phase)
            self._teardown("unexpected message")
            return
        try:
            self._dispatch(msg)
        except pairing.PairingFailed as exc:
            self.state.set(error=str(exc))
            self._teardown(str(exc))
        except Exception as exc:  # noqa: BLE001
            # A message we cannot make sense of means the stream is no
            # longer trustworthy. Staying connected around it is how input
            # gets left suppressed or held down.
            log.exception("message handling failed")
            self.state.set(error=f"Connection dropped: {exc}")
            self._teardown("bad message")

    def _arbitrate(self, msg: dict) -> None:
        """Both machines pressed Connect at the same moment.

        Each side runs the same comparison on the same two ids and reaches
        the same answer, so exactly one link survives -- decided before
        either machine starts suppressing input.
        """
        incoming = msg.get("device_id", "")
        if not incoming or incoming == self.cfg.device_id:
            self._refuse("in", "missing or self device id")
            return
        if session.pick_winner(self.cfg.device_id, incoming) == self.cfg.device_id:
            log.info("simultaneous connect: keeping our outbound link")
            self._refuse("in", "we won the tie-break")
            return
        log.info("simultaneous connect: yielding to %s", incoming)
        self._drop_outbound()
        self._begin_target_pairing(msg)

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "pair_request":
            self._begin_target_pairing(msg)
        elif t == "pair_challenge":
            self._on_challenge(msg)
        elif t == "pair_proof":
            self._on_proof(msg)
        elif t == "auth":
            self._on_auth(msg)
        elif t == "pair_ok":
            self._on_pair_ok(msg)
        elif t == "pair_err":
            self._on_pair_err(msg)
        elif t == "layout":
            self._set_peer_caps(msg)
            self._peer_monitors = monitors.from_wire(self._peer_id, msg["monitors"])
            self.state.set(layout=self._layout_view(self._build_layout()))
            self._start_heartbeat()
        elif t == "ping":
            if "heartbeat" in self._peer_caps:
                reply = protocol.pong(msg["seq"])
                if self._outbox is not None:
                    self._outbox.put(reply)
                else:
                    self._send(reply)
        elif t == "pong":
            return
        elif t == "edge":
            return
        elif self._client_session is not None:
            self._inject(msg)

    def _on_pair_err(self, msg: dict) -> None:
        reason = msg.get("reason", "Pairing failed.")
        if reason == "wrong code":
            # Retryable: keep the link and the code entry on screen so the
            # user can simply type it again.
            self.state.set(error="That code was not right. Try again.")
            return
        self.state.set(error=reason)
        self._teardown(reason)

    def _begin_target_pairing(self, msg: dict) -> None:
        """We were connected to, so we are the client: show the code."""
        self._peer_id = msg["device_id"]
        self._peer_name = msg.get("name", "")
        self._role = "client"
        self._phase = "challenged"
        saved = self.cfg.peers.get(self._peer_id)
        self._nonce = pairing.make_nonce()
        if saved and saved.token:
            self._pending = None  # already paired: expect an auth proof
            self._send(protocol.pair_challenge(self._nonce, self.cfg.device_id))
            return
        self._pending = pairing.PendingPairing(self.cfg.device_id, self._peer_id)
        self._nonce = self._pending.nonce
        self.state.set(
            screen="pairing",
            pairing={
                "role": "target",
                "code": self._pending.code,
                "remaining": int(self._pending.remaining()),
                "peer": self._peer_name,
            },
        )
        self._send(protocol.pair_challenge(self._nonce, self.cfg.device_id))
        threading.Thread(
            target=self._tick_code, args=(self._pending,), daemon=True
        ).start()

    def _tick_code(self, mine) -> None:
        """Count down one specific pairing.

        The worker holds the pending object it was started for and checks
        identity every time. Following the shared attribute instead would
        let an obsolete timer adopt a newer pairing and tear down a session
        that had nothing to do with it.
        """
        while True:
            with self._lock:
                if self._pending is not mine or mine.remaining() <= 0:
                    break
            time.sleep(1.0)
            with self._lock:
                # Re-check inside the lock: the reader thread clears
                # _pending the instant the code is accepted, and acting on
                # a stale read would update or tear down a newer pairing.
                if self._pending is not mine:
                    return
                snapshot = self.state.snapshot().get("pairing")
                if not snapshot or snapshot.get("role") != "target":
                    return
                remaining = int(mine.remaining())
            self.state.set(pairing={**snapshot, "remaining": remaining})
        with self._lock:
            expired = self._pending is mine
        if expired:
            self._teardown("code expired")

    def _on_challenge(self, msg: dict) -> None:
        """We are connecting. If we already have a token, prove it and skip
        the code entirely."""
        self._nonce = msg["nonce"]
        # A manual connect reaches an address, not an identity; the
        # challenge is where we learn who answered.
        self._peer_id = msg.get("device_id") or self._peer_id
        saved = self.cfg.peers.get(self._peer_id)
        if saved and saved.token:
            self._pair_secret = bytes.fromhex(saved.token)
            mac = pairing.proof(
                self._pair_secret, self._nonce,
                self.cfg.device_id, self._peer_id,
            )
            self._send(protocol.auth(self.cfg.device_id, mac))
            self._phase = "proved"
            return
        self.state.set(pairing={
            "role": "connector", "code": "", "remaining": 0, "peer": self._peer_id
        })

    def _on_proof(self, msg: dict) -> None:
        if self._pending is None:
            self._send(protocol.pair_err("not pairing"))
            return
        try:
            ok = self._pending.check(msg.get("hmac", ""))
        except pairing.PairingFailed as exc:
            self._send(protocol.pair_err(str(exc)))
            self._teardown(str(exc))
            return
        if not ok:
            self._send(protocol.pair_err("wrong code"))
            return
        secret = self._pending.code.encode("ascii")
        token = pairing.make_token()
        self.cfg.peers[self._peer_id] = config.Peer(
            name=self._peer_name, token=token
        )
        config.save(self.cfg, self._cfg_path)
        self._pending = None
        self._send(protocol.pair_ok(
            self.cfg.name,
            monitors.to_wire(self.monitors),
            token,
            hmac=pairing.ok_proof(
                secret, self._nonce, self.cfg.device_id, self._peer_id
            ),
        ))
        self._become_client()

    def _on_auth(self, msg: dict) -> None:
        saved = self.cfg.peers.get(msg.get("device_id", ""))
        if not saved or not saved.token:
            self._send(protocol.pair_err("unknown device"))
            self._teardown("unknown device")
            return
        ok = pairing.verify(
            bytes.fromhex(saved.token), msg.get("hmac", ""),
            self._nonce, msg["device_id"], self.cfg.device_id,
        )
        if not ok:
            self._send(protocol.pair_err("authentication failed"))
            self._teardown("authentication failed")
            return
        self._send(protocol.pair_ok(
            self.cfg.name,
            monitors.to_wire(self.monitors),
            hmac=pairing.ok_proof(
                bytes.fromhex(saved.token), self._nonce,
                self.cfg.device_id, msg["device_id"],
            ),
        ))
        self._become_client()

    def _on_pair_ok(self, msg: dict) -> None:
        """We are the host. Store the token, take the peer's monitors, and
        start driving."""
        peer_name = msg.get("name", "")
        mac = msg.get("hmac", "")
        if self._negotiated_version == 2 and not mac:
            log.warning(
                "peer %s does not authenticate pair_ok (protocol v2)",
                peer_name or self._peer_id,
            )
            self._unauthenticated_peer = True
        elif not isinstance(mac, str) or not pairing.verify_ok(
            self._pair_secret, mac, self._nonce,
            self._peer_id, self.cfg.device_id,
        ):
            self._teardown("pair_ok not authenticated")
            return
        else:
            self._unauthenticated_peer = False
        self._pair_secret = b""

        self._peer_name = peer_name
        self._set_peer_caps(msg)
        self._peer_monitors = monitors.from_wire(self._peer_id, msg["monitors"])
        token = msg.get("token", "")
        saved = self.cfg.peers.get(self._peer_id)
        # Keep what we had when this session gave us nothing better: being
        # dialled into leaves no outbound socket to read an address from,
        # and overwriting with nothing would throw away the only way back
        # to a machine whose discovery is blocked.
        self.cfg.peers[self._peer_id] = config.Peer(
            name=self._peer_name,
            token=token or (saved.token if saved else ""),
            last_address=(
                self._client._addr[0] if self._client
                else (saved.last_address if saved else "")
            ),
            last_port=(
                self._client._addr[1] if self._client
                else (saved.last_port if saved else 0)
            ),
        )
        self.cfg.offsets.setdefault(self.cfg.device_id, (0, 0))
        if self._peer_id not in self.cfg.offsets:
            self.cfg.offsets[self._peer_id] = self._default_offset()
        config.save(self.cfg, self._cfg_path)
        self._become_host()

    # -- roles ---------------------------------------------------------------

    def _default_offset(self) -> tuple:
        width = max((m.x + m.w for m in self.monitors), default=1920)
        left = min((m.x for m in self.monitors), default=0)
        return (width - left, 0)

    def _build_layout(self) -> Optional[Layout]:
        if not self._peer_monitors:
            return None
        offsets = {
            self.cfg.device_id: self.cfg.offsets.get(self.cfg.device_id, (0, 0)),
            self._peer_id: self.cfg.offsets.get(self._peer_id, self._default_offset()),
        }
        return Layout(self.monitors + self._peer_monitors, offsets)

    def _layout_view(self, layout: Optional[Layout]) -> dict:
        def block(device_id, name, mons):
            offset = self.cfg.offsets.get(device_id, (0, 0))
            return {
                "device_id": device_id,
                "name": name,
                "offset": list(offset),
                "monitors": [
                    {
                        "id": m.id, "w": m.w, "h": m.h, "primary": m.primary,
                        "x": (layout.plane_rect(device_id, m.id)[0] if layout
                              else offset[0] + m.x),
                        "y": (layout.plane_rect(device_id, m.id)[1] if layout
                              else offset[1] + m.y),
                    }
                    for m in mons
                ],
            }

        blocks = [block(self.cfg.device_id, self.cfg.name, self.monitors)]
        if self._peer_monitors:
            blocks.append(block(
                self._peer_id, self._peer_name or "Peer", self._peer_monitors
            ))
        return {"devices": blocks}

    def _become_host(self) -> None:
        self._phase = "session"
        layout = self._build_layout()
        self._injector = Injector.create()
        # The input hook must not wait on a socket; see outbox.py.
        self._outbox = Outbox(
            self._send,
            on_error=lambda exc: self._on_disconnect(f"send failed: {exc}"),
            min_pos_interval=POSITION_INTERVAL,
        )
        self._host = HostSession(
            layout=layout,
            local_id=self.cfg.device_id,
            peer_id=self._peer_id,
            capture=None,
            injector=self._injector,
            send=self._outbox.put,
            on_remote_change=self._host_remote_changed,
        )
        self._capture = InputCapture(
            on_move=self._host.on_move,
            on_delta=self._host.on_delta,
            on_click=self._host.on_click,
            on_scroll=self._host.on_scroll,
            on_key=self._host.on_key,
            on_escape=self._host.on_escape,
            on_capture_lost=self._host.on_capture_lost,
            escape_key=self.cfg.escape_key,
        )
        self._host.capture = self._capture
        self._capture.start()
        for m in layout.monitors:
            log.debug(
                "plane rect %s/%s os=(%d,%d,%d,%d) plane=%s",
                m.device_id, m.id, m.x, m.y, m.w, m.h,
                layout.plane_rect(m.device_id, m.id),
            )
        self._send(protocol.layout(monitors.to_wire(self.monitors)))
        self._start_heartbeat()
        self.state.set(
            screen="layout",
            pairing=None,
            session={
                "peer_id": self._peer_id, "peer_name": self._peer_name,
                "role": "host",
                "escape_key": self.cfg.escape_key,
                "remote": False,
                "unauthenticated_peer": self._unauthenticated_peer,
            },
            layout=self._layout_view(layout),
            error="",
        )
        self._publish_peers()

    def _host_remote_changed(self, remote: bool) -> None:
        current = self.state.snapshot().get("session")
        if current and current.get("role") == "host":
            self.state.set_async(session={**current, "remote": remote})

    def _inject(self, msg: dict) -> None:
        """One refused event is not a reason to stop obeying the host.

        The queue stops itself on error, which is right for sending -- a
        failed send means the peer is gone -- but wrong here: the link is
        still healthy, and a dead queue would leave the user's own
        machine ignoring them with no sign of why.
        """
        started = time.perf_counter()
        try:
            self._client_session.on_message(msg)
        except Exception:  # noqa: BLE001 - see above
            log.exception("could not inject %s", msg.get("t"))
        self._injection_cost(msg.get("t"), time.perf_counter() - started)

    def _injection_cost(self, kind: str, seconds: float) -> None:
        """Temporary instrumentation: what does one injection actually cost?

        The queue in front of this holds only the newest position, so if
        the cursor still arrives late the time has to be going either
        inside this call or somewhere past it that we cannot see.
        """
        if not log.isEnabledFor(logging.DEBUG):
            return
        us = seconds * 1e6
        self._inj_n += 1
        self._inj_total += us
        self._inj_max = max(self._inj_max, us)
        if kind == "pos":
            self._inj_pos += 1
            self._inj_pos_total += us
        now = time.monotonic()
        if now - self._inj_since < 5.0:
            return
        log.debug(
            "inject: %d calls in %.1fs, mean %.0f us, worst %.0f us; "
            "%d were pos, mean %.0f us",
            self._inj_n, now - self._inj_since,
            self._inj_total / max(self._inj_n, 1), self._inj_max,
            self._inj_pos, self._inj_pos_total / max(self._inj_pos, 1),
        )
        self._inj_since, self._inj_n, self._inj_total, self._inj_max = now, 0, 0.0, 0.0
        self._inj_pos = self._inj_pos_total = 0

    def _become_client(self) -> None:
        self._phase = "session"
        self._injector = Injector.create()
        self._outbox = Outbox(
            self._send,
            on_error=lambda exc: self._on_disconnect(f"send failed: {exc}"),
        )
        self._client_session = ClientSession(self._injector, self._outbox.put)
        self.state.set(
            screen="layout",
            pairing=None,
            session={
                "peer_id": self._peer_id, "peer_name": self._peer_name,
                "role": "client",
                "unauthenticated_peer": False,
            },
            error="",
        )
        self._publish_peers()

    def _set_peer_caps(self, msg: dict) -> None:
        caps = msg.get("caps", [])
        self._peer_caps = frozenset(cap for cap in caps if isinstance(cap, str)) \
            if isinstance(caps, list) else frozenset()

    def _start_heartbeat(self) -> None:
        with self._lock:
            if (
                self._phase != "session"
                or "heartbeat" not in self._peer_caps
                or self._heartbeat_stop is not None
            ):
                return
            stop = threading.Event()
            self._heartbeat_stop = stop
            self._last_received = time.monotonic()
            threading.Thread(
                target=self._heartbeat_loop,
                args=(stop,),
                name="mouseshare-heartbeat",
                daemon=True,
            ).start()

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        seq = 0
        try:
            while not stop.wait(HEARTBEAT_INTERVAL):
                if time.monotonic() - self._last_received > HEARTBEAT_TIMEOUT:
                    with self._lock:
                        if stop.is_set() or self._heartbeat_stop is not stop:
                            return
                    self._teardown("heartbeat")
                    return
                message = protocol.ping(seq)
                seq += 1
                if self._outbox is not None:
                    self._outbox.put(message)
                else:
                    self._send(message)
        except Exception:  # noqa: BLE001 - recovery worker must never escape
            log.exception("heartbeat worker failed")
            with self._lock:
                if stop.is_set() or self._heartbeat_stop is not stop:
                    return
            self._teardown("heartbeat")

    # -- teardown ------------------------------------------------------------

    def _on_disconnect(self, reason: str, source: str = "in") -> None:
        with self._lock:
            if self._active is not None and source != self._active:
                # A link we refused going away is not the session ending.
                log.info("refused %s link closed (%s)", source, reason)
                return
        log.info("link closed: %s", reason)
        self._teardown(reason)

    def _teardown(self, reason: str) -> None:
        """Input first, always."""
        with self._lock:
            if self._tearing_down:
                return  # closing a link re-enters here; do it once
            self._tearing_down = True

        try:
            self._do_teardown(reason)
        finally:
            # try/finally, because an exception on the way down must not
            # leave the guard stuck True: that would disable every future
            # teardown, and teardown is what releases a suppressed keyboard.
            with self._lock:
                self._tearing_down = False
        self.state.set(
            session=None,
            pairing=None,
            screen="devices",
            error="" if reason in ("cancelled", "reconnecting") else f"Disconnected ({reason}).",
        )
        self._publish_peers()

    def _do_teardown(self, reason: str) -> None:
        with self._lock:
            if self._heartbeat_stop is not None:
                self._heartbeat_stop.set()
                self._heartbeat_stop = None
            client = self._client
            server = self._server

        # A handler may already be waiting for the app lock. Stop and join it
        # without owning that lock, then no input can arrive after release.
        if client is not None:
            client.stop_inbound()
        if server is not None:
            server.stop_inbound()

        with self._lock:
            if self._host is not None:
                self._host.on_disconnect(reason)
            if self._client_session is not None:
                self._client_session.on_disconnect(reason)
            if self._capture is not None:
                self._capture.stop()
            if self._outbox is not None:
                self._outbox.stop()
            # The outbox flushes while the socket is still usable.
            if client is not None:
                client.close()
            if server is not None:
                server.disconnect(reason)
            self._host = self._client_session = self._capture = None
            self._outbox = None
            self._client = self._injector = None
            self._role = ""
            self._phase = "idle"
            self._pending = None
            self._nonce = ""
            self._pair_secret = b""
            self._unauthenticated_peer = False
            self._peer_monitors = []
            self._peer_caps = frozenset()
            self._negotiated_version = protocol.VERSION
            self._active = None
