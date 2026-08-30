from mouseshare.inject import Injector


class FakeBackend:
    """Records what would have been done to the real machine."""

    def __init__(self, unresolvable=()):
        self.calls = []
        self._unresolvable = set(unresolvable)

    def move_to(self, x, y):
        self.calls.append(("move", x, y))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def button(self, name, pressed):
        if name in self._unresolvable:
            return False
        self.calls.append(("button", name, pressed))
        return True

    def key(self, kind, value, pressed):
        if value in self._unresolvable:
            return False
        self.calls.append(("key", kind, value, pressed))
        return True


def test_a_pressed_key_is_remembered_until_it_is_released():
    inj = Injector(FakeBackend())
    inj.key("char", "a", True)
    assert inj.held() == {("key", "char", "a")}
    inj.key("char", "a", False)
    assert inj.held() == set()


def test_release_all_releases_every_key_still_down():
    """Crossing back mid-chord, or losing the connection, must not leave a
    modifier stuck down on the other machine."""
    backend = FakeBackend()
    inj = Injector(backend)
    inj.key("special", "shift_l", True)
    inj.key("special", "ctrl_l", True)
    inj.key("char", "c", True)

    backend.calls.clear()
    inj.release_all()

    released = {c[2] for c in backend.calls if c[0] == "key"}
    assert released == {"shift_l", "ctrl_l", "c"}
    assert all(c[3] is False for c in backend.calls if c[0] == "key")
    assert inj.held() == set()


def test_release_all_also_releases_held_mouse_buttons():
    backend = FakeBackend()
    inj = Injector(backend)
    inj.click("left", True)
    backend.calls.clear()
    inj.release_all()
    assert ("button", "left", False) in backend.calls
    assert inj.held() == set()


def test_release_all_is_safe_to_call_when_nothing_is_held():
    backend = FakeBackend()
    inj = Injector(backend)
    inj.release_all()
    inj.release_all()
    assert backend.calls == []


def test_release_all_is_idempotent_after_a_real_press():
    backend = FakeBackend()
    inj = Injector(backend)
    inj.key("char", "x", True)
    inj.release_all()
    count = len(backend.calls)
    inj.release_all()
    assert len(backend.calls) == count


def test_a_key_the_platform_cannot_resolve_is_not_recorded_as_held():
    """Otherwise release_all would try forever to release something that
    was never pressed."""
    inj = Injector(FakeBackend(unresolvable={"eject"}))
    inj.key("special", "eject", True)
    assert inj.held() == set()


def test_a_button_the_platform_lacks_is_not_recorded_as_held():
    inj = Injector(FakeBackend(unresolvable={"x1"}))
    inj.click("x1", True)
    assert inj.held() == set()


def test_repeated_key_downs_from_autorepeat_are_held_once():
    inj = Injector(FakeBackend())
    for _ in range(5):
        inj.key("char", "a", True)
    assert inj.held() == {("key", "char", "a")}
    inj.key("char", "a", False)
    assert inj.held() == set()


def test_movement_and_scroll_are_forwarded_without_being_tracked():
    backend = FakeBackend()
    inj = Injector(backend)
    inj.move_to(10, 20)
    inj.scroll(0, -1)
    assert backend.calls == [("move", 10, 20), ("scroll", 0, -1)]
    assert inj.held() == set()
