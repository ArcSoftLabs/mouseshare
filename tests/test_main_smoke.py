import sys
import types

import pytest

from mouseshare import __main__ as entrypoint
from mouseshare import config


def test_linux_pynput_probe_imports_xorg_backends_and_opens_display(monkeypatch):
    imported = []
    display_calls = []

    class Display:
        def __init__(self):
            display_calls.append("open")

        def close(self):
            display_calls.append("close")

    modules = {
        "pynput.mouse._xorg": types.SimpleNamespace(),
        "pynput.keyboard._xorg": types.SimpleNamespace(),
        "Xlib.display": types.SimpleNamespace(Display=Display),
    }

    def import_module(name):
        imported.append(name)
        return modules[name]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("importlib.import_module", import_module)

    assert entrypoint._probe_pynput() == "pynput.mouse._xorg, pynput.keyboard._xorg; display open"
    assert imported == [
        "pynput.mouse._xorg",
        "pynput.keyboard._xorg",
        "Xlib.display",
    ]
    assert display_calls == ["open", "close"]


def test_linux_pynput_probe_fails_when_display_cannot_open(monkeypatch):
    class Display:
        def __init__(self):
            raise OSError("cannot open display")

    modules = {
        "pynput.mouse._xorg": types.SimpleNamespace(),
        "pynput.keyboard._xorg": types.SimpleNamespace(),
        "Xlib.display": types.SimpleNamespace(Display=Display),
    }
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("importlib.import_module", modules.__getitem__)

    with pytest.raises(OSError, match="cannot open display"):
        entrypoint._probe_pynput()


def test_linux_session_probe_reports_detected_session(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("mouseshare.linux.session_type", lambda: "x11")

    assert entrypoint._probe_linux_session() == "x11"


def test_smoke_reports_linux_session_and_fails_when_a_probe_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(entrypoint, "web_dir", lambda: "/missing")
    monkeypatch.setattr(entrypoint, "_probe_backend", lambda: "gtk")
    monkeypatch.setattr(entrypoint, "_probe_zeroconf", lambda: "ok")
    monkeypatch.setattr(entrypoint, "_probe_monitors", lambda: "ok")
    monkeypatch.setattr(entrypoint, "_probe_pynput", lambda: "ok")
    monkeypatch.setattr(entrypoint, "_probe_linux_session", lambda: "x11")

    assert entrypoint.smoke() == 1
    output = capsys.readouterr().out
    assert "PASS  Linux session: x11" in output
    assert "SMOKE FAILED" in output


def test_linux_smoke_succeeds_when_every_probe_passes(tmp_path, monkeypatch, capsys):
    (tmp_path / "index.html").write_text("ok")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(entrypoint, "web_dir", lambda: str(tmp_path))
    monkeypatch.setattr(entrypoint, "_probe_backend", lambda: "gtk")
    monkeypatch.setattr(entrypoint, "_probe_zeroconf", lambda: "ok")
    monkeypatch.setattr(entrypoint, "_probe_monitors", lambda: "ok")
    monkeypatch.setattr(entrypoint, "_probe_pynput", lambda: "xorg")
    monkeypatch.setattr(entrypoint, "_probe_linux_session", lambda: "x11")

    assert entrypoint.smoke() == 0
    assert "SMOKE OK" in capsys.readouterr().out


def test_linux_public_paths_use_xdg_locations(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert config.default_path() == tmp_path / "config" / "mouseshare" / "config.json"
    assert entrypoint.log_path() == str(tmp_path / "state" / "mouseshare" / "debug.log")
