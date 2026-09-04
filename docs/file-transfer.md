# File transfer: native boundary-drop spike

Status: **awaiting Benjamin's runs** (Windows first with `py.exe`, then macOS in
Terminal). The scripts have been statically checked in the development environment,
which cannot exercise either native GUI drag manager.

## Question and current integration seam

The spike asks whether an invisible MouseShare-owned window at the source screen's
edge can become the native destination of an Explorer/Finder file drag, learn the file
list on release, and whether warping across that edge leaves the native drag alive.

Today `HostSession.on_move` crosses as soon as the cursor reaches a mapped edge.
`InputCapture.start_remote` then parks the source cursor and suppresses physical mouse
events. In particular, its Windows filter consumes the held button's eventual release,
and the macOS intercept deletes the corresponding real event. F2 cannot simply add file
transfer after that transition: it must recognize left-button drag intent and put the
edge destination in place *before* normal remote capture begins.

There are two distinct outcomes which should not be conflated:

- Hovering over a drop target does not disclose or transfer the files. A native drop is
  delivered only when the user releases the button.
- `DragLeave`/`draggingExited:` after a warp proves only that the pointer left our target.
  It does not prove the system drag was cancelled. The decisive evidence is whether the
  Explorer/Finder drag image remains and whether the same drag can subsequently produce
  a drop. The scripts print that distinction explicitly.

## Windows probe

Run:

```text
py.exe tools/dragdrop/win_edge_drop.py
```

[`tools/dragdrop/win_edge_drop.py`](../tools/dragdrop/win_edge_drop.py) creates a
24-physical-pixel, borderless, topmost tool window at the right edge of the primary
monitor selected from `screeninfo`. It opts the process into per-monitor DPI awareness
before reading geometry, so the 125% display below the 100% primary does not turn the
edge strip into scaled logical coordinates. The window uses `WS_EX_LAYERED` with alpha
1/255 and remains hit-testable without activation.

The same HWND tries both requested mechanisms:

1. `DragAcceptFiles(hwnd, TRUE)`, with `WM_DROPFILES`, `DragQueryFileW`, and
   `DragFinish`.
2. `OleInitialize` followed by a ctypes `IDropTarget` vtable and `RegisterDragDrop`.
   `DragEnter` and `DragOver` advertise `DROPEFFECT_COPY`; `Drop` asks the
   `IDataObject` for `CF_HDROP`/`TYMED_HGLOBAL`, enumerates it with `DragQueryFileW`,
   then calls `ReleaseStgMedium`.

After one second of `DragOver`, the script calls `SetCursorPos` 48 pixels beyond the
primary's right boundary and reports the requested and actual positions. Windows may
clamp that coordinate when no display occupies it, so both values matter. A timer
destroys the window after 60 seconds; `RevokeDragDrop`, `DragAcceptFiles(FALSE)`, and
`OleUninitialize` clean up process-local registration.

### Windows results to capture

Awaiting a real run. Paste the emitted `RESULT` lines here, especially:

```text
RESULT OleInitialize=...
RESULT DragAcceptFiles_registered=True
RESULT RegisterDragDrop=...
RESULT IDataObject.GetData(CF_HDROP)=...
RESULT WM_DROPFILES_received=... / IDropTarget_drop_received=...
RESULT warp SetCursorPos=... requested=... actual=...
RESULT warp_drag_survived_to_drop=... (if observed)
RESULT SUMMARY WM_DROPFILES=... IDropTarget=...
```

Interpretation:

- A nonzero `WM_DROPFILES` count establishes mechanism (a). A nonzero `IDropTarget`
  count plus successful `GetData` establishes mechanism (b). If COM registration
  succeeds and Explorer chooses OLE, it is normal for only mechanism (b) to receive the
  drop.
- Moving to the edge while continuing to hold must print drag-enter/over events but no
  `DROP`; this is an inherent UX constraint, not probe failure.
- Record the visual answer to the script's `PENDING` warp line. A later
  `warp_drag_survived_to_drop=True` is stronger machine-observed evidence that the same
  session survived.

## macOS probe

Run:

```text
python3 tools/dragdrop/mac_edge_drop.py
```

[`tools/dragdrop/mac_edge_drop.py`](../tools/dragdrop/mac_edge_drop.py) creates a
borderless `NSWindow` at `NSStatusWindowLevel`, alpha 0.01, accepting mouse events over
the rightmost 24 points of `NSScreen.mainScreen()`. Its `NSView` registers both legacy
`NSFilenamesPboardType` and `public.file-url`. `draggingEntered:` and
`draggingUpdated:` return `NSDragOperationCopy`; `performDragOperation:` first reads the
legacy filename property list and then falls back to file `NSURL` objects.

After one second over the strip it calls `CGWarpMouseCursorPosition` 48 points beyond
the main screen's right edge, preserving the cursor's current Quartz y coordinate. The
main-thread AppKit run loop ends through an `NSTimer` after 60 seconds.

### macOS results to capture

Awaiting a real run. Paste the `RESULT` lines and note whether Finder's drag image stays
attached after the warp:

```text
RESULT registered_types=...
RESULT performDragOperation=True files=...
RESULT warp CGWarpMouseCursorPosition=... requested=... actual=...
RESULT warp_drag_survived_to_drop=... (if observed)
RESULT SUMMARY drops=...
```

As on Windows, hover alone cannot reveal the pasteboard payload through a completed
drop. `draggingExited:` is expected when a warp moves the pointer away; the visual drag
image or a subsequent successful drop decides whether the session itself survived.

## Decision recommendation for F2

No platform feasibility claim is justified until the two hands-on runs are recorded.
Use these evidence gates rather than treating successful target registration as enough:

- **Native boundary drop is feasible on a platform** if Explorer/Finder can release on
  the invisible strip, the callback returns the complete file list, and the UX of
  releasing at the source boundary is acceptable. F2's exact sequence is: detect a
  local left-button drag approaching a mapped edge; show the platform edge target
  before `HostSession` crosses; keep local native capture active; on release, accept the
  copy drop and collect paths; hide/revoke the target; start the transfer to the mapped
  device; only then enter MouseShare remote mode (with no button held) and show transfer
  progress. This is a boundary drop followed by a cursor crossing, not one continuous
  native drag onto a remote Finder/Explorer destination.
- **A continuous cross-boundary native drag is feasible** only if the warp experiment
  also shows the OS drag remains alive and there is a defined destination-side native
  drag handoff. These source-only probes can demonstrate survival, but cannot by
  themselves establish a cross-machine OLE/NSDraggingSession handoff. F2 must not claim
  remote-folder drop semantics based solely on a surviving drag image.
- **Fallback on either platform** if its edge target does not receive complete paths,
  registration fails in the supported runtime, or edge-release UX is rejected: accept
  files in a visible MouseShare drop zone and offer **Send to <device>**. Use the same
  F1 transfer protocol and progress UI; do not suppress or warp the native drag.

The unavoidable UX consequence is now explicit: the source edge window learns the
files only after release. Merely pushing a held file drag against the edge cannot both
give MouseShare the file list and transparently continue that same drop on the remote
desktop without additional platform-specific drag-session handoff evidence.
