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

from . import config, discovery, monitors, pairing, protocol
from .capture import InputCapture
from .inject import Injector
from .layout import Layout
from .network import MessageClient, MessageServer
from .session import ClientSession, HostSession
from .state import StateOwner

log = logging.getLogger("mouseshare")


class App:
    def __init__(self, deliver, cfg_path=config.DEFAULT_PATH):
        self.cfg = config.load(cfg_path)
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
        self._nonce = ""
        self._role = ""
        self._host: Optional[HostSession] = None
        self._client_session: Optional[ClientSession] = None
        self._capture: Optional[InputCapture] = None
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
        })

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._server = MessageServer(
            "0.0.0.0", self.cfg.port, self._on_message, self._on_disconnect
        )
        self._server.start()
        self._advertiser = discovery.Advertiser(
            self.cfg.device_id, self.cfg.name, self.cfg.port
        )
        self._browser = discovery.Browser(self.cfg.device_id, self._on_peers)
        try:
            self._advertiser.start()
            self._browser.start()
        except OSError as exc:
            self.state.set(error=f"Network discovery unavailable: {exc}")
        self._publish_peers()

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
            address, port = saved.last_address, self.cfg.port
        if not address:
            self.state.set(error="That machine is no longer on the network.")
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
        self._send(protocol.pair_proof(mac))
        return self.state.snapshot()

    def cancel(self) -> dict:
        self._teardown("cancelled")
        return self.state.snapshot()

    def set_offset(self, device_id: str, x: int, y: int) -> dict:
        self.cfg.offsets[device_id] = (int(x), int(y))
        config.save(self.cfg, self._cfg_path)
        if self._host is not None:
            self._host.layout.set_offset(device_id, (int(x), int(y)))
        self.state.set(layout=self._layout_view(self._build_layout()))
        return self.state.snapshot()

    def snap(self, device_id: str, anchor_id: str) -> dict:
        layout = self._build_layout()
        if layout is None:
            return self.state.snapshot()
        layout.snap_device(device_id, anchor_id)
        return self.set_offset(device_id, *layout.offsets[device_id])

    def rename(self, name: str) -> dict:
        self.cfg.name = name.strip() or self.cfg.name
        config.save(self.cfg, self._cfg_path)
        self.state.set(device={
            "id": self.cfg.device_id, "name": self.cfg.name, "port": self.cfg.port
        })
        return self.state.snapshot()

    def forget(self, peer_id: str) -> dict:
        self.cfg.peers.pop(peer_id, None)
        self.cfg.offsets.pop(peer_id, None)
        config.save(self.cfg, self._cfg_path)
        self._publish_peers()
        return self.state.snapshot()

    def permissions(self) -> dict:
        """macOS gates input capture behind Accessibility. Report it rather
        than letting the app look silently broken."""
        import sys

        if sys.platform != "darwin":
            return {"platform": sys.platform, "needed": False, "trusted": True}
        try:
            from ApplicationServices import AXIsProcessTrusted

            trusted = bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            trusted = False
        return {"platform": "darwin", "needed": True, "trusted": trusted}

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
                    "port": self.cfg.port,
                    "online": False,
                }
        for device_id, entry in seen.items():
            entry["paired"] = device_id in self.cfg.peers
            entry["connected"] = device_id == self._peer_id and self._role != ""
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
        self._peer_id = peer_id
        self._role = "host"
        client.start_reader(self._on_message, self._on_disconnect)
        client.send(protocol.pair_request(self.cfg.device_id, self.cfg.name))
        self.state.set(
            screen="pairing",
            pairing={"role": "connector", "code": "", "remaining": 0, "peer": peer_id},
            error="",
        )
        return self.state.snapshot()

    def _send(self, msg: dict) -> None:
        link = self._client if self._role == "host" and self._client else self._server
        if link is not None:
            link.send(msg)

    # -- protocol ------------------------------------------------------------

    def _on_message(self, msg: dict) -> None:
        try:
            self._dispatch(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("message handling failed")
            self.state.set(error=str(exc))

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
            self.state.set(error=msg.get("reason", "Pairing failed."), pairing=None)
        elif t == "layout":
            self._peer_monitors = monitors.from_wire(self._peer_id, msg["monitors"])
            self.state.set(layout=self._layout_view(self._build_layout()))
        elif self._client_session is not None:
            self._client_session.on_message(msg)

    def _begin_target_pairing(self, msg: dict) -> None:
        """We were connected to, so we are the client: show the code."""
        self._peer_id = msg["device_id"]
        self._peer_name = msg.get("name", "")
        self._role = "client"
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
        threading.Thread(target=self._tick_code, daemon=True).start()

    def _tick_code(self) -> None:
        while self._pending is not None and self._pending.remaining() > 0:
            time.sleep(1.0)
            snapshot = self.state.snapshot().get("pairing")
            if not snapshot or snapshot.get("role") != "target":
                return
            self.state.set(pairing={
                **snapshot, "remaining": int(self._pending.remaining())
            })
        if self._pending is not None:
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
            mac = pairing.proof(
                bytes.fromhex(saved.token), self._nonce,
                self.cfg.device_id, self._peer_id,
            )
            self._send(protocol.auth(self.cfg.device_id, mac))
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
        token = pairing.make_token()
        self.cfg.peers[self._peer_id] = config.Peer(
            name=self._peer_name, token=token
        )
        config.save(self.cfg, self._cfg_path)
        self._pending = None
        self._send(protocol.pair_ok(
            self.cfg.name, monitors.to_wire(self.monitors), token
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
        self._send(protocol.pair_ok(self.cfg.name, monitors.to_wire(self.monitors)))
        self._become_client()

    def _on_pair_ok(self, msg: dict) -> None:
        """We are the host. Store the token, take the peer's monitors, and
        start driving."""
        self._peer_name = msg.get("name", "")
        self._peer_monitors = monitors.from_wire(self._peer_id, msg["monitors"])
        token = msg.get("token", "")
        saved = self.cfg.peers.get(self._peer_id)
        self.cfg.peers[self._peer_id] = config.Peer(
            name=self._peer_name,
            token=token or (saved.token if saved else ""),
            last_address=self._client._addr[0] if self._client else "",
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
        layout = self._build_layout()
        self._injector = Injector.create()
        self._host = HostSession(
            layout=layout,
            local_id=self.cfg.device_id,
            peer_id=self._peer_id,
            capture=None,
            injector=self._injector,
            send=self._send,
        )
        self._capture = InputCapture(
            on_move=self._host.on_move,
            on_delta=self._host.on_delta,
            on_click=self._host.on_click,
            on_scroll=self._host.on_scroll,
            on_key=self._host.on_key,
        )
        self._host.capture = self._capture
        self._capture.start()
        self._send(protocol.layout(monitors.to_wire(self.monitors)))
        self.state.set(
            screen="layout",
            pairing=None,
            session={
                "peer_id": self._peer_id, "peer_name": self._peer_name,
                "role": "host",
            },
            layout=self._layout_view(layout),
            error="",
        )
        self._publish_peers()

    def _become_client(self) -> None:
        self._injector = Injector.create()
        self._client_session = ClientSession(self._injector)
        self.state.set(
            screen="layout",
            pairing=None,
            session={
                "peer_id": self._peer_id, "peer_name": self._peer_name,
                "role": "client",
            },
            error="",
        )
        self._publish_peers()

    # -- teardown ------------------------------------------------------------

    def _on_disconnect(self, reason: str) -> None:
        log.info("link closed: %s", reason)
        self._teardown(reason)

    def _teardown(self, reason: str) -> None:
        """Input first, always."""
        with self._lock:
            if self._host is not None:
                self._host.on_disconnect(reason)
            if self._client_session is not None:
                self._client_session.on_disconnect(reason)
            if self._capture is not None:
                self._capture.stop()
            if self._client is not None:
                self._client.close()
            self._host = self._client_session = self._capture = None
            self._client = self._injector = None
            self._role = ""
            self._pending = None
            self._nonce = ""
            self._peer_monitors = []
        self.state.set(
            session=None,
            pairing=None,
            screen="devices",
            error="" if reason in ("cancelled", "reconnecting") else f"Disconnected ({reason}).",
        )
        self._publish_peers()
