"""Attacks the handshake from the wire, the way a stray machine would.

Each test connects a raw socket and sends something a well-behaved peer
never would. None of them may end with a session.
"""
import json
import socket
import threading
import time

import pytest

from mouseshare import config, monitors, pairing, protocol
from mouseshare.app import App, _Peer
from mouseshare.layout import Monitor
from mouseshare.network import MessageClient, MessageServer

from .test_app import no_real_input  # noqa: F401 - autouse fixture


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def victim(tmp_path):
    instance = App(lambda s: None, cfg_path=tmp_path / "victim.json")
    instance.cfg.name = "Victim"
    instance.state.mark_ready()
    instance._server = MessageServer(
        "127.0.0.1", 0,
        instance._on_inbound_message, instance._on_inbound_disconnect,
    )
    instance._server.start()
    yield instance
    instance.stop()


def attacker(victim):
    client = MessageClient("127.0.0.1", victim._server.port)
    client.connect()
    received = []
    client.start_reader(received.append)
    return client, received


def forged_pair_ok_server(version, pair_ok_hmac=None):
    peer_id = "attacker"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def send(conn, message):
        conn.sendall(
            json.dumps({**message, "v": version}, separators=(",", ":")).encode()
            + b"\n"
        )

    def answer():
        conn, _ = listener.accept()
        stream = conn.makefile("rb")
        json.loads(stream.readline())
        send(conn, protocol.pair_challenge("ab" * 16, peer_id))
        json.loads(stream.readline())
        response = protocol.pair_ok(
            "Attacker",
            monitors.to_wire([
                Monitor(peer_id, "0", 0, 0, 800, 600, primary=True)
            ]),
        )
        if pair_ok_hmac is not None:
            response["hmac"] = pair_ok_hmac
        send(conn, response)
        time.sleep(0.2)
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    return listener


@pytest.mark.parametrize("forged_hmac", [None, "00" * 32])
def test_forged_pair_ok_is_rejected_without_changing_the_token(
    tmp_path, forged_hmac
):
    token = "cd" * 32
    listener = forged_pair_ok_server(3, forged_hmac)
    victim = App(lambda s: None, cfg_path=tmp_path / "connector.json")
    victim.state.mark_ready()
    victim.cfg.peers["attacker"] = config.Peer(name="Known peer", token=token)
    try:
        victim.connect_manually("127.0.0.1", listener.getsockname()[1])
        assert wait_for(lambda: not victim._handshakes)
        assert victim.state.snapshot().get("session") is None
        assert victim.cfg.peers["attacker"].token == token
        assert "pair_ok not authenticated" in victim.state.snapshot()["error"]
    finally:
        victim.stop()
        listener.close()


@pytest.mark.parametrize("forged_hmac", [None, "00" * 32])
def test_forged_pair_ok_does_not_save_a_fresh_token(tmp_path, forged_hmac):
    listener = forged_pair_ok_server(3, forged_hmac)
    victim = App(lambda s: None, cfg_path=tmp_path / "connector.json")
    victim.state.mark_ready()
    try:
        victim.connect_manually("127.0.0.1", listener.getsockname()[1])
        assert wait_for(lambda: any(p.nonce for p in victim._handshakes))
        victim.submit_code("123456")
        assert wait_for(lambda: not victim._handshakes)
        assert victim.state.snapshot().get("session") is None
        assert victim.cfg.peers == {}
        assert "pair_ok not authenticated" in victim.state.snapshot()["error"]
    finally:
        victim.stop()
        listener.close()


def test_v2_pair_ok_without_hmac_is_accepted_with_a_warning(
    tmp_path, caplog
):
    token = "cd" * 32
    listener = forged_pair_ok_server(2)
    victim = App(lambda s: None, cfg_path=tmp_path / "connector.json")
    victim.state.mark_ready()
    victim.cfg.peers["attacker"] = config.Peer(name="Known peer", token=token)
    try:
        with caplog.at_level("WARNING", logger="mouseshare"):
            victim.connect_manually("127.0.0.1", listener.getsockname()[1])
            assert wait_for(lambda: victim.state.snapshot().get("session"))
        session = victim.state.snapshot()["session"]
        assert session["unauthenticated_peer"] is True
        assert "peer Attacker does not authenticate pair_ok (protocol v2)" in caplog.text
    finally:
        victim.stop()
        listener.close()


def test_protocol_version_cannot_be_downgraded_after_the_first_frame(tmp_path):
    """Discriminate app-level pinning from the link's own version pin.

    Removing the per-peer negotiated-version check must make this fail even
    though ``network._Link`` independently rejects mixed-version wire frames.
    """
    class Link:
        def send(self, _msg):
            pass

    victim = App(lambda _s: None, cfg_path=tmp_path / "downgrade.json")
    victim.state.mark_ready()
    victim.cfg.peers["attacker"] = config.Peer("Attacker", "cd" * 32)
    peer = _Peer("attacker", "", Link(), phase="offered", role="host")
    victim._handshakes.append(peer)
    try:
        challenge = protocol.pair_challenge("ab" * 16, "attacker")
        challenge["v"] = 3
        victim._on_message(peer, challenge)
        with pytest.raises(protocol.ProtocolError, match="version changed"):
            ok = protocol.pair_ok("Attacker", [])
            ok["v"] = 2
            victim._on_message(peer, ok)
        assert victim.state.snapshot()["session"] is None
        assert victim.cfg.peers["attacker"].token == "cd" * 32
    finally:
        victim.stop()


def test_inbound_duplicate_identity_cannot_disturb_live_session(victim):
    """An inbound pair_request cannot replace the live owner of its ID."""
    class LiveLink:
        def send(self, _msg):
            pass

    live = _Peer("client", "Client", LiveLink(), phase="session", role="host")
    victim._peers[live.device_id] = live
    victim._host = type("Host", (), {"remote": True, "peer_id": "client"})()
    capture = object()
    victim._capture = capture
    client, got = attacker(victim)
    try:
        client.send(protocol.pair_request("client", "Impostor"))
        assert wait_for(lambda: got)
        assert got[0] == {"t": "pair_err", "reason": "duplicate", "v": 3}
        assert victim._peers["client"] is live
        assert victim._capture is capture
        assert victim._host.remote is True
        assert victim.state.snapshot()["session"]["remote"] is True
    finally:
        client.close()
        victim._peers.pop("client", None)
        victim._host = victim._capture = None


def test_an_unsolicited_pair_ok_does_not_start_a_session(victim):
    """The attack that matters: pair_ok is what makes the other side start
    capturing input. Accepting one from a socket that never proved a code
    would make the whole handshake decorative."""
    client, _ = attacker(victim)
    client.send(protocol.pair_ok("Attacker", monitors.to_wire([]), token="ab" * 32))
    time.sleep(0.4)
    assert victim.state.snapshot().get("session") is None
    assert victim.cfg.peers == {}
    client.close()


def test_an_unsolicited_pair_ok_after_a_challenge_is_still_refused(victim):
    """Getting as far as a challenge does not entitle the caller to skip
    the proof."""
    client, got = attacker(victim)
    client.send(protocol.pair_request("rogue", "Rogue"))
    assert wait_for(lambda: got)
    client.send(protocol.pair_ok("Rogue", monitors.to_wire([])))
    time.sleep(0.4)
    assert victim.state.snapshot().get("session") is None
    client.close()


def test_a_proof_sent_before_any_request_is_refused(victim):
    client, _ = attacker(victim)
    client.send(protocol.pair_proof("00" * 32))
    time.sleep(0.4)
    assert victim.state.snapshot().get("session") is None
    client.close()


def test_input_messages_before_pairing_are_not_injected(victim):
    """A stray machine must not be able to type on this one."""
    client, _ = attacker(victim)
    client.send(protocol.key_char("a", True))
    client.send(protocol.enter(10, 10))
    time.sleep(0.4)
    assert victim.state.snapshot().get("session") is None
    assert victim._injector is None
    client.close()


def test_an_input_message_out_of_phase_drops_the_connection(victim):
    """Named for what it actually exercises: the phase gate, which rejects
    this before any handler sees it. A stream that is talking out of turn
    cannot be trusted to say when to stop suppressing input."""
    client, _ = attacker(victim)
    client.send({"t": "key", "kind": "char"})  # no 'value', no 'pressed'
    assert wait_for(lambda: not victim._server.has_connection())
    client.close()


def test_ping_during_pairing_is_ignored_without_advancing_or_dropping(victim):
    client, got = attacker(victim)
    client.send(protocol.pair_request("rogue", "Rogue"))
    assert wait_for(lambda: got)
    peer = victim._inbound_peer
    assert peer.phase == "challenged"
    client.send(protocol.ping(1))
    time.sleep(0.1)
    assert peer.phase == "challenged"
    assert victim._server.has_connection()
    client.close()


def test_three_wrong_codes_end_the_pairing(tmp_path):
    """The design says three attempts. The third wrong code is the third
    attempt, not a free one before the real limit."""
    from mouseshare.pairing import MAX_ATTEMPTS, PairingFailed, PendingPairing, proof

    pending = PendingPairing(local_id="b", peer_id="a")
    bad = proof(b"000000", pending.nonce, "a", "b")
    assert MAX_ATTEMPTS == 3
    assert pending.check(bad) is False
    assert pending.check(bad) is False
    with pytest.raises(PairingFailed, match="attempts"):
        pending.check(bad)


def test_a_fresh_identity_survives_a_restart(tmp_path):
    """The device id keys every stored token and is baked into the HMAC
    transcript. Regenerating it on each launch would silently break every
    pairing the user had made."""
    path = tmp_path / "config.json"
    first = App(lambda s: None, cfg_path=path).cfg.device_id
    assert path.exists(), "a fresh identity was never written to disk"
    assert App(lambda s: None, cfg_path=path).cfg.device_id == first


def test_a_corrupt_config_gets_a_new_identity_that_is_also_persisted(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ truncated")
    recovered = App(lambda s: None, cfg_path=path).cfg.device_id
    assert App(lambda s: None, cfg_path=path).cfg.device_id == recovered


# -- one handshake per socket ------------------------------------------------


def test_an_inbound_socket_cannot_hijack_an_outbound_handshake(tmp_path):
    """The phase must belong to a socket, not to the app.

    While this machine is dialling out and waiting to be let in, its phase
    legitimately allows `pair_ok`. If that phase is global, an entirely
    unauthenticated inbound socket can send one and take the session.
    """
    from mouseshare import config as cfgmod
    from mouseshare import monitors, protocol
    from mouseshare.network import MessageClient, MessageServer

    peer_id = "peerpeerpeer"

    # A peer that answers our call with a challenge and then goes quiet,
    # parking us in `proved` -- the phase that legitimately accepts pair_ok.
    holder = {}

    def peer_logic(msg):
        if msg.get("t") == "pair_request":
            holder["server"].send(protocol.pair_challenge(pairing.make_nonce(), peer_id))

    holder["server"] = MessageServer("127.0.0.1", 0, peer_logic)
    holder["server"].start()

    victim = App(lambda s: None, cfg_path=tmp_path / "victim.json")
    victim.state.mark_ready()
    # Already paired, so the challenge is answered automatically and we
    # land in `proved` without a human typing anything.
    victim.cfg.peers[peer_id] = cfgmod.Peer(name="Peer", token="cd" * 32)
    victim._server = MessageServer(
        "127.0.0.1", 0,
        victim._on_inbound_message, victim._on_inbound_disconnect,
    )
    victim._server.start()
    try:
        victim.connect_manually("127.0.0.1", holder["server"].port)
        assert wait_for(lambda: any(p.phase == "proved" for p in victim._handshakes)), \
            "never reached proved"

        from mouseshare.layout import Monitor

        attacker_link = MessageClient("127.0.0.1", victim._server.port)
        attacker_link.connect()
        attacker_link.start_reader(lambda m: None)
        # A well-formed pair_ok, so this cannot pass merely because the
        # payload was malformed enough to crash the handler.
        attacker_link.send(protocol.pair_ok(
            "Attacker",
            monitors.to_wire([Monitor("x", "0", 0, 0, 1920, 1080, primary=True)]),
            token="ab" * 32,
        ))
        time.sleep(0.5)

        assert victim.state.snapshot().get("session") is None
        assert victim._injector is None
        attacker_link.close()
    finally:
        victim.stop()
        holder["server"].stop()


def test_a_disconnect_on_a_refused_link_does_not_kill_the_live_one(tmp_path):
    """Refusing a second caller must not take down the peer we are talking
    to -- otherwise anyone on the LAN can drop the session at will."""
    from mouseshare.network import MessageClient, MessageServer

    silent = MessageServer("127.0.0.1", 0, lambda m: None)
    silent.start()

    victim = App(lambda s: None, cfg_path=tmp_path / "victim.json")
    victim.state.mark_ready()
    victim._server = MessageServer(
        "127.0.0.1", 0,
        victim._on_inbound_message, victim._on_inbound_disconnect,
    )
    victim._server.start()
    try:
        victim.connect_manually("127.0.0.1", silent.port)
        assert wait_for(lambda: bool(victim._handshakes))

        intruder = MessageClient("127.0.0.1", victim._server.port)
        intruder.connect()
        intruder.close()
        time.sleep(0.4)

        outbound = victim._handshakes[0]
        assert outbound.link.is_connected(), "the outbound link was collateral"
    finally:
        victim.stop()
        silent.stop()
