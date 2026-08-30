# MouseShare

Share one keyboard and mouse between a Mac and a Windows PC. Move the
cursor off the edge of one screen and it appears on the other, with the
keyboard following it.

Install it on both machines, find the other one on your network, type the
code it shows you, then drag the screens into the arrangement you actually
have. That is the whole setup.

## Install

Download the build for each machine from
[Releases](https://github.com/ArcSoftLabs/mouseshare/releases) and run it.

- **Windows** — `mouseshare.exe`. Needs the WebView2 runtime, which is
  already present on Windows 11 and on any updated Windows 10. On first
  launch Windows Firewall asks whether to allow it on public and private
  networks: **allow it**, or the two machines cannot find or reach each
  other. The build is unsigned, so SmartScreen may also warn.
- **macOS** — `MouseShare.app`. The builds are unsigned, so the first
  launch needs right-click → Open. macOS will then ask for **Accessibility**
  and **Input Monitoring**; MouseShare cannot read or forward input without
  both. Settings shows their status if you skip the prompt.

## Use

1. Open MouseShare on both machines. Each finds the other under **Devices**.
2. Press **Connect** on the machine whose keyboard and mouse you want to
   use. That machine becomes the host.
3. The other machine shows a six-digit code. Type it into the first one.
   You are asked once per pair of machines; after that they recognise each
   other automatically.
4. Under **Layout**, drag the two machines into the positions their screens
   really sit in. Edges snap together.

Move the cursor past a shared edge and it crosses. Move it back and it
returns. The code is asked for once; after that the two machines recognise
each other automatically.

## How it works

Every install listens, advertises itself over mDNS, and browses for peers,
so neither machine is configured as "the server". The one you press
Connect on drives the session.

The host suppresses its own mouse and keyboard while the cursor is on the
other machine and forwards the events instead. The client is a dumb
injector: it receives absolute coordinates already resolved against its own
screens, and holds no layout of its own.

Every monitor of both machines sits on a shared plane. Whole machines move
on that plane; the monitors inside one machine keep the arrangement its OS
reports, because that is the OS's business, not this app's.

### Pairing

The code proves a person can see both screens. The proof is an HMAC over a
fresh nonce and both machine ids, so it cannot be replayed or aimed
elsewhere. On success the target hands out a random 32-byte token, and
every later connection proves knowledge of that token the same way. Neither
the code nor the token is ever sent.

**The session itself is not encrypted.** Keystrokes — passwords included —
cross your LAN in clear JSON. For two personal machines on a home network
that is a deliberate trade rather than an oversight; on a network you do
not trust, do not use this.

## Building

```sh
pip install -e . pyinstaller
pyinstaller packaging/mouseshare.spec
```

Tests run anywhere, with no display and no second machine:

```sh
pip install pytest zeroconf screeninfo
python -m pytest tests
```

The core is deliberately importable without a display, so protocol,
pairing, layout, transport, session and state logic are all covered there.
Input capture, injection and monitor enumeration are platform code and are
verified by hand.

To check an install without opening a window — CI runs this inside the
packaged app on both platforms:

```sh
mouseshare --smoke
```

### Release checklist

Before tagging, on both real machines:

- [ ] Cursor crosses in both directions and returns.
- [ ] Keyboard follows the cursor; modifiers do not stick when crossing
      mid-chord.
- [ ] Killing the peer process while the cursor is remote restores local
      input immediately.
- [ ] Pulling the network cable while remote does the same.
- [ ] macOS permission prompts appear, and Settings reports their state.
- [ ] Monitors at >100% scaling and on Retina land where the layout says.
- [ ] A monitor placed left of or above the primary works.
- [ ] The packaged app launches from its installed location.
- [ ] Windows Firewall was allowed for the app, and the Mac granted the
      local-network prompt.

## Limits

Two machines, one at a time. No clipboard sharing, no file transfer, no
keyboard remapping, no Linux build. Closing the window quits — minimize it
to keep sharing.
