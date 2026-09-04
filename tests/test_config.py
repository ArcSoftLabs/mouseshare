import json
import os
import stat
import sys

from mouseshare import config


def test_a_fresh_install_generates_a_device_identity(tmp_path):
    cfg = config.load(tmp_path / "config.json")
    assert len(cfg.device_id) == 32
    assert cfg.name  # defaults to the machine's hostname
    assert cfg.port == config.DEFAULT_PORT


def test_escape_key_defaults_to_cmd_on_macos_and_ctrl_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.load(tmp_path / "mac.json").escape_key == "cmd"
    monkeypatch.setattr(sys, "platform", "win32")
    assert config.load(tmp_path / "win.json").escape_key == "ctrl"


def test_the_device_id_is_stable_across_restarts(tmp_path):
    path = tmp_path / "config.json"
    first = config.load(path)
    config.save(first, path)
    assert config.load(path).device_id == first.device_id


def test_peers_round_trip_with_their_tokens_and_offsets(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.load(path)
    cfg.peers["deadbeef"] = config.Peer(
        name="Benjamin's Mac", token="ab" * 32, last_address="192.168.1.20"
    )
    cfg.offsets["deadbeef"] = (1920, 0)
    cfg.escape_key = "alt"
    cfg.share_clipboard = False
    config.save(cfg, path)

    loaded = config.load(path)
    assert loaded.peers["deadbeef"].name == "Benjamin's Mac"
    assert loaded.peers["deadbeef"].token == "ab" * 32
    assert loaded.peers["deadbeef"].last_address == "192.168.1.20"
    assert loaded.offsets["deadbeef"] == (1920, 0)
    assert loaded.escape_key == "alt"
    assert loaded.share_clipboard is False


def test_clipboard_sharing_defaults_on(tmp_path):
    assert config.load(tmp_path / "config.json").share_clipboard is True


def test_a_corrupt_config_is_replaced_rather_than_crashing_the_app(tmp_path):
    """Losing the pairing is annoying. Refusing to start because a JSON
    file got truncated is worse."""
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    cfg = config.load(path)
    assert len(cfg.device_id) == 32
    assert cfg.peers == {}


def test_a_v0_1_config_file_still_loads(tmp_path):
    """v0.1 wrote peer_host/port/screens. None of those survive, but an
    upgrade must not crash on them."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "peer_host": "192.168.1.20",
        "port": 39471,
        "screens": {"host": {"x": 0, "y": 0, "w": 1920, "h": 1080}},
    }))
    cfg = config.load(path)
    assert cfg.port == 39471
    assert len(cfg.device_id) == 32


def test_save_leaves_no_temporary_files_behind(tmp_path):
    path = tmp_path / "config.json"
    config.save(config.load(path), path)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_save_is_atomic_so_a_reader_never_sees_a_half_written_file(tmp_path):
    """The replace happens on a fully written file, so the config is either
    the old one or the new one -- never a truncated one."""
    path = tmp_path / "config.json"
    cfg = config.load(path)
    config.save(cfg, path)
    before = path.read_text()

    cfg.peers["x" * 8] = config.Peer(name="n", token="cd" * 32, last_address="")
    config.save(cfg, path)
    assert path.read_text() != before
    json.loads(path.read_text())  # always parseable


def test_the_config_is_not_world_readable_because_it_holds_tokens(tmp_path):
    if sys.platform == "win32":
        return  # POSIX mode bits do not apply
    path = tmp_path / "config.json"
    config.save(config.load(path), path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
