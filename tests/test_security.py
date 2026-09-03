"""Attacks the handshake from the wire, the way a stray machine would.

Each test connects a raw socket and sends something a well-behaved peer
never would. None of them may end with a session.
"""
import time

import pytest

from mouseshare import monitors, pairing, protocol
from mouseshare.app import App
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
        "127.0.0.1", 0, instance._on_message, instance._on_disconnect
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
    assert victim._phase == "challenged"
    client.send(protocol.ping(1))
    time.sleep(0.1)
    assert victim._phase == "challenged"
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
        "127.0.0.1", 0, victim._on_message, victim._on_disconnect
    )
    victim._server.start()
    try:
        victim.connect_manually("127.0.0.1", holder["server"].port)
        assert wait_for(lambda: victim._phase == "proved"), "never reached proved"

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
        "127.0.0.1", 0, victim._on_message, victim._on_disconnect
    )
    victim._server.start()
    try:
        victim.connect_manually("127.0.0.1", silent.port)
        assert wait_for(lambda: victim._client is not None)

        intruder = MessageClient("127.0.0.1", victim._server.port)
        intruder.connect()
        intruder.close()
        time.sleep(0.4)

        assert victim._client is not None, "the outbound link was collateral"
        assert victim._client.is_connected()
    finally:
        victim.stop()
        silent.stop()
