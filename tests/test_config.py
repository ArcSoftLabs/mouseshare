from mouseshare.config import Config, load, save
from mouseshare.layout import Screen


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load(tmp_path / "config.json")
    assert cfg.port == 39471
    assert cfg.peer_host == ""
    assert set(cfg.screens) == {"host", "client"}
    assert cfg.screens["client"].x == cfg.screens["host"].w  # side by side default


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(
        peer_host="192.168.1.50",
        port=4000,
        screens={"host": Screen(0, 0, 2560, 1440), "client": Screen(2560, 200, 1920, 1080)},
    )
    save(cfg, path)
    assert load(path) == cfg
