# Installing a MouseShare build on Linux

The Linux build supports X11 sessions. It does not support a native Wayland
session: the compositor deliberately prevents applications from globally
capturing and injecting keyboard and pointer events. A portal-based path is
future work.

Each command block below is copy-pasteable as a whole.

---

## 1. Pick a build

Download one of the Linux assets from
[Releases](https://github.com/ArcSoftLabs/mouseshare/releases):

- `MouseShare-x86_64.AppImage` is the portable choice for Ubuntu, Fedora, and
  other x86-64 distributions. It still needs the GTK and WebKitGTK runtime
  packages listed below.
- `mouseshare_..._amd64.deb` integrates with Ubuntu and other Debian-based
  systems and declares its runtime dependencies.
- `mouseshare-...x86_64.rpm` integrates with Fedora and declares its runtime
  dependencies.
- `MouseShare-linux-x86_64.tar.gz` contains the bare executable, desktop file,
  and icon for a manual installation.

Python is bundled in every build. A system `python3-gi` package is not needed,
but the system GTK/WebKitGTK libraries and GObject-introspection typelibs are.

## 2. Install the runtime packages

Ubuntu 22.04:

```sh
sudo apt update
sudo apt install libgtk-3-0 libwebkit2gtk-4.0-37 gir1.2-gtk-3.0 gir1.2-webkit2-4.0
```

Ubuntu 24.04:

```sh
sudo apt update
sudo apt install libgtk-3-0 libwebkit2gtk-4.1-0 gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

Fedora 40 and newer:

```sh
sudo dnf install gtk3 webkit2gtk4.1
```

## 3. Use an X11 session

Check the current session before launching:

```sh
echo "$XDG_SESSION_TYPE"
```

It must print `x11`. On Ubuntu, log out, select your user, click the gear in
the lower-right corner, choose **Ubuntu on Xorg**, and sign in again. Under
WSLg, MouseShare selects the X11 path: its listeners start, and grabbing,
warping, and ungrabbing work on the XWayland server. Whether the grab confines
real input under WSLg has not been tested.

A native Wayland session is explicitly unsupported. XWayland alone cannot see
or control Wayland-native applications, so it cannot provide reliable global
capture and injection.

## 4. Install and launch

For the AppImage:

```sh
chmod +x MouseShare-x86_64.AppImage
./MouseShare-x86_64.AppImage
```

For Ubuntu/Debian:

```sh
sudo apt install ./mouseshare_*_amd64.deb
mouseshare
```

For Fedora:

```sh
sudo dnf install ./mouseshare-*.x86_64.rpm
mouseshare
```

For the tarball, unpack it and install all three files:

```sh
tar -xzf MouseShare-linux-x86_64.tar.gz
sudo install -m 0755 mouseshare /usr/local/bin/mouseshare
sudo install -m 0644 mouseshare.desktop /usr/local/share/applications/mouseshare.desktop
sudo install -m 0644 MouseShare.png /usr/local/share/icons/hicolor/256x256/apps/mouseshare.png
mouseshare
```

## 5. Open the firewall

MouseShare discovery uses mDNS on UDP port 5353. Connections use TCP port
39471 by default; substitute the configured app port if you changed it.

Ubuntu with UFW:

```sh
sudo ufw allow 5353/udp
sudo ufw allow 39471/tcp
```

Fedora with firewalld:

```sh
sudo firewall-cmd --permanent --add-port=5353/udp
sudo firewall-cmd --permanent --add-port=39471/tcp
sudo firewall-cmd --reload
```

## Config and logs

The configuration is at
`${XDG_CONFIG_HOME:-$HOME/.config}/mouseshare/config.json`. Debug logging goes
to `${XDG_STATE_HOME:-$HOME/.local/state}/mouseshare/debug.log` when the app is
started with `mouseshare --debug`.

Check an installation without opening the window:

```sh
mouseshare --smoke
```

## Troubleshooting

- **Blank window:** install the WebKitGTK library and matching
  GObject-introspection typelib for your distribution, then run
  `mouseshare --smoke`.
- **Mouse or keyboard is not captured:** run `echo "$XDG_SESSION_TYPE"`. If it
  prints `wayland`, log out and select an X11 session such as **Ubuntu on
  Xorg**.
- **Permission denied mentions `/dev/uinput`:** MouseShare's X11 backend does
  not use `/dev/uinput`, so adding udev rules or running as root will not fix
  it. Confirm that an X display is available and that `DISPLAY` is set.
- **Peers do not appear or connect:** open UDP 5353 and the configured TCP app
  port in the firewall on both machines.
