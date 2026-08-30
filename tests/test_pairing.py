import pytest

from mouseshare import pairing

A = "aaaaaaaaaaaa"  # the machine connecting (initiator)
B = "bbbbbbbbbbbb"  # the machine showing the code (target)
C = "cccccccccccc"


def test_code_is_exactly_six_ascii_digits():
    for _ in range(200):
        code = pairing.make_code()
        assert len(code) == 6
        assert code.isdigit()


def test_code_keeps_leading_zeros():
    """A code rendered as an int would show '42' where the other machine
    computed its HMAC over '000042'."""
    assert pairing.format_code(42) == "000042"


def test_nonce_is_hex_and_fresh_every_time():
    nonces = {pairing.make_nonce() for _ in range(100)}
    assert len(nonces) == 100
    assert all(len(n) == 32 and int(n, 16) >= 0 for n in nonces)


def test_correct_code_verifies():
    nonce, code = pairing.make_nonce(), pairing.make_code()
    mac = pairing.proof(code.encode(), nonce, A, B)
    assert pairing.verify(code.encode(), mac, nonce, A, B)


def test_wrong_code_does_not_verify():
    nonce, code = pairing.make_nonce(), pairing.make_code()
    mac = pairing.proof(b"999999", nonce, A, B)
    assert not pairing.verify(code.encode(), mac, nonce, A, B)


def test_proof_is_bound_to_both_device_ids():
    """A proof captured between A and B must not authenticate A to C."""
    nonce, code = pairing.make_nonce(), pairing.make_code()
    mac = pairing.proof(code.encode(), nonce, A, B)
    assert not pairing.verify(code.encode(), mac, nonce, A, C)
    assert not pairing.verify(code.encode(), mac, nonce, C, B)


def test_proof_is_bound_to_the_nonce():
    code = pairing.make_code()
    mac = pairing.proof(code.encode(), pairing.make_nonce(), A, B)
    assert not pairing.verify(code.encode(), mac, pairing.make_nonce(), A, B)


def test_token_is_random_not_derived_from_the_code():
    """A token derived from the code and the public nonce would be
    computable by anyone who watched the pairing exchange."""
    assert len({pairing.make_token() for _ in range(100)}) == 100
    assert len(bytes.fromhex(pairing.make_token())) == 32


def test_reconnect_proof_uses_the_token_bytes_as_the_key():
    token = pairing.make_token()
    nonce = pairing.make_nonce()
    mac = pairing.proof(bytes.fromhex(token), nonce, A, B)
    assert pairing.verify(bytes.fromhex(token), mac, nonce, A, B)
    assert not pairing.verify(bytes.fromhex(pairing.make_token()), mac, nonce, A, B)


# -- the pending-pairing state machine ---------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_pending_pairing_accepts_the_right_proof():
    clock = FakeClock()
    pending = pairing.PendingPairing(local_id=B, peer_id=A, clock=clock)
    mac = pairing.proof(pending.code.encode(), pending.nonce, A, B)
    assert pending.check(mac) is True


def test_the_third_wrong_code_ends_the_pairing():
    """Three attempts means the third failure is terminal, not the fourth."""
    clock = FakeClock()
    pending = pairing.PendingPairing(local_id=B, peer_id=A, clock=clock)
    bad = pairing.proof(b"000000", pending.nonce, A, B)
    assert pending.check(bad) is False
    assert pending.check(bad) is False
    with pytest.raises(pairing.PairingFailed, match="attempts"):
        pending.check(bad)


def test_a_correct_proof_after_expiry_is_refused():
    clock = FakeClock()
    pending = pairing.PendingPairing(local_id=B, peer_id=A, clock=clock)
    mac = pairing.proof(pending.code.encode(), pending.nonce, A, B)
    clock.now = pairing.TIMEOUT + 0.1
    with pytest.raises(pairing.PairingFailed, match="expired"):
        pending.check(mac)


def test_a_correct_proof_just_before_expiry_is_accepted():
    clock = FakeClock()
    pending = pairing.PendingPairing(local_id=B, peer_id=A, clock=clock)
    mac = pairing.proof(pending.code.encode(), pending.nonce, A, B)
    clock.now = pairing.TIMEOUT - 0.1
    assert pending.check(mac) is True
