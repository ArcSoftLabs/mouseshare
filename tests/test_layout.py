from mouseshare.layout import Screen, Layout


def side_by_side():
    return Layout({
        "host": Screen(0, 0, 1920, 1080),
        "client": Screen(1920, 0, 1920, 1080),
    })


def stacked():
    return Layout({
        "host": Screen(0, 0, 1920, 1080),
        "client": Screen(0, -1080, 1920, 1080),  # client above host
    })


def test_inside_screen_no_crossing():
    assert side_by_side().map_exit("host", 960, 540) is None


def test_cross_right_edge():
    assert side_by_side().map_exit("host", 1920, 500) == ("client", 0, 500)


def test_cross_right_edge_with_overshoot():
    assert side_by_side().map_exit("host", 1925, 500) == ("client", 5, 500)


def test_cross_back_left_edge():
    assert side_by_side().map_exit("client", -3, 500) == ("host", 1917, 500)


def test_no_adjacent_screen_returns_none():
    # exiting the top of the host: nothing is there
    assert side_by_side().map_exit("host", 960, -1) is None


def test_vertical_crossing_up():
    assert stacked().map_exit("host", 700, -1) == ("client", 700, 1079)


def test_vertical_crossing_back_down():
    assert stacked().map_exit("client", 700, 1080) == ("host", 700, 0)


def test_offset_screens_only_shared_edge_crosses():
    layout = Layout({
        "host": Screen(0, 0, 1920, 1080),
        "client": Screen(1920, 300, 1920, 1080),
    })
    # y=200 is above the client's top edge -> no crossing
    assert layout.map_exit("host", 1920, 200) is None
    # y=800 lands inside the client (local y = 800 - 300)
    assert layout.map_exit("host", 1920, 800) == ("client", 0, 500)


def test_set_size_preserves_position():
    layout = side_by_side()
    layout.set_size("client", 2560, 1440)
    assert layout.screens["client"] == Screen(1920, 0, 2560, 1440)


def test_snap_closes_gap_after_host_shrinks():
    # MacBook case: host adopts a real size narrower than the configured
    # default, leaving a dead gap before the client at x=1920
    layout = side_by_side()
    layout.set_size("host", 1440, 900)
    layout.snap("client", "host")
    assert layout.screens["client"] == Screen(1440, 0, 1920, 1080)
    assert layout.map_exit("host", 1440, 500) == ("client", 0, 500)


def test_snap_resolves_overlap():
    layout = Layout({
        "host": Screen(0, 0, 1920, 1080),
        "client": Screen(1000, 0, 1920, 1080),  # overlapping the host
    })
    layout.snap("client", "host")
    assert layout.screens["client"] == Screen(1920, 0, 1920, 1080)


def test_clamp_keeps_cursor_on_screen():
    layout = side_by_side()
    assert layout.clamp("host", -5, 2000) == (0, 1079)
    assert layout.clamp("host", 1930, -4) == (1919, 0)
