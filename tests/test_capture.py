from mouseshare.capture import key_to_wire


class FakeKey:
    """Stands in for a `pynput.keyboard.Key` enum member."""

    def __init__(self, name):
        self.name = name


class FakeKeyCode:
    """Stands in for a `pynput.keyboard.KeyCode`."""

    def __init__(self, char=None):
        self.char = char


def test_a_named_key_travels_as_a_special():
    assert key_to_wire(FakeKey("shift_l"), FakeKey) == ("special", "shift_l")


def test_a_printable_key_travels_as_a_character():
    assert key_to_wire(FakeKeyCode("a"), FakeKey) == ("char", "a")


def test_an_unresolvable_key_is_dropped_rather_than_sent_as_junk():
    """Sending a key the other machine cannot resolve would leave the
    injector unable to release it later."""
    assert key_to_wire(FakeKeyCode(None), FakeKey) is None
    assert key_to_wire(None, FakeKey) is None


def test_a_dead_key_with_no_character_is_dropped():
    assert key_to_wire(FakeKeyCode(""), FakeKey) is None
