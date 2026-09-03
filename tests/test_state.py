import threading

from mouseshare.state import StateOwner


def test_nothing_is_delivered_before_the_page_says_it_is_ready():
    """State can change while the webview is still loading. Pushing into a
    page with no listener would silently lose it."""
    delivered = []
    owner = StateOwner(delivered.append, initial={"screen": "devices"})
    owner.set(screen="pairing")
    assert delivered == []


def test_the_ready_handshake_returns_the_state_changed_while_loading():
    delivered = []
    owner = StateOwner(delivered.append, initial={"screen": "devices"})
    owner.set(screen="pairing")
    snapshot = owner.mark_ready()
    assert snapshot["screen"] == "pairing"


def test_updates_after_ready_are_delivered():
    delivered = []
    owner = StateOwner(delivered.append, initial={"screen": "devices"})
    owner.mark_ready()
    owner.set(screen="layout")
    assert [d["screen"] for d in delivered] == ["layout"]


def test_async_update_delivers_off_the_calling_thread():
    calling_thread = threading.get_ident()
    delivered_on = []
    delivered = threading.Event()

    def record(_snapshot):
        delivered_on.append(threading.get_ident())
        delivered.set()

    owner = StateOwner(record, initial={"remote": False})
    owner.mark_ready()
    owner.set_async(remote=True)

    assert delivered.wait(1)
    assert delivered_on != [calling_thread]


def test_every_delivery_carries_a_higher_revision_than_the_last():
    delivered = []
    owner = StateOwner(delivered.append, initial={"n": 0})
    owner.mark_ready()
    for i in range(1, 6):
        owner.set(n=i)
    revisions = [d["revision"] for d in delivered]
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == len(revisions)


def test_a_stale_snapshot_never_rewinds_the_ui():
    delivered = []
    owner = StateOwner(delivered.append, initial={"n": 0})
    owner.mark_ready()
    owner.set(n=1)
    owner.deliver(owner.snapshot_at(revision=0, state={"n": 99}))
    assert delivered[-1]["n"] == 1


def test_concurrent_updates_stay_monotonic():
    """Discovery, the socket reader and the input listeners all publish
    from their own threads."""
    delivered = []
    lock = threading.Lock()

    def record(snapshot):
        with lock:
            delivered.append(snapshot["revision"])

    owner = StateOwner(record, initial={"n": 0})
    owner.mark_ready()

    def worker(base):
        for i in range(50):
            owner.set(n=base * 100 + i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert delivered == sorted(delivered)
    assert len(set(delivered)) == len(delivered)


def test_a_failing_delivery_does_not_break_the_state_owner():
    """Closing the window invalidates the JS target mid-push."""
    calls = []

    def flaky(snapshot):
        calls.append(snapshot)
        raise RuntimeError("window is gone")

    owner = StateOwner(flaky, initial={"n": 0})
    owner.mark_ready()
    owner.set(n=1)
    owner.set(n=2)
    assert len(calls) == 2
    assert owner.snapshot()["n"] == 2


def test_snapshot_is_a_copy_so_callers_cannot_mutate_shared_state():
    owner = StateOwner(lambda s: None, initial={"peers": {}})
    snap = owner.snapshot()
    snap["peers"]["x"] = 1
    assert owner.snapshot()["peers"] == {}


def test_concurrent_async_publishes_never_rewind():
    """Two deliveries racing must land in revision order even when the
    earlier one is slow inside the callback. (The check-then-lock window
    the delivery lock now covers is too narrow to provoke from a test;
    this pins the observable ordering contract.)"""
    import time

    delivered = []

    def slow_then_fast(snapshot):
        if snapshot["revision"] == 1:
            time.sleep(0.1)
        delivered.append(snapshot["revision"])

    owner = StateOwner(slow_then_fast, initial={})
    owner.mark_ready()
    first = StateOwner.snapshot_at(1, {})
    second = StateOwner.snapshot_at(2, {})
    a = threading.Thread(target=owner.deliver, args=(first,))
    b = threading.Thread(target=owner.deliver, args=(second,))
    a.start()
    time.sleep(0.02)
    b.start()
    a.join(2)
    b.join(2)
    assert delivered == [1, 2]
