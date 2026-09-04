from mouseshare.layout import Layout, Monitor

PC = "pc"
MAC = "mac"


def side_by_side() -> Layout:
    """A two-monitor PC on the left, a one-monitor Mac to its right."""
    return Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(PC, "1", 1920, 0, 1920, 1080),
            Monitor(MAC, "0", 0, 0, 1728, 1117, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (3840, 0)},
    )


# -- plane placement ---------------------------------------------------------


def test_monitors_keep_their_os_arrangement_within_a_device():
    layout = side_by_side()
    assert layout.plane_rect(PC, "0") == (0, 0, 1920, 1080)
    assert layout.plane_rect(PC, "1") == (1920, 0, 1920, 1080)
    assert layout.plane_rect(MAC, "0") == (3840, 0, 1728, 1117)


def test_a_device_with_a_negative_origin_is_normalised_onto_the_plane():
    """A monitor left of the primary reports a negative OS x. The plane
    starts at the device offset regardless."""
    layout = Layout(
        monitors=[
            Monitor(MAC, "0", 0, 0, 1728, 1117, primary=True),
            Monitor(MAC, "1", -1920, 0, 1920, 1080),
        ],
        offsets={MAC: (100, 0)},
    )
    assert layout.plane_rect(MAC, "1") == (100, 0, 1920, 1080)
    assert layout.plane_rect(MAC, "0") == (2020, 0, 1728, 1117)


# -- crossing ----------------------------------------------------------------


def test_crossing_the_outer_edge_lands_on_the_other_device():
    layout = side_by_side()
    assert layout.map_exit(PC, 3840, 500) == (MAC, 0, 500)


def test_crossing_returns_coordinates_in_the_target_machines_own_os_space():
    """The Mac's monitor sits at plane x=3840 but at OS x=0. The client is
    a dumb injector, so it must receive its own coordinates."""
    layout = side_by_side()
    _, x, y = layout.map_exit(PC, 3840, 0)
    assert (x, y) == (0, 0)


def test_moving_between_two_monitors_of_the_same_device_is_not_a_crossing():
    """The OS already moves the cursor between a machine's own monitors."""
    layout = side_by_side()
    assert layout.map_exit(PC, 1920, 500) is None


def test_a_point_still_inside_the_device_is_not_a_crossing():
    layout = side_by_side()
    assert layout.map_exit(PC, 500, 500) is None


def test_a_point_in_empty_plane_space_is_not_a_crossing():
    layout = Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(MAC, "0", 0, 0, 1728, 1080, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (5000, 0)},  # a deliberate gap
    )
    assert layout.map_exit(PC, 1920, 500) is None


def test_crossing_back_from_the_mac_lands_on_the_pcs_right_hand_monitor():
    layout = side_by_side()
    assert layout.map_exit(MAC, -1, 500) == (PC, 3839, 500)


def test_stale_overlapping_targets_are_chosen_by_device_then_monitor_id():
    layout = Layout([
        Monitor(PC, "0", 0, 0, 100, 100),
        Monitor("z-device", "0", 0, 0, 100, 100),
        Monitor("a-device", "z-monitor", 0, 0, 100, 100),
        Monitor("a-device", "a-monitor", 0, 0, 100, 100),
    ], {PC: (0, 0), "z-device": (100, 0), "a-device": (100, 0)})
    assert layout.map_exit(PC, 100, 50) == ("a-device", 0, 50)


# -- clamping ----------------------------------------------------------------


def test_clamp_pins_a_point_inside_the_nearest_monitor_of_that_device():
    layout = side_by_side()
    assert layout.clamp(MAC, -50, 500) == (0, 500)
    assert layout.clamp(MAC, 5000, 5000) == (1727, 1116)


def test_clamp_leaves_a_point_that_is_already_on_a_monitor_alone():
    layout = side_by_side()
    assert layout.clamp(PC, 2500, 500) == (2500, 500)


# -- overlap validation ------------------------------------------------------


def test_overlapping_devices_are_rejected():
    layout = side_by_side()
    assert not layout.can_place(MAC, (1000, 0))


def test_a_placement_into_the_gap_of_an_l_shaped_device_is_allowed():
    """The PC's bounding box covers a rectangle its monitors do not fill.
    Validating against the box would wrongly refuse this placement, so
    validation uses the union of the actual monitors."""
    layout = Layout(
        monitors=[
            Monitor(PC, "0", 0, 0, 1920, 1080, primary=True),
            Monitor(PC, "1", 1920, -1080, 1920, 1080),
            Monitor(MAC, "0", 0, 0, 1728, 1080, primary=True),
        ],
        offsets={PC: (0, 0), MAC: (10000, 0)},
    )
    assert layout.plane_rect(PC, "0") == (0, 1080, 1920, 1080)
    assert layout.plane_rect(PC, "1") == (1920, 0, 1920, 1080)
    # (0, 0) sits in the empty corner of the L, inside the bounding box.
    assert layout.can_place(MAC, (0, 0))


def test_snap_moves_a_device_flush_against_its_neighbour():
    layout = side_by_side()
    layout.set_offset(MAC, (4200, 40))
    layout.snap_device(MAC, PC)
    assert layout.plane_rect(MAC, "0")[:2] == (3840, 40)


def test_snap_closes_an_overlap_as_well_as_a_gap():
    layout = side_by_side()
    layout.set_offset(MAC, (3600, 0))
    layout.snap_device(MAC, PC)
    assert layout.plane_rect(MAC, "0")[:2] == (3840, 0)


def test_snap_picks_the_nearest_edge():
    layout = side_by_side()
    layout.set_offset(MAC, (0, 1000))  # below the PC, roughly aligned
    layout.snap_device(MAC, PC)
    assert layout.plane_rect(MAC, "0")[:2] == (0, 1080)


def test_snap_without_an_anchor_uses_the_nearest_of_all_other_devices():
    layout = Layout([
        Monitor(PC, "0", 0, 0, 100, 100),
        Monitor(MAC, "0", 0, 0, 100, 100),
        Monitor("third", "0", 0, 0, 100, 100),
    ], {PC: (0, 0), MAC: (100, 0), "third": (205, 20)})
    layout.snap_device("third")
    assert layout.offsets["third"] == (200, 20)


def test_snap_ignores_a_device_without_monitor_geometry():
    layout = Layout(
        [Monitor(PC, "0", 0, 0, 100, 100)],
        {PC: (0, 0), "ghost": (150, 0)},
    )
    layout.snap_device("ghost")
    assert layout.offsets["ghost"] == (150, 0)
