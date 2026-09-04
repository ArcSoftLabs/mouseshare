"""Live compat probe: new code (protocol v3) connecting to an old v2 peer.

Runs headless: input capture, injection and monitor enumeration are faked
exactly as tests/test_app.py does, so nothing on this machine is touched.
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mouseshare import app as app_module  # noqa: E402
from mouseshare import protocol  # noqa: E402
from mouseshare.app import App  # noqa: E402
from mouseshare.layout import Monitor  # noqa: E402

MAC_IP, MAC_PORT = "192.168.129.86", 39471
CFG = HERE / "config.json"


class FakeInjector:
    @classmethod
    def create(cls):
        return cls()

    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeCapture:
    def __init__(self, **kwargs):
        pass

    def __getattr__(self, _name):
        return lambda *a, **k: None


app_module.Injector = FakeInjector
app_module.InputCapture = FakeCapture
app_module.monitors.enumerate_local = lambda device_id: [
    Monitor(device_id, "0", 0, 0, 1920, 1080, primary=True)]

sent, received, disconnects, snapshots = [], [], [], []
_encode = protocol.encode


def encode(msg, version=protocol.VERSION):
    # Control run: `--first-v 2` makes the first (unnegotiated) frame v2,
    # which is what a fixed sender would do. Later frames follow peer_version.
    if "--first-v" in sys.argv and not received:
        version = int(sys.argv[sys.argv.index("--first-v") + 1])
    sent.append((msg.get("t"), version))
    return _encode(msg, version)


protocol.encode = encode

app = App(lambda s: snapshots.append(s), cfg_path=CFG)
_on_message, _on_disconnect = app._on_message, app._on_disconnect


def on_message(peer, msg):
    received.append((msg.get("t"), msg.get("v")))
    return _on_message(peer, msg)


def on_disconnect(peer, reason):
    disconnects.append(reason)
    return _on_disconnect(peer, reason)


app._on_message, app._on_disconnect = on_message, on_disconnect
app.cfg.port = 0
app.ready()
app.start()
print("listening on", app._server.port if app._server else None,
      "error=", app.state.snapshot().get("error"))

app.connect_manually(MAC_IP, MAC_PORT)
hs = app._handshakes[0] if app._handshakes else None

deadline = time.time() + 10
while time.time() < deadline:
    snap = app.state.snapshot()
    if snap.get("session") or snap.get("error"):
        break
    if hs is not None and hs not in app._handshakes and not app._peers:
        break
    time.sleep(0.05)

snap = app.state.snapshot()
session = snap.get("session")
peer = next(iter(app._peers.values()), None)
print("RESULT phase=%s role=%s connected=%s error=%r negotiated_version=%s "
      "peer_version=%s hs_version=%s" % (
          peer.phase if peer else (hs.phase if hs else None),
          session["role"] if session else None,
          bool(session),
          snap.get("error"),
          app.negotiated_version,
          peer.version if peer else None,
          hs.version if hs else None))
print("SENT", sent)
print("RECEIVED", received)
print("DISCONNECTS", disconnects)
print("SESSION", session)
print("SCREEN", snap.get("screen"), "PAIRING", snap.get("pairing"))

app.disconnect()
app.stop()
sys.stdout.flush()
os._exit(0)
