from mouseshare.capture import EscapeDetector


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_two_complete_taps_inside_the_window_escape():
    clock = Clock()
    detector = EscapeDetector("ctrl", 0.5, clock)
    assert detector.event("ctrl_l", True) is False
    assert detector.event("ctrl_l", False) is False
    clock.now = 0.4
    assert detector.event("ctrl_r", True) is False
    assert detector.event("ctrl_r", False) is True


def test_another_key_between_taps_cancels_the_gesture():
    detector = EscapeDetector("ctrl", 0.5, Clock())
    detector.event("ctrl_l", True)
    detector.event("ctrl_l", False)
    detector.event("a", True)
    detector.event("a", False)
    detector.event("ctrl_l", True)
    assert detector.event("ctrl_l", False) is False


def test_taps_outside_the_window_do_not_escape():
    clock = Clock()
    detector = EscapeDetector("ctrl", 0.5, clock)
    detector.event("ctrl_l", True)
    detector.event("ctrl_l", False)
    clock.now = 0.51
    detector.event("ctrl_l", True)
    assert detector.event("ctrl_l", False) is False


def test_key_repeat_does_not_count_as_an_extra_tap():
    detector = EscapeDetector("ctrl", 0.5, Clock())
    detector.event("ctrl_l", True)
    detector.event("ctrl_l", True)
    detector.event("ctrl_l", False)
    detector.event("ctrl_l", True)
    assert detector.event("ctrl_l", False) is False
