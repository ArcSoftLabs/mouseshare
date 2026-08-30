# MouseShare

Share one mouse between a **Mac** and a **Windows PC** at the same time, over
your local network — like a software KVM. Plug your mouse (e.g. a Logitech
Hero Bluetooth mouse) into either machine, and glide the cursor from one
computer's screen onto the other's, exactly where you configured the screens
to sit.

> **How it works:** the mouse stays Bluetooth-paired to *one* machine (the
> **host**). MouseShare captures its movement there and forwards it over TCP
> to the other machine (the **client**), which injects the events. No
> re-pairing, no dongle games — the mouse hardware never knows.

## Install

Download `MouseShare-macos.zip` or `MouseShare-windows.zip` from the
[releases page](https://github.com/ArcSoftLabs/mouseshare/releases), unzip,
and run the `mouseshare` executable. Or, from source on either machine:

```sh
pip install git+https://github.com/ArcSoftLabs/mouseshare
```

## Usage

1. **Configure the layout** (either machine): run `mouseshare` (or
   `mouseshare layout`). Drag the two screen rectangles so they touch along
   the edge where the cursor should cross — left/right/above/below, any
   offset you like. Enter the host machine's LAN IP, and Save.
2. **On the machine the mouse is plugged into:** `mouseshare host`
3. **On the other machine:** `mouseshare client <host-ip>`

Now push the cursor past the shared edge — it appears on the other computer.
Clicks and scrolling follow it. Push back across the edge to return.

Both directions work regardless of which OS hosts the mouse: Mac→Windows and
Windows→Mac.

## macOS permissions (required)

macOS blocks global input capture/injection until you allow it. In **System
Settings → Privacy & Security**, add the `mouseshare` executable (or your
terminal app, when running from source) to both:

- **Accessibility**
- **Input Monitoring**

Then restart MouseShare. Windows needs no special permissions.

## Firewall

The client connects to the host on TCP port **39471** (configurable in the
layout editor). Allow it through the host's firewall for your local network.

## Configuration file

Saved at `~/.mouseshare/config.json` — screen rectangles on a shared virtual
plane, the host IP, and the port. The layout editor is just a friendly way to
edit it.

## Development

```sh
pip install -e . pytest
python -m pytest tests
```

The core (layout math, wire protocol, config, transport) is pure Python and
fully tested; `capture.py`/`inject.py` are thin [pynput](https://pypi.org/project/pynput/)
adapters. Packaged executables are built by GitHub Actions (PyInstaller) for
macOS and Windows on every tagged release.

## Limitations (v0.1)

- Two machines, one shared mouse; keyboard and clipboard are not shared.
- Traffic is unencrypted — use on trusted networks only.
- Single display per machine (use each machine's primary display).
