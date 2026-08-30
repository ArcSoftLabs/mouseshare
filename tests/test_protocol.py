import json

import pytest

from mouseshare import protocol as p


def test_encode_decode_round_trip():
    msg = p.pos(5, -3)
    line = p.encode(msg)
    assert line.endswith(b"\n")
    assert p.decode(line) == {"t": "pos", "v": p.VERSION, "x": 5, "y": -3}


def test_every_encoded_message_carries_the_protocol_version():
    assert json.loads(p.encode(p.leave()))["v"] == p.VERSION


def test_movement_helpers():
    assert p.enter(10, 20) == {"t": "enter", "x": 10, "y": 20}
    assert p.click("left", True) == {"t": "click", "button": "left", "pressed": True}
    assert p.scroll(0, -1) == {"t": "scroll", "dx": 0, "dy": -1}
    assert p.leave() == {"t": "leave"}


def test_key_helper_tags_special_keys_separately_from_characters():
    assert p.key_special("shift_l", True) == {
        "t": "key", "kind": "special", "value": "shift_l", "pressed": True
    }
    assert p.key_char("a", False) == {
        "t": "key", "kind": "char", "value": "a", "pressed": False
    }


def test_line_buffer_reassembles_partial_and_multiple_messages():
    buf = p.LineBuffer()
    data = p.encode(p.pos(1, 1)) + p.encode(p.leave())
    assert list(buf.feed(data[:5])) == []
    msgs = list(buf.feed(data[5:]))
    assert [m["t"] for m in msgs] == ["pos", "leave"]


def test_malformed_json_is_a_protocol_error_not_a_skipped_line():
    """v0.1 silently skipped garbage. Around authentication and global input
    suppression, a desynchronised stream must break the connection."""
    buf = p.LineBuffer()
    with pytest.raises(p.ProtocolError):
        list(buf.feed(b"not json\n"))


def test_version_mismatch_is_a_protocol_error():
    buf = p.LineBuffer()
    wrong = json.dumps({"t": "leave", "v": p.VERSION + 1}).encode() + b"\n"
    with pytest.raises(p.ProtocolError):
        list(buf.feed(wrong))


def test_missing_version_is_a_protocol_error():
    buf = p.LineBuffer()
    with pytest.raises(p.ProtocolError):
        list(buf.feed(b'{"t":"leave"}\n'))


def test_oversize_line_is_a_protocol_error_before_it_is_buffered():
    buf = p.LineBuffer()
    with pytest.raises(p.ProtocolError):
        list(buf.feed(b"x" * (p.MAX_LINE + 1)))


def test_line_just_under_the_cap_is_accepted():
    buf = p.LineBuffer()
    padding = "y" * (p.MAX_LINE // 2)
    line = json.dumps({"t": "pair_err", "v": p.VERSION, "reason": padding}).encode()
    assert len(line) < p.MAX_LINE
    msgs = list(buf.feed(line + b"\n"))
    assert msgs[0]["reason"] == padding
