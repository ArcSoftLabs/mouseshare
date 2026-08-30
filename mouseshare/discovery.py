"""Finding the other machine on the LAN, over mDNS.

Both installs advertise and both browse, so each shows the other in its
device list with nothing typed. IPv4 only -- this is a two-machine home
setup, and carrying a dual-stack address list would buy nothing.

A machine sees its own advertisement, so the browser filters on device id.
Peers disappear on zeroconf's own removal events rather than on a timer.
"""
import socket
from dataclasses import dataclass
from typing import Callable, Dict, Optional

SERVICE = "_mouseshare._tcp.local."


@dataclass(frozen=True)
class Peer:
    device_id: str
    name: str
    address: str
    port: int


class Advertiser:
    """Publishes this machine so the other one can find it."""

    def __init__(self, device_id: str, name: str, port: int):
        self._device_id = device_id
        self._name = name
        self._port = port
        self._zc = None
        self._info = None

    def start(self) -> None:
        from zeroconf import ServiceInfo, Zeroconf

        self._zc = Zeroconf()
        self._info = ServiceInfo(
            SERVICE,
            f"{self._device_id}.{SERVICE}",
            addresses=[socket.inet_aton(_local_address())],
            port=self._port,
            properties={"device_id": self._device_id, "name": self._name},
            server=f"{self._device_id}.local.",
        )
        self._zc.register_service(self._info)

    def stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._info is not None:
                self._zc.unregister_service(self._info)
        finally:
            self._zc.close()
            self._zc = None


class Browser:
    """Watches for peers. Calls `on_change` with the full peer map whenever
    it changes, so the caller renders from one snapshot rather than trying
    to apply a stream of add/remove deltas."""

    def __init__(self, device_id: str, on_change: Callable[[Dict[str, Peer]], None]):
        self._device_id = device_id
        self._on_change = on_change
        self._peers: Dict[str, Peer] = {}
        self._zc = None
        self._browser = None

    def start(self) -> None:
        from zeroconf import ServiceBrowser, Zeroconf

        self._zc = Zeroconf()
        self._browser = ServiceBrowser(self._zc, SERVICE, handlers=[self._on_service])

    def peers(self) -> Dict[str, Peer]:
        return dict(self._peers)

    def _on_service(self, zeroconf, service_type, name, state_change) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            device_id = name.split(".", 1)[0]
            if self._peers.pop(device_id, None) is not None:
                self._on_change(self.peers())
            return

        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        peer = _to_peer(info)
        if peer is None or peer.device_id == self._device_id:
            return  # not usable, or it is us
        if self._peers.get(peer.device_id) != peer:
            self._peers[peer.device_id] = peer
            self._on_change(self.peers())

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zc is not None:
            self._zc.close()
            self._zc = None


def _text(info, key: str) -> str:
    raw = (info.properties or {}).get(key.encode())
    return raw.decode() if isinstance(raw, bytes) else (raw or "")


def _to_peer(info) -> Optional[Peer]:
    if info is None:
        return None
    addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
    ipv4 = [a for a in addresses if ":" not in a]
    device_id = _text(info, "device_id")
    if not ipv4 or not device_id:
        return None
    return Peer(device_id, _text(info, "name"), ipv4[0], info.port)


def _local_address() -> str:
    """The address this machine would use to reach the LAN. No traffic is
    sent -- connecting a UDP socket just picks the route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
