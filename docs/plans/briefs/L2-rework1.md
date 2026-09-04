# Task L2+L3 — rework round 1 (review findings to fix)

Repository: current working directory. Your L2 changes are uncommitted. Fix exactly the items below; nothing else. Do NOT commit. **Concurrency:** another task (F1) is editing `mouseshare/transfer.py`, `mouseshare/app.py`, `mouseshare/protocol.py`, `mouseshare/config.py`, `mouseshare/ui/web/*`, `tests/test_transfer.py`, `tests/test_multi.py`, `tests/test_app.py` — do NOT touch any of those, and do not run `git checkout`/`stash`. Only edit: `packaging/linux/build-appimage.sh`, `packaging/linux/nfpm.yaml`, `packaging/mouseshare.spec`, `.github/workflows/build.yml`, `docs/linux-install.md`.

## [important] Unpinned runtime download at package time
`packaging/linux/build-appimage.sh:31` — appimagetool 1.9.x downloads the *latest* type2 AppImage runtime from GitHub while packaging (upstream README: "this version downloads the latest AppImage runtime"). That is an unpinned network fetch and makes builds non-reproducible.
**Fix:** download `https://github.com/AppImage/type2-runtime/releases/download/<pinned tag>/runtime-x86_64` next to the tool, with a **real** sha256 (fetch the file now in your sandbox and compute it; do not invent a hash; record the tag and the date in a comment), verify it, and pass `--runtime-file "${DIST}/runtime-x86_64"` to appimagetool. Keep `set -euo pipefail`.

## [minor]
- `packaging/linux/nfpm.yaml:5` — `maintainer` needs `Name <email>` form for dpkg/lintian: use `ArcSoft Labs <arcsoftlabs@users.noreply.github.com>`.
- `packaging/mouseshare.spec:27` — `gi.overrides.WebKit2` does not exist in PyGObject; remove it from the hiddenimports (PyInstaller would only warn, but a clean build log matters).
- `docs/linux-install.md:61-62` — the WSLg statement is now **verified**: on this machine (`DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, `XDG_SESSION_TYPE` unset) `session_type()` returns `x11`, listeners start, grab/warp/ungrab succeed on the XWayland server. Reword the doc to state that (X11 path works under WSLg; whether the grab confines real input under WSLg is untested).

## Check command (must exit 0)
```
ruff check packaging tools && bash -n packaging/linux/build-appimage.sh && grep -q "runtime-file" packaging/linux/build-appimage.sh && grep -q "^maintainer: .*<.*>" packaging/linux/nfpm.yaml
```

## Report
Per item: what changed (file:line); the runtime tag and sha256 you pinned and how you obtained it.
