import sys
import types

from mouseshare import monitors
from mouseshare.layout import Monitor


class FakeScreen:
    def __init__(self, x, y, width, height, is_primary=False, name=None):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.is_primary = is_primary
        self.name = name


def test_screens_become_monitors_tagged_with_this_device():
    got = monitors.from_screens("pc", [
        FakeScreen(0, 0, 1920, 1080, is_primary=True, name="DISPLAY1"),
        FakeScreen(1920, 0, 2560, 1440, name="DISPLAY2"),
    ])
    assert got == [
        Monitor("pc", "DISPLAY1", 0, 0, 1920, 1080, primary=True),
        Monitor("pc", "DISPLAY2", 1920, 0, 2560, 1440, primary=False),
    ]


def test_a_monitor_left_of_the_primary_keeps_its_negative_origin():
    """Those coordinates go straight to the injector, so normalising them
    here would warp the cursor to the wrong screen."""
    got = monitors.from_screens("mac", [FakeScreen(-1920, -200, 1920, 1080)])
    assert (got[0].x, got[0].y) == (-1920, -200)


def test_screens_without_names_get_stable_positional_ids():
    got = monitors.from_screens("pc", [
        FakeScreen(0, 0, 1920, 1080),
        FakeScreen(1920, 0, 1920, 1080),
    ])
    assert [m.id for m in got] == ["0", "1"]


def test_duplicate_names_are_disambiguated():
    got = monitors.from_screens("pc", [
        FakeScreen(0, 0, 1920, 1080, name="Display"),
        FakeScreen(1920, 0, 1920, 1080, name="Display"),
    ])
    assert len({m.id for m in got}) == 2


def test_monitors_serialise_for_the_wire_and_back():
    original = [
        Monitor("pc", "0", 0, 0, 1920, 1080, primary=True),
        Monitor("pc", "1", -1920, 0, 1920, 1080),
    ]
    assert monitors.from_wire("pc", monitors.to_wire(original)) == original


def test_appkit_single_primary_is_already_in_quartz_coordinates():
    screens = [FakeScreen(0, 0, 2560, 1440, is_primary=True)]
    converted = monitors.appkit_to_quartz(screens)
    assert [(s.x, s.y, s.width, s.height) for s in converted] == [
        (0, 0, 2560, 1440)
    ]


def test_appkit_bottom_aligned_secondary_moves_down_in_quartz():
    screens = [
        FakeScreen(0, 0, 2560, 1440, is_primary=True),
        FakeScreen(2560, 0, 1920, 1080),
    ]
    converted = monitors.appkit_to_quartz(screens)
    assert converted[1].y == 360


def test_appkit_secondary_above_primary_gets_negative_quartz_origin():
    screens = [
        FakeScreen(0, 0, 2560, 1440, is_primary=True),
        FakeScreen(0, 1440, 1920, 1080),
    ]
    converted = monitors.appkit_to_quartz(screens)
    assert converted[1].y == -1080


def test_appkit_top_aligned_secondary_has_zero_quartz_y():
    screens = [
        FakeScreen(0, 0, 2560, 1440, is_primary=True),
        FakeScreen(2560, 360, 1920, 1080),
    ]
    converted = monitors.appkit_to_quartz(screens)
    assert converted[1].y == 0


def test_enumerate_local_only_converts_appkit_coordinates_on_darwin(monkeypatch):
    fake_screeninfo = types.SimpleNamespace(get_monitors=lambda: [
        FakeScreen(0, 0, 2560, 1440, is_primary=True),
        FakeScreen(2560, 0, 1920, 1080),
    ])
    monkeypatch.setitem(sys.modules, "screeninfo", fake_screeninfo)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert monitors.enumerate_local("mac")[1].y == 360

    monkeypatch.setattr(sys, "platform", "win32")
    assert monitors.enumerate_local("pc")[1].y == 0
