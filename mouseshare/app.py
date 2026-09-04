"""The application: everything except the window itself.

Wiring lives in one place because the pieces are only interesting together.
The public methods are the surface called by pywebview's ``js_api``.  Network
readers, discovery, heartbeat workers, input listeners, and the webview thread
all meet here; ``self._lock`` therefore protects peer lifecycle and the state
snapshots derived from it.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from . import config, discovery, monitors, pairing, protocol, session
from .capture import InputCapture
from .clipboard import Clipboard, ClipboardSync
from .inject import Injector
from .layout import Layout
from .network import MessageClient, MessageServer
from .outbox import Outbox
from .session import ClientSession, HostSession
from .state import StateOwner

# How often each peer is told where the cursor is. A mouse reports about a
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
MAX_CLIENTS = 8
log = logging.getLogger("mouseshare")


@dataclass
class _Peer:
    """All mutable protocol and session state owned by one network link."""
    device_id: str
    name: str
    link: Any
    phase: str = "idle"
    nonce: str = ""
    pending: Optional[pairing.PendingPairing] = None
    monitors: list = field(default_factory=list)
    caps: frozenset[str] = field(default_factory=frozenset)
    version: Optional[int] = None
    outbox: Optional[Outbox] = None
    inbound: Any = None
    heartbeat_stop: Optional[threading.Event] = None
    last_received: float = field(default_factory=time.monotonic)
    role: str = ""
    secret: bytes = b""
    unauthenticated: bool = False
    tearing_down: bool = False


class App:
    _ALLOWED = {
        "idle": {"pair_request"},
        "challenged": {"pair_proof", "auth", "pair_err"},
        "offered": {"pair_challenge", "pair_err"},
        "proved": {"pair_ok", "pair_err"},
        "session": {"layout", "enter", "pos", "click", "scroll", "key",
                    "leave", "ping", "pong", "edge", "clip", "clip_chunk"},
    }

    def __init__(self, deliver, cfg_path=None) -> None:
        self.cfg = config.load_or_create(cfg_path)
        self._cfg_path = config.default_path() if cfg_path is None else cfg_path
        self._lock = threading.RLock()
        self.monitors = monitors.enumerate_local(self.cfg.device_id)

        # Session fields first: the initial snapshot renders the layout,
        # which reads them.
        self._server: Optional[MessageServer] = None
        self._advertiser: Optional[discovery.Advertiser] = None
        self._browser: Optional[discovery.Browser] = None
        self._found: dict = {}
        self._peers: dict[str, _Peer] = {}
        self._handshakes: list[_Peer] = []
        self._known_monitors: dict[str, list] = {}
        # The server accepts one inbound socket at a time; remember which
        # per-peer record owns it so refused links cannot disturb sessions.
        self._inbound_peer: Optional[_Peer] = None
        self._host: Optional[HostSession] = None
        self._client_session: Optional[ClientSession] = None
        self._capture: Optional[InputCapture] = None
        self._clipboard_backend = Clipboard.create()
        self._clipboard: Optional[ClipboardSync] = None
        self._clipboard_notice_token = 0
        if self._clipboard_backend is not None:
            self._clipboard = ClipboardSync(
                self._clipboard_backend, self.cfg.device_id, self.cfg.name,
                self._publish_clipboard, self._clipboard_notice,
                self._clipboard_active)
            self._clipboard.set_enabled(self.cfg.share_clipboard)
        # Temporary; see _injection_cost.
        self._inj_since = time.monotonic()
        self._inj_n = self._inj_pos = 0
        self._inj_total = self._inj_max = self._inj_pos_total = 0.0
        self._injector: Optional[Injector] = None
        self.state = StateOwner(deliver, initial={
            "screen": "devices",
            "device": {"id": self.cfg.device_id, "name": self.cfg.name,
                       "port": self.cfg.port},
            "peers": [], "pairing": None, "session": None,
            "layout": self._layout_view(None), "error": "",
            # Something worth saying that is not something going wrong.
            "notice": "",
            "settings": {"escape_key": self.cfg.escape_key,
                         "share_clipboard": self.cfg.share_clipboard},
        })

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        notice = self._listen()
        if self._server is None:
            self.state.set(error=notice)
            return
        # Advertise the port actually bound, not the one we asked for --
        # they differ whenever the preferred port was unavailable.
        self._advertiser = discovery.Advertiser(
            self.cfg.device_id, self.cfg.name, self._server.port)
        self._browser = discovery.Browser(self.cfg.device_id, self._on_peers)
        try:
            self._advertiser.start()
            self._browser.start()
        except OSError as exc:
            notice = notice or f"Network discovery unavailable: {exc}"
        self.state.set(notice=notice, device={"id": self.cfg.device_id,
            "name": self.cfg.name, "port": self._server.port})
        self._publish_peers()

    def _listen(self) -> str:
        """Bind the preferred listener port, falling back to any free port."""
        for port, fallback in ((self.cfg.port, False), (0, True)):
            try:
                self._server = MessageServer("0.0.0.0", port,
                    self._on_inbound_message, self._on_inbound_disconnect)
            except OSError as exc:
                if not fallback:
                    continue
                return f"Could not open a network port: {exc}"
            self._server.start()
            return (f"Port {self.cfg.port} was unavailable, so MouseShare is "
                    f"listening on {self._server.port} instead.") if fallback else ""
        return "Could not open a network port."

    def stop(self) -> None:
        """Release input before stopping the process-level services."""
        with self._lock:
            peers = list(self._handshakes) + list(self._peers.values())
        for peer in peers:
            self._teardown_peer(peer, "shutdown", False)
        if self._clipboard:
            self._clipboard.stop()
        self._release_input()
        for part in (self._server, self._browser, self._advertiser):
            try:
                if part is not None:
                    part.stop()
            except Exception:  # noqa: BLE001
                pass

    def _release_input(self) -> None:
        if self._host:
            self._host.stop()
        if self._capture:
            self._capture.stop()
        if self._injector:
            self._injector.release_all()
        self._host = self._client_session = self._capture = self._injector = None

    # -- the surface the page calls -----------------------------------------

    def ready(self) -> dict:
        return self.state.mark_ready()

    def show(self, screen: str) -> dict:
        self.state.set(screen=screen, error="")
        return self.state.snapshot()

    def connect(self, peer_id: str) -> dict:
        """Start a session against a discovered peer. We initiated, so we
        become the host: this machine's keyboard and mouse do the driving."""
        found, saved = self._found.get(peer_id), self.cfg.peers.get(peer_id)
        address = found.address if found else (saved.last_address if saved else "")
        port = found.port if found else (saved.last_port if saved else 0)
        if not address:
            self.state.set(error="No address for that machine yet. Connect to it once "
                                 "by typing its address.")
            return self.state.snapshot()
        return self._connect_to(peer_id, address, port or self.cfg.port)

    def connect_manually(self, address: str, port: int = 0) -> dict:
        """Firewalls block mDNS often enough that typing an address has to
        stay possible."""
        return self._connect_to("", address.strip(), int(port or self.cfg.port))

    def _role(self) -> Optional[str]:
        """Return the role of an established session; handshakes do not count."""
        with self._lock:
            roles = [p.role for p in self._peers.values() if p.phase == "session"]
            return roles[0] if roles else None

    def _connect_to(self, peer_id: str, address: str, port: int) -> dict:
        if self._role() == "client":
            host = next((p.name or p.device_id for p in self._peers.values()), "host")
            self.state.set(error=f"Disconnect from {host} first")
            return self.state.snapshot()
        with self._lock:
            if sum(p.role == "host" and p.phase == "session"
                   for p in self._peers.values()) >= MAX_CLIENTS:
                self.state.set(error="This machine already has the maximum "
                                     "number of clients.")
                return self.state.snapshot()
            if peer_id in self._peers:
                self.state.set(error="Already connected to that machine.")
                return self.state.snapshot()
        try:
            link = MessageClient(address, port)
            link.connect(timeout=5)
        except OSError as exc:
            self.state.set(error=f"Could not reach {address}: {exc}")
            return self.state.snapshot()
        peer = _Peer(peer_id, "", link, phase="offered", role="host")
        peer.inbound = link._link._inbound
        with self._lock:
            self._handshakes.append(peer)
        link.start_reader(lambda msg: self._on_message(peer, msg),
                          lambda why: self._on_disconnect(peer, why))
        link.send(protocol.pair_request(self.cfg.device_id, self.cfg.name))
        self.state.set(screen="pairing", pairing={"role": "connector", "code": "",
            "remaining": 0, "peer": peer_id}, error="")
        return self.state.snapshot()

    def submit_code(self, code: str) -> dict:
        """The connecting machine sends its proof of the displayed code."""
        with self._lock:
            peer = next((p for p in reversed(self._handshakes)
                         if p.role == "host" and p.nonce), None)
        code = "".join(c for c in code if c.isdigit())
        if peer is None or len(code) != 6:
            self.state.set(error="Enter the six digits shown on the other machine.")
            return self.state.snapshot()
        with self._lock:
            peer.secret = code.encode()
            proof = pairing.proof(peer.secret, peer.nonce,
                                  self.cfg.device_id, peer.device_id)
            peer.phase = "proved"
        self._send_peer(peer, protocol.pair_proof(proof))
        return self.state.snapshot()

    def disconnect(self, device_id: Optional[str] = None) -> dict:
        """Disconnect one peer, or every peer when no device is specified."""
        with self._lock:
            if device_id:
                targets = ([self._peers[device_id]] if device_id in self._peers else [])
                targets += [p for p in self._handshakes if p.device_id == device_id]
            else:
                targets = list(self._peers.values()) + list(self._handshakes)
        for peer in targets:
            self._teardown_peer(peer, "cancelled", False)
        self._publish_session("", "devices" if not self._peers else None)
        return self.state.snapshot()

    def cancel(self) -> dict:
        return self.disconnect()

    def rename(self, name: str) -> dict:
        self.cfg.name = name.strip() or self.cfg.name
        if self._clipboard:
            self._clipboard.device_name = self.cfg.name
        config.save(self.cfg, self._cfg_path)
        self.state.set(device={"id": self.cfg.device_id, "name": self.cfg.name,
                               "port": self.cfg.port})
        return self.state.snapshot()

    def set_escape_key(self, value: str) -> dict:
        if value not in config.ESCAPE_KEYS:
            raise ValueError("escape key must be ctrl, cmd, alt, or shift")
        self.cfg.escape_key = value
        config.save(self.cfg, self._cfg_path)
        if self._capture:
            self._capture.set_escape_key(value)
        self.state.set(settings={"escape_key": value,
            "share_clipboard": self.cfg.share_clipboard})
        self._publish_session()
        return self.state.snapshot()

    def set_share_clipboard(self, enabled: bool) -> dict:
        self.cfg.share_clipboard = bool(enabled)
        config.save(self.cfg, self._cfg_path)
        if self._clipboard:
            self._clipboard.set_enabled(self.cfg.share_clipboard)
        self.state.set(settings={"escape_key": self.cfg.escape_key,
            "share_clipboard": self.cfg.share_clipboard})
        self._broadcast(protocol.layout(monitors.to_wire(self.monitors),
                                        caps=self._capabilities()))
        return self.state.snapshot()

    def forget(self, peer_id: str) -> dict:
        self.cfg.peers.pop(peer_id, None)
        self.cfg.offsets.pop(peer_id, None)
        self._known_monitors.pop(peer_id, None)
        config.save(self.cfg, self._cfg_path)
        self._publish_peers()
        return self.state.snapshot()

    def set_offset(self, device_id: str, x: int, y: int) -> dict:
        """Snap a device to the layout and reject overlapping screens."""
        layout = self._build_layout()
        if layout:
            previous = self.cfg.offsets.get(device_id, (0, 0))
            layout.set_offset(device_id, (int(x), int(y)))
            layout.snap_device(device_id)
            if not layout.can_place(device_id, layout.offsets[device_id]):
                layout.set_offset(device_id, previous)
                self.state.set(error="Those screens would overlap, so the move was undone.",
                               layout=self._layout_view(layout))
                return self.state.snapshot()
            self.cfg.offsets[device_id] = layout.offsets[device_id]
            if self._host:
                self._host.layout.set_offset(device_id, layout.offsets[device_id])
        else:
            self.cfg.offsets[device_id] = (int(x), int(y))
        config.save(self.cfg, self._cfg_path)
        self.state.set(error="", layout=self._layout_view(layout))
        return self.state.snapshot()

    def permissions(self) -> dict:
        """macOS gates input behind two separate switches.

        They are granted independently and mean different things --
        Accessibility lets us inject, Input Monitoring lets us read -- so
        reporting one number would tell the user the app is broken without
        telling them which switch to flip.
        """
        import sys
        if sys.platform.startswith("linux"):
            from . import linux

            kind = linux.session_type()
            if kind == "x11":
                return {"needed": False, "items": []}
            why = ("MouseShare needs an X11 session on Linux "
                   "(log out and choose 'Ubuntu on Xorg')"
                   if kind == "wayland" else
                   "MouseShare needs a graphical X11 display on Linux")
            return {"needed": True, "items": [{
                "key": "session", "label": "X11 session", "why": why,
                "granted": False,
            }]}
        if sys.platform != "darwin":
            return {"needed": False, "items": []}
        try:
            from ApplicationServices import AXIsProcessTrusted
            accessibility = bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            accessibility = False
        try:
            import Quartz
            input_monitoring = bool(Quartz.CGPreflightListenEventAccess())
        except Exception:  # noqa: BLE001
            input_monitoring = True
        return {"needed": True, "items": [
            {"key": "accessibility", "label": "Accessibility",
             "why": "lets MouseShare move the cursor and type here",
             "granted": accessibility},
            {"key": "input", "label": "Input Monitoring",
             "why": "lets MouseShare read your keyboard and mouse",
             "granted": input_monitoring},
        ]}

    def open_permissions(self, key: str) -> Union[bool, str]:
        """Open the System Settings pane for one permission."""
        import subprocess
        import sys
        if sys.platform.startswith("linux"):
            from . import linux

            if linux.session_type() == "wayland":
                return ("MouseShare needs an X11 session on Linux "
                        "(log out and choose 'Ubuntu on Xorg')")
            return "MouseShare needs a graphical X11 display on Linux"
        pane = {"accessibility": "Privacy_Accessibility",
                "input": "Privacy_ListenEvent"}.get(key)
        if sys.platform != "darwin" or pane is None:
            return False
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:"
                              "com.apple.preference.security?" + pane])
            return True
        except OSError:
            return False

    # -- discovery -----------------------------------------------------------

    def _on_peers(self, peers: dict) -> None:
        with self._lock:
            self._found = peers
            self._publish_peers()

    def _publish_peers(self) -> None:
        with self._lock:
            self._publish_peers_locked()

    def _publish_peers_locked(self) -> None:
        """Publish discovered and remembered peers while holding the app lock."""
        seen = {i: {"device_id": i, "name": p.name or i[:8],
                    "address": p.address, "port": p.port, "online": True}
                for i, p in self._found.items()}
        for i, p in self.cfg.peers.items():
            seen.setdefault(i, {"device_id": i, "name": p.name or i[:8],
                "address": p.last_address, "port": p.last_port or self.cfg.port,
                "online": False})
        for i, item in seen.items():
            item.update(paired=i in self.cfg.peers,
                        connected=i in self._peers and self._peers[i].phase == "session",
                        reachable=bool(item["address"]))
            # Separate from `online` on purpose. Being heard from and being
            # reachable are different facts: one blocked multicast packet
            # ends the first without touching the second, and gating the
            # button on the wrong one strands the user with no way back in.
        # Paired first, then online, then by name -- the machine you use is
        # the one you want at the top.
        self.state.set(peers=sorted(seen.values(), key=lambda p: (
            not p["paired"], not p["online"], p["name"].lower())))

    def _session_view(self) -> Optional[dict]:
        """Build the UI's aggregate view of all established peer sessions."""
        peers = [p for p in self._peers.values() if p.phase == "session"]
        if not peers:
            return None
        role = peers[0].role
        return {"role": role, "remote": bool(self._host and self._host.remote),
                "active_peer": (self._host.peer_id if self._host and self._host.remote
                                else peers[0].device_id if role == "client" else None),
                "clients": [p.device_id for p in peers if p.role == "host"],
                "escape_key": self.cfg.escape_key,
                "unauthenticated_peer": any(p.unauthenticated for p in peers)}

    def _publish_session(
        self, error: Optional[str] = None, screen: Optional[str] = None
    ) -> None:
        """Publish session, layout, and peer state from one locked snapshot."""
        with self._lock:
            changes = {"session": self._session_view(), "pairing": None,
                       "layout": self._layout_view(self._build_layout())}
            if error is not None:
                changes["error"] = error
            if screen is not None:
                changes["screen"] = screen
            self.state.set(**changes)
            self._publish_peers_locked()

    # -- connecting ----------------------------------------------------------

    def _on_inbound_message(self, msg: dict) -> None:
        """Attach an inbound server link to its per-peer record and dispatch it."""
        with self._lock:
            if self._inbound_peer is None:
                self._inbound_peer = _Peer("", "", self._server, role="client")
                if self._server._link is not None:
                    self._inbound_peer.inbound = self._server._link._inbound
                self._handshakes.append(self._inbound_peer)
            peer = self._inbound_peer
        self._on_message(peer, msg)

    def _on_inbound_disconnect(self, reason: str) -> None:
        """Route a server disconnect only to the peer that owns that link."""
        with self._lock:
            peer = self._inbound_peer
        if peer:
            self._on_disconnect(peer, reason)

    # -- protocol ------------------------------------------------------------

    def _on_message(self, peer: _Peer, msg: dict) -> None:
        with self._lock:
            version = msg.get("v")
            kind = msg.get("t")
            if peer.version is None:
                advertised = (msg.get("max_v") or version
                              if peer.role == "client" and kind == "pair_request"
                              else version)
                if (not isinstance(advertised, int)
                        or isinstance(advertised, bool)
                        or advertised < protocol.MIN_VERSION):
                    raise protocol.ProtocolError("invalid maximum version")
                peer.version = min(protocol.VERSION, advertised)
            elif version != peer.version:
                raise protocol.ProtocolError("protocol version changed mid-stream")
            peer.last_received = time.monotonic()
            # The phase belongs to this peer's socket, not to the app. An
            # unauthenticated inbound socket must not be able to spend an
            # outbound peer's legitimate permission to send pair_ok.
            if kind not in self._ALLOWED.get(peer.phase, set()):
                if kind in protocol.OPTIONAL_TYPES:
                    return
                invalid = True
            else:
                invalid = False
        if invalid:
            # Without this, an unsolicited pair_ok would start a session on
            # a socket that never proved anything, and the whole handshake
            # would be decoration.
            self._teardown_peer(peer, "unexpected message")
            return
        try:
            self._dispatch(peer, msg)
        except pairing.PairingFailed as exc:
            self._teardown_peer(peer, str(exc))
        except protocol.ProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A message we cannot make sense of means the stream is no
            # longer trustworthy. Staying connected around it is how input
            # gets left suppressed or held down.
            log.exception("message handling failed")
            self._teardown_peer(peer, "bad message")
            self.state.set(error=f"Connection dropped: {exc}")

    def _dispatch(self, peer: _Peer, msg: dict) -> None:
        kind = msg["t"]
        handlers = {"pair_request": self._begin_target_pairing,
                    "pair_challenge": self._on_challenge,
                    "pair_proof": self._on_proof, "auth": self._on_auth,
                    "pair_ok": self._on_pair_ok, "pair_err": self._on_pair_err}
        if kind in handlers:
            handlers[kind](peer, msg)
        elif kind == "layout":
            self._set_caps(peer, msg)
            peer.monitors = monitors.from_wire(peer.device_id, msg["monitors"])
            self._known_monitors[peer.device_id] = peer.monitors
            if self._host:
                layout = self._build_layout()
                if layout is not None:
                    self._host.update_layout(layout)
            self._start_heartbeat(peer)
            self._publish_session()
        elif kind == "ping" and "heartbeat" in peer.caps:
            self._queue(peer, protocol.pong(msg["seq"]))
        elif kind in {"clip", "clip_chunk"}:
            self._on_clipboard(peer, msg)
        elif kind not in {"pong", "edge"} and self._client_session:
            self._inject(msg)

    def _refuse(self, peer: _Peer, reason: str) -> None:
        """Hang up on one peer without disturbing any live session."""
        self._send_peer(peer, protocol.pair_err(reason))
        self._teardown_peer(peer, reason)

    def _begin_target_pairing(self, peer: _Peer, msg: dict) -> None:
        """We were connected to, so we are the client: show the code."""
        identity = msg.get("device_id", "")
        with self._lock:
            duplicate = (not identity or identity == self.cfg.device_id or
                         identity in self._peers)
            outbound = next((p for p in self._handshakes if p is not peer and
                             p.role == "host" and p.device_id == identity), None)
            busy = bool(self._peers or any(
                p is not peer for p in self._handshakes))
        if duplicate:
            return self._refuse(peer, "duplicate")
        if outbound:
            if session.pick_winner(self.cfg.device_id, identity) == self.cfg.device_id:
                return self._refuse(peer, "busy")
            self._teardown_peer(outbound, "simultaneous connect", False)
        elif busy:
            return self._refuse(peer, "busy")
        with self._lock:
            # Refuse capacity before showing a pairing screen or challenge.
            if sum(p.role == "host" and p.phase == "session"
                   for p in self._peers.values()) >= MAX_CLIENTS:
                full = True
            else:
                full = False
                peer.device_id = identity
                peer.name = msg.get("name", "")
                peer.phase = "challenged"
                saved = self.cfg.peers.get(identity)
                if saved and saved.token:
                    peer.nonce = pairing.make_nonce()
                else:
                    peer.pending = pairing.PendingPairing(self.cfg.device_id, identity)
                    peer.nonce = peer.pending.nonce
        if full:
            return self._refuse(peer, "full")
        if peer.pending is not None:
            self.state.set(screen="pairing", pairing={"role": "target",
                "code": peer.pending.code, "remaining": int(peer.pending.remaining()),
                "peer": peer.name}, error="")
            threading.Thread(target=self._tick_code, args=(peer, peer.pending),
                             daemon=True).start()
        self._send_peer(peer, protocol.pair_challenge(peer.nonce, self.cfg.device_id))

    def _tick_code(self, peer: _Peer, pending: pairing.PendingPairing) -> None:
        """Count down only the pairing object this worker was created for."""
        while True:
            with self._lock:
                if peer.pending is not pending or pending.remaining() <= 0:
                    break
            time.sleep(1)
            with self._lock:
                # Re-check inside the lock: the reader thread clears the
                # pending pairing the instant the code is accepted, and acting
                # on a stale read would update or tear down a newer pairing.
                if peer.pending is not pending:
                    return
        with self._lock:
            expired = peer.pending is pending
        if expired:
            self._teardown_peer(peer, "code expired")

    def _on_challenge(self, peer: _Peer, msg: dict) -> None:
        """We are connecting. If we already have a token, prove it and skip
        the code entirely."""
        with self._lock:
            full = sum(p.role == "host" and p.phase == "session"
                       for p in self._peers.values()) >= MAX_CLIENTS
            peer.nonce = msg["nonce"]
            # A manual connect reaches an address, not an identity; the
            # challenge is where we learn who answered.
            identity = msg.get("device_id") or peer.device_id
            duplicate = identity == self.cfg.device_id or identity in self._peers
            peer.device_id = identity
            saved = self.cfg.peers.get(identity)
            authenticated = bool(saved and saved.token)
            if authenticated:
                peer.secret = bytes.fromhex(saved.token)
                proof = pairing.proof(peer.secret, peer.nonce,
                                      self.cfg.device_id, identity)
                peer.phase = "proved"
        if full:
            return self._refuse(peer, "full")
        if duplicate:
            return self._refuse(peer, "duplicate")
        if authenticated:
            self._send_peer(peer, protocol.auth(self.cfg.device_id, proof))
        else:
            self.state.set(pairing={"role": "connector", "code": "",
                                    "remaining": 0, "peer": identity})

    def _on_proof(self, peer: _Peer, msg: dict) -> None:
        if peer.pending is None:
            return self._send_peer(peer, protocol.pair_err("not pairing"))
        if not peer.pending.check(msg.get("hmac", "")):
            return self._send_peer(peer, protocol.pair_err("wrong code"))
        secret, token = peer.pending.code.encode(), pairing.make_token()
        self.cfg.peers[peer.device_id] = config.Peer(peer.name, token)
        config.save(self.cfg, self._cfg_path)
        peer.pending = None
        self._send_peer(peer, protocol.pair_ok(self.cfg.name,
            monitors.to_wire(self.monitors), token, caps=self._capabilities(),
            hmac=pairing.ok_proof(
                secret, peer.nonce, self.cfg.device_id, peer.device_id)))
        self._become_client(peer)

    def _on_auth(self, peer: _Peer, msg: dict) -> None:
        identity = msg.get("device_id", "")
        with self._lock:
            duplicate = (identity != peer.device_id or
                         (identity in self._peers and self._peers[identity] is not peer))
        if duplicate:
            return self._refuse(peer, "duplicate")
        saved = self.cfg.peers.get(identity)
        if not saved or not saved.token:
            return self._refuse(peer, "unknown device")
        secret = bytes.fromhex(saved.token)
        if not pairing.verify(secret, msg.get("hmac", ""), peer.nonce,
                              identity, self.cfg.device_id):
            return self._refuse(peer, "authentication failed")
        self._send_peer(peer, protocol.pair_ok(self.cfg.name,
            monitors.to_wire(self.monitors), caps=self._capabilities(),
            hmac=pairing.ok_proof(
                secret, peer.nonce, self.cfg.device_id, identity)))
        self._become_client(peer)

    def _on_pair_ok(self, peer: _Peer, msg: dict) -> None:
        """We are the host. Store the token, take the peer's monitors, and
        start driving."""
        mac = msg.get("hmac", "")
        if peer.version == 2 and not mac:
            peer.unauthenticated = True
            log.warning("peer %s does not authenticate pair_ok (protocol v2)",
                        msg.get("name") or peer.device_id)
        elif not isinstance(mac, str) or not pairing.verify_ok(
                peer.secret, mac, peer.nonce, peer.device_id, self.cfg.device_id):
            self._teardown_peer(peer, "pair_ok not authenticated")
            return
        peer.name = msg.get("name", "")
        peer.monitors = monitors.from_wire(peer.device_id, msg["monitors"])
        self._known_monitors[peer.device_id] = peer.monitors
        self._set_caps(peer, msg)
        saved = self.cfg.peers.get(peer.device_id)
        address, port = getattr(peer.link, "_addr", ("", 0))
        # Keep what we had when this session gave us nothing better: being
        # dialled into leaves no outbound socket to read an address from,
        # and overwriting with nothing would throw away the only way back
        # to a machine whose discovery is blocked.
        self.cfg.peers[peer.device_id] = config.Peer(peer.name,
            msg.get("token", "") or (saved.token if saved else ""),
            address or (saved.last_address if saved else ""),
            port or (saved.last_port if saved else 0))
        self.cfg.offsets.setdefault(self.cfg.device_id, (0, 0))
        self.cfg.offsets.setdefault(peer.device_id, self._default_offset())
        config.save(self.cfg, self._cfg_path)
        self._become_host(peer)

    def _on_pair_err(self, peer: _Peer, msg: dict) -> None:
        reason = msg.get("reason", "Pairing failed.")
        if reason == "wrong code":
            # Retryable: keep the link and the code entry on screen so the
            # user can simply type it again.
            self.state.set(error="That code was not right. Try again.")
        else:
            self._teardown_peer(peer, reason)

    def _promote(self, peer: _Peer) -> bool:
        """Atomically make one authenticated link the winner for an identity."""
        with self._lock:
            winner = self._peers.get(peer.device_id)
            if winner is not None and winner is not peer:
                duplicate = True
            else:
                duplicate = False
                self._peers[peer.device_id] = peer
                if peer in self._handshakes:
                    self._handshakes.remove(peer)
        if duplicate:
            self._refuse(peer, "duplicate")
            return False
        return True

    def _become_host(self, peer: _Peer) -> None:
        if not self._promote(peer):
            return
        with self._lock:
            peer.phase = "session"
        # The input hook must not wait on a socket; see outbox.py.
        peer.outbox = Outbox(lambda msg: self._send_peer(peer, msg),
            lambda exc: self._on_disconnect(peer, f"send failed: {exc}"),
            POSITION_INTERVAL)
        layout = self._build_layout()
        if self._host is None:
            self._injector = Injector.create()
            self._host = HostSession(layout=layout, local_id=self.cfg.device_id,
                capture=None, injector=self._injector, send=self._send,
                peers={p.device_id: p.monitors for p in self._peers.values()},
                on_remote_change=self._host_remote_changed)
            self._capture = InputCapture(on_move=self._host.on_move,
                on_delta=self._host.on_delta, on_click=self._host.on_click,
                on_scroll=self._host.on_scroll, on_key=self._host.on_key,
                on_escape=self._host.on_escape,
                on_capture_lost=self._host.on_capture_lost,
                escape_key=self.cfg.escape_key)
            self._host.capture = self._capture
            self._capture.start()
        else:
            self._host.add_peer(peer.device_id, peer.monitors)
            self._host.update_layout(layout)
        peer.outbox.put(protocol.layout(monitors.to_wire(self.monitors),
                                        caps=self._capabilities()))
        self._start_heartbeat(peer)
        self._publish_session("", "layout")

    def _become_client(self, peer: _Peer) -> None:
        if not self._promote(peer):
            return
        with self._lock:
            peer.phase = "session"
        self._injector = Injector.create()
        peer.outbox = Outbox(lambda msg: self._send_peer(peer, msg),
            lambda exc: self._on_disconnect(peer, f"send failed: {exc}"))
        self._client_session = ClientSession(self._injector, peer.outbox.put)
        self._publish_session("", "layout")

    def _set_caps(self, peer: _Peer, msg: dict) -> None:
        """Record the optional protocol capabilities advertised by one peer."""
        caps = msg.get("caps", [])
        peer.caps = frozenset(x for x in caps if isinstance(x, str)) \
            if isinstance(caps, list) else frozenset()

    def _capabilities(self) -> list[str]:
        caps = list(protocol.CAPABILITIES)
        if self.cfg.share_clipboard and self._clipboard is not None:
            caps.append("clipboard")
        return caps

    def _clipboard_active(self) -> bool:
        with self._lock:
            return any(p.phase == "session" and "clipboard" in p.caps
                       for p in self._peers.values())

    def _publish_clipboard(self, msg: dict, skip: str | None = None) -> None:
        with self._lock:
            peer_ids = [p.device_id for p in self._peers.values()
                        if p.phase == "session" and "clipboard" in p.caps
                        and p.device_id != skip]
        for peer_id in peer_ids:
            self._send(peer_id, msg)

    def _clipboard_notice(self, notice: str) -> None:
        with self._lock:
            self._clipboard_notice_token += 1
            token = self._clipboard_notice_token
        self.state.set(notice=notice)
        timer = threading.Timer(3, self._clear_clipboard_notice, args=(token,))
        timer.daemon = True
        timer.start()

    def _clear_clipboard_notice(self, token: int) -> None:
        with self._lock:
            if token != self._clipboard_notice_token:
                return
        self.state.set(notice="")

    def _on_clipboard(self, peer: _Peer, msg: dict) -> None:
        if (not self.cfg.share_clipboard or self._clipboard is None
                or "clipboard" not in peer.caps):
            return
        source_id = msg.get("device_id")
        source_name = msg.get("name") if isinstance(msg.get("name"), str) else ""
        source_name = source_name or self.cfg.peers.get(
            source_id, config.Peer(str(source_id), "")).name
        accepted = self._clipboard.receive(peer.device_id, source_name, msg)
        if accepted is not None and peer.role == "host":
            self._publish_clipboard(accepted, peer.device_id)

    def _send_peer(self, peer: _Peer, msg: dict) -> None:
        """Send on one peer's link, adapting extensions to its wire version."""
        if peer.version and peer.version < 3:
            msg = {k: v for k, v in msg.items() if k != "caps" and not (
                k == "hmac" and msg.get("t") == "pair_ok")}
        peer.link.send(msg)

    def _queue(self, peer: _Peer, msg: dict) -> None:
        """Use a peer's outbox when running, otherwise send immediately."""
        peer.outbox.put(msg) if peer.outbox else self._send_peer(peer, msg)

    def _send(self, peer_id: str, msg: dict) -> None:
        """Answer only on the established link owned by the named peer."""
        with self._lock:
            peer = self._peers.get(peer_id)
        if peer and peer.phase == "session":
            self._queue(peer, msg)

    def _broadcast(self, msg: dict) -> None:
        """Send one protocol message to every established peer."""
        with self._lock:
            peer_ids = list(self._peers)
        for peer_id in peer_ids:
            self._send(peer_id, msg)

    @property
    def negotiated_version(self) -> int:
        with self._lock:
            return min((p.version or protocol.VERSION for p in self._peers.values()),
                       default=protocol.VERSION)

    def _start_heartbeat(self, peer: _Peer) -> None:
        with self._lock:
            if (peer.phase != "session" or "heartbeat" not in peer.caps or
                    peer.heartbeat_stop):
                return
            peer.heartbeat_stop = threading.Event()
            peer.last_received = time.monotonic()
            stop = peer.heartbeat_stop
        threading.Thread(target=self._heartbeat_loop,
                         args=(peer, stop), name="mouseshare-heartbeat",
                         daemon=True).start()

    def _heartbeat_loop(self, peer: _Peer, stop: threading.Event) -> None:
        seq = 0
        try:
            while not stop.wait(HEARTBEAT_INTERVAL):
                if time.monotonic() - peer.last_received > HEARTBEAT_TIMEOUT:
                    with self._lock:
                        if (stop.is_set() or peer.heartbeat_stop is not stop or
                                self._peers.get(peer.device_id) is not peer):
                            return
                    self._teardown_peer(peer, "heartbeat")
                    return
                self._queue(peer, protocol.ping(seq))
                seq += 1
        except Exception:  # noqa: BLE001 - a recovery worker must not escape
            log.exception("heartbeat worker failed")
            with self._lock:
                if (stop.is_set() or peer.heartbeat_stop is not stop or
                        self._peers.get(peer.device_id) is not peer):
                    return
            self._teardown_peer(peer, "heartbeat")

    # -- roles ---------------------------------------------------------------

    def _default_offset(
        self,
        peer_monitors: Optional[dict[str, list]] = None,
        offsets: Optional[dict[str, tuple[int, int]]] = None,
    ) -> tuple[int, int]:
        peer_monitors = peer_monitors if peer_monitors is not None else {
            p.device_id: p.monitors for p in self._peers.values() if p.monitors}
        offsets = offsets if offsets is not None else self.cfg.offsets
        # Only devices that already have a place count, otherwise a peer
        # still waiting for its default would sit at x=0 and shift the box.
        placed = [(self.cfg.device_id, self.monitors)] + [
            (device_id, mons) for device_id, mons in peer_monitors.items()
            if device_id in offsets]
        right = 0
        for device_id, device_monitors in placed:
            offset_x = offsets.get(device_id, (0, 0))[0]
            origin_x = min((m.x for m in device_monitors), default=0)
            right = max(right, max((offset_x + m.x - origin_x + m.w
                                    for m in device_monitors), default=right))
        return right or 1920, 0

    def _build_layout(self) -> Optional[Layout]:
        peer_monitors = {device_id: self._known_monitors.get(device_id, [])
                         for device_id in self.cfg.peers}
        peer_monitors.update({p.device_id: p.monitors for p in self._peers.values()
                              if p.monitors})
        if not any(peer_monitors.values()):
            return None
        mons = self.monitors + [m for known in peer_monitors.values() for m in known]
        offsets = {self.cfg.device_id: self.cfg.offsets.get(self.cfg.device_id, (0, 0))}
        # Saved arrangements first, so every default lands to the right of
        # everything that already has a place, including offline peers.
        for device_id in peer_monitors:
            if device_id in self.cfg.offsets:
                offsets[device_id] = self.cfg.offsets[device_id]
        for device_id in peer_monitors:
            if device_id not in offsets:
                offsets[device_id] = self._default_offset(peer_monitors, offsets)
        return Layout(mons, offsets)

    def _layout_view(self, layout: Optional[Layout]) -> dict:
        def block(identity: str, name: str, mons: list, connected: bool) -> dict:
            off = (layout.offsets.get(identity, (0, 0)) if layout else
                   self.cfg.offsets.get(identity, (0, 0)))
            return {"device_id": identity, "name": name, "offset": list(off),
                "connected": connected, "draggable": bool(mons),
                "monitors": [{"id": m.id, "w": m.w, "h": m.h,
                    "primary": m.primary,
                    "x": layout.plane_rect(identity, m.id)[0] if layout else off[0] + m.x,
                    "y": layout.plane_rect(identity, m.id)[1] if layout else off[1] + m.y}
                    for m in mons]}
        devices = [block(self.cfg.device_id, self.cfg.name, self.monitors, True)]
        for device_id, saved in self.cfg.peers.items():
            peer = self._peers.get(device_id)
            known = peer.monitors if peer and peer.monitors else self._known_monitors.get(
                device_id, [])
            devices.append(block(device_id, peer.name if peer and peer.name else
                                 saved.name or "Peer", known,
                                 bool(peer and peer.phase == "session")))
        return {"devices": devices}

    def _host_remote_changed(self, remote: bool) -> None:
        with self._lock:
            self.state.set_async(session=self._session_view())

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
        except Exception:  # noqa: BLE001 - the network session is still healthy
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
        if now - self._inj_since < 5:
            return
        log.debug(
            "injection: %d events / %.1fs, mean %.0f us, max %.0f us; "
            "%d were pos, mean %.0f us",
            self._inj_n, now - self._inj_since,
            self._inj_total / max(self._inj_n, 1), self._inj_max,
            self._inj_pos, self._inj_pos_total / max(self._inj_pos, 1),
        )
        self._inj_since, self._inj_n = now, 0
        self._inj_total = self._inj_max = 0.0
        self._inj_pos = self._inj_pos_total = 0

    # -- teardown ------------------------------------------------------------

    def _on_disconnect(self, peer: _Peer, reason: str) -> None:
        # A link we refused going away is not another peer's session ending.
        self._teardown_peer(peer, reason)

    def _teardown_peer(
        self, peer: _Peer, reason: str, publish: bool = True
    ) -> None:
        """Retire one link without ever joining its reader under the app lock.

        The object-identity check matters when a refused reconnect presents a
        device ID already owned by a live session: only the owning peer may
        remove that mapping or trigger last-peer input release.
        """
        with self._lock:
            if peer.tearing_down:
                return
            peer.tearing_down = True
            if peer.heartbeat_stop:
                peer.heartbeat_stop.set()
                peer.heartbeat_stop = None
            owns_session = self._peers.get(peer.device_id) is peer
            if owns_session:
                del self._peers[peer.device_id]
            if peer in self._handshakes:
                self._handshakes.remove(peer)
            if self._inbound_peer is peer:
                self._inbound_peer = None
            if self._clipboard:
                self._clipboard.discard_peer(peer.device_id)

        # A reader may already be waiting for the app lock. Stop and join it
        # without owning that lock, then no input can arrive after release.
        try:
            peer.link.stop_inbound()
        except (AttributeError, RuntimeError):
            pass
        if peer.outbox:
            peer.outbox.stop()
        try:
            peer.link.disconnect(reason) if peer.link is self._server else peer.link.close()
        except (AttributeError, OSError):
            pass

        with self._lock:
            if owns_session and peer.role == "host" and self._host:
                self._host.on_peer_lost(peer.device_id)
                if self._peers:
                    layout = self._build_layout()
                    if layout is not None:
                        self._host.update_layout(layout)
            elif owns_session and peer.role == "client" and self._client_session:
                self._client_session.on_disconnect(reason)
            peer.outbox = None
            peer.phase = "idle"
            # Never clear tearing_down: stale disconnect and heartbeat callbacks
            # for this retired object must remain harmless forever.
            if owns_session and not self._peers:
                self._release_input()
            if publish:
                quiet = reason in {"cancelled", "shutdown", "simultaneous connect"}
                self._publish_session("" if quiet else f"Disconnected ({reason}).",
                                      "devices" if not self._peers else None)
