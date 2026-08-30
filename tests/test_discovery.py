import time

import pytest

from mouseshare import discovery

zeroconf = pytest.importorskip("zeroconf")


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_browser_finds_an_advertised_peer():
    found = {}
    advertiser = discovery.Advertiser(
        device_id="aaaa", name="Advertised PC", port=39471
    )
    browser = discovery.Browser(
        device_id="bbbb", on_change=lambda peers: found.update(peers)
    )
    advertiser.start()
    browser.start()
    try:
        assert wait_for(lambda: "aaaa" in found), "peer never appeared"
        peer = found["aaaa"]
        assert peer.name == "Advertised PC"
        assert peer.port == 39471
        assert peer.address
    finally:
        browser.stop()
        advertiser.stop()


def test_a_browser_does_not_report_the_machine_it_runs_on():
    """Both machines advertise, so without a self-filter every install
    would show itself in its own device list."""
    seen = {}
    advertiser = discovery.Advertiser(device_id="same", name="Me", port=39472)
    browser = discovery.Browser(
        device_id="same", on_change=lambda peers: seen.update(peers)
    )
    advertiser.start()
    browser.start()
    try:
        time.sleep(2.0)
        assert "same" not in seen
    finally:
        browser.stop()
        advertiser.stop()


def test_stopping_is_safe_before_starting():
    discovery.Advertiser(device_id="x", name="x", port=1).stop()
    discovery.Browser(device_id="x", on_change=lambda peers: None).stop()
