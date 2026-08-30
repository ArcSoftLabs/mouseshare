from mouseshare import protocol as p


def test_encode_decode_round_trip():
    msg = p.pos(5, -3)
    line = p.encode(msg)
    assert line.endswith(b"\n")
    assert p.decode(line) == {"t": "pos", "x": 5, "y": -3}


def test_message_helpers():
    assert p.hello("host", 1920, 1080)["t"] == "hello"
    assert p.enter(10, 20) == {"t": "enter", "x": 10, "y": 20}
    assert p.click("left", True) == {"t": "click", "button": "left", "pressed": True}
    assert p.scroll(0, -1) == {"t": "scroll", "dx": 0, "dy": -1}
    assert p.leave() == {"t": "leave"}


def test_line_buffer_reassembles_partial_and_multiple_messages():
    buf = p.LineBuffer()
    data = p.encode(p.pos(1, 1)) + p.encode(p.leave())
    # feed in awkward chunks
    msgs = list(buf.feed(data[:5]))
    assert msgs == []
    msgs = list(buf.feed(data[5:]))
    assert msgs == [{"t": "pos", "x": 1, "y": 1}, {"t": "leave"}]


def test_line_buffer_skips_garbage_lines():
    buf = p.LineBuffer()
    msgs = list(buf.feed(b"not json\n" + p.encode(p.leave())))
    assert msgs == [{"t": "leave"}]
