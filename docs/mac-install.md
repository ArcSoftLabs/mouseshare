# Installing a MouseShare build on the Mac

The checklist the build workflow refers to. Written for the Mac because that
is the side that is packaged, ad-hoc signed, and permission-gated; the PC can
be run straight from a checkout.

Each block below is copy-pasteable as a whole.

---

## 1. Wait for the build, then download it

```sh
cd ~/Downloads
gh run watch --repo ArcSoftLabs/mouseshare $(gh run list --repo ArcSoftLabs/mouseshare --limit 1 --json databaseId --jq '.[0].databaseId')
rm -rf MouseShare-macos MouseShare-macos.zip
gh run download --repo ArcSoftLabs/mouseshare --name MouseShare-macos --dir ~/Downloads/MouseShare-macos
```

If `gh` is not logged in on the Mac, run `gh auth login` first, or download the
`MouseShare-macos` artifact from the run page in a browser and unzip it into
`~/Downloads/MouseShare-macos`.

## 2. Quit the old app and remove it

Remove rather than overwrite. A half-replaced bundle keeps the old binary, and
then you are testing the old build without knowing it -- which has happened,
and costs a whole round of testing before anyone notices.

```sh
osascript -e 'quit app "MouseShare"' 2>/dev/null
sleep 2
pkill -f MouseShare 2>/dev/null
rm -rf /Applications/MouseShare.app
```

## 3. Install the new one

```sh
cd ~/Downloads/MouseShare-macos
unzip -o MouseShare-macos.zip
ditto MouseShare.app /Applications/MouseShare.app
xattr -dr com.apple.quarantine /Applications/MouseShare.app
codesign --force --deep --sign - /Applications/MouseShare.app
```

## 4. Clear the stale permission grants

This is the step that bites, and it is not optional.

The app is ad-hoc signed, so its identity is a hash of the binary. Every
rebuild is therefore a *different application* as far as macOS is concerned,
and the permissions granted to the previous one silently do not apply. The
symptom is the app's own permission dots staying orange no matter how many
times you toggle the switches in System Settings.

```sh
tccutil reset Accessibility com.arcsoftlabs.mouseshare || true
tccutil reset ListenEvent com.arcsoftlabs.mouseshare || true
tccutil reset PostEvent com.arcsoftlabs.mouseshare || true
```

A "no such service" message from any one of these is fine -- not all three
exist on every macOS version.

## 5. Launch and re-grant

```sh
open /Applications/MouseShare.app
```

Then in System Settings -> Privacy & Security, add and enable
`/Applications/MouseShare.app` under **both**:

- Accessibility
- Input Monitoring

If MouseShare is already listed, remove it with `-` and add it again. A stale
entry points at the old binary hash and will never take effect.

Quit and reopen MouseShare. **The dots in the app must be green before you
test anything.** Orange means the grants have not taken, and anything you
observe after that is measuring the wrong thing.

## 6. Connect

On the machine with the keyboard and mouse, press Connect and enter the
six-digit code shown on the other one. The machine you press Connect on is
the host; the code always appears on the client.

---

## What to check by hand

The test suite runs without a display and fakes the input layer, so capture,
injection and monitor enumeration are only ever verified here. Use the mouse
rather than a trackpad: a trackpad reports at a fraction of the rate and hides
every throughput problem there is.

- **Crossing.** Every shared edge, in both directions, several times.
- **The glide test.** Move fast until it lags, then stop your hand dead. The
  remote cursor must stop when your hand stops. Continued gliding means a
  backlog is draining somewhere -- see `outbox.py`.
- **Sustained speed.** Several seconds of fast movement should track rather
  than trail.
- **Drag across.** Hold the left button down while crossing between machines.
- **Scrolling.** Scroll hard for a few seconds. Scroll events are deltas, so
  unlike absolute positions they cannot be collapsed by replacement, and they
  are the likeliest remaining place for a backlog to build.
- **Keyboard.** Type into something on the remote machine, including a
  modifier combination, and confirm nothing stays held down afterwards.

## Reading the debug log

Run the app from the terminal instead of `open` to get it:

```sh
/Applications/MouseShare.app/Contents/MacOS/MouseShare --debug
```

The line worth watching is the periodic

```
outbox: <n> queued, <n> sent, <n> collapsed, depth <n>
```

- `depth` climbing means the consumer on that side cannot keep up.
- `collapsed` climbing during fast movement is healthy: it counts stale
  positions discarded instead of replayed.
- Both flat at zero during fast movement means messages are not arriving at
  the rate you think they are.
