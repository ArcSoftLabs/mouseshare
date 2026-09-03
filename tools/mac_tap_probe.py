#!/usr/bin/env python3
"""Probe how a kCGSessionEventTap that deletes mouse-moved events behaves.

Run on macOS with pynput installed (it brings pyobjc's Quartz/AppKit):

    python3 tools/mac_tap_probe.py

Three phases, each capped at 15 s, each restoring the tap and the mouse
association afterwards. Lines starting with "RESULT " are the findings.
H1: the cursor sprite keeps moving even though the tap deletes the event.
H2: warping from inside the callback gets the tap disabled by timeout.
"""
import os
import platform
import sys
import time

try:
    import Quartz
    from AppKit import NSEvent, NSScreen
    from ApplicationServices import AXIsProcessTrusted
except ImportError as exc:
    sys.exit("pyobjc frameworks missing (%s). Install with: "
             "python3 -m pip install pynput" % exc)

PHASE_CAP = 15.0        # hard cap: the tap is torn down after this, no matter what
MOVE_SECONDS = 6.0      # ~5 s of movement plus slack
DISABLED_BY_TIMEOUT = 0xFFFFFFFE
DISABLED_BY_USER = 0xFFFFFFFF
MAGIC = 0x4D534850      # marks the warps this script posts itself
MASK = sum(1 << t for t in (Quartz.kCGEventMouseMoved,
                            Quartz.kCGEventLeftMouseDragged,
                            Quartz.kCGEventRightMouseDragged,
                            Quartz.kCGEventOtherMouseDragged))


def result(line):
    print("RESULT " + line, flush=True)


def cursor_now():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return (p.x, p.y)


def preflight():
    result("macos=%s python=%s" % (platform.mac_ver()[0], platform.python_version()))
    trusted = bool(AXIsProcessTrusted())
    result("accessibility_trusted=%s" % trusted)
    if hasattr(Quartz, "CGPreflightListenEventAccess"):
        result("input_monitoring=%s" % bool(Quartz.CGPreflightListenEventAccess()))
    if not trusted:
        print("Accessibility is not granted, so no event tap can be created.")
        print("Open System Settings > Privacy & Security > Accessibility and add the")
        print("app that runs this script (Terminal or iTerm, not python3), then rerun.")
        sys.exit(2)
    main = NSScreen.mainScreen() or NSScreen.screens()[0]
    result("backing_scale=%s" % main.backingScaleFactor())
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    for d in list(ids or [])[:count]:
        b = Quartz.CGDisplayBounds(d)
        result("CGDisplayBounds id=%s x=%g y=%g w=%g h=%g" % (
            d, b.origin.x, b.origin.y, b.size.width, b.size.height))
    for s in NSScreen.screens():
        f = s.frame()
        result("NSScreen.frame x=%g y=%g w=%g h=%g" % (
            f.origin.x, f.origin.y, f.size.width, f.size.height))
    r = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return (r.origin.x + r.size.width / 2, r.origin.y + r.size.height / 2)


class Phase:
    def __init__(self, name, anchor, warp_back=False):
        self.name, self.anchor, self.warp_back = name, anchor, warp_back
        self.tap = None
        self.events = []        # (t, x, y, dx, dy, ns_x, ns_y)
        self.timeouts = self.user_disables = self.mine = self.mine_with_pid = 0
        self.started = self.ended = 0.0

    def callback(self, proxy, event_type, event, refcon):
        if event_type in (DISABLED_BY_TIMEOUT, DISABLED_BY_USER):
            if event_type == DISABLED_BY_TIMEOUT:
                self.timeouts += 1
            else:
                self.user_disables += 1
            if self.tap is not None:
                Quartz.CGEventTapEnable(self.tap, True)
            return event
        pid = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID)
        tag = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
        if tag == MAGIC or pid == os.getpid():
            # Our own warp re-entering the tap. Let it through, or Phase 3
            # would feed on itself and any timeout would be self-inflicted.
            self.mine += 1
            if pid == os.getpid():
                self.mine_with_pid += 1
            return event
        loc = Quartz.CGEventGetLocation(event)
        dx = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX)
        dy = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY)
        ns = NSEvent.mouseLocation()
        self.events.append((time.monotonic(), loc.x, loc.y, dx, dy, ns.x, ns.y))
        if self.warp_back:
            # Same call pynput's Controller.position setter makes, plus a tag.
            ev = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, self.anchor, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, MAGIC)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return None  # delete the real event, as capture.py does

    def run(self, instruction):
        print("\n=== %s ===\n%s" % (self.name, instruction))
        for n in (3, 2, 1):
            print("starting in %d..." % n, flush=True)
            time.sleep(1)
        Quartz.CGWarpMouseCursorPosition(self.anchor)
        time.sleep(0.3)  # the warp briefly suppresses hardware events
        # A tap is live from creation; service it at once or it times out.
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, MASK, self.callback, None)
        if self.tap is None:
            result("%s tap_created=False (Input Monitoring or Accessibility missing?)" % self.name)
            return False
        src = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(self.tap, True)
        self.started = time.monotonic()
        deadline = self.started + min(MOVE_SECONDS, PHASE_CAP)
        print("GO - move now.", flush=True)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, min(0.25, remaining), False)
        finally:
            self.ended = time.monotonic()
            Quartz.CGEventTapEnable(self.tap, False)
            if hasattr(Quartz, "CFRunLoopRemoveSource"):
                Quartz.CFRunLoopRemoveSource(loop, src, Quartz.kCFRunLoopDefaultMode)
            Quartz.CFRunLoopStop(loop)
            print("phase over, stop moving.", flush=True)
        return True

    def report(self):
        n, ax, ay = len(self.events), self.anchor[0], self.anchor[1]
        top_h = NSScreen.screens()[0].frame().size.height  # NSEvent y is bottom-up
        offs = [max(abs(x - ax), abs(y - ay)) for (_, x, y, _, _, _, _) in self.events]
        ns_offs = [max(abs(nx - ax), abs((top_h - ny) - ay))
                   for (_, _, _, _, _, nx, ny) in self.events]
        max_off = max(offs) if offs else 0.0
        max_ns = max(ns_offs) if ns_offs else 0.0
        sum_dx = sum(e[3] for e in self.events)
        sum_dy = sum(e[4] for e in self.events)
        nonzero = sum(1 for e in self.events if e[3] or e[4])
        times = [e[0] for e in self.events]
        gaps = [b - a for a, b in zip(times, times[1:])]
        max_gap = max(gaps) if gaps else 0.0
        tail = (self.ended - times[-1]) if times else (self.ended - self.started)
        after = cursor_now()
        result("%s events=%d timeouts=%d user_disables=%d own_warps=%d own_warps_with_pid=%d"
               % (self.name, n, self.timeouts, self.user_disables, self.mine, self.mine_with_pid))
        result("%s anchor=(%g, %g) max_location_offset=%.1f max_nsevent_offset=%.1f"
               % (self.name, ax, ay, max_off, max_ns))
        result("%s delta_sum=(%d, %d) events_with_nonzero_delta=%d"
               % (self.name, sum_dx, sum_dy, nonzero))
        result("%s max_gap_s=%.2f tail_s=%.2f stalled=%s"
               % (self.name, max_gap, tail, max_gap > 2.0 or tail > 2.0))
        result("%s cursor_after=(%g, %g)" % (self.name, after[0], after[1]))
        for e in self.events[:3]:
            result("%s first_event loc=(%g, %g) delta=(%d, %d) nsevent=(%g, %g)"
                   % (self.name, e[1], e[2], e[3], e[4], e[5], e[6]))
        return n, max_off, nonzero

    def h1_verdict(self, n, max_off):
        if n == 0 or 5 < max_off <= 20:
            return "UNKNOWN"
        return "CONFIRMED" if max_off > 20 else "REFUTED"


def main():
    anchor = preflight()
    result("anchor=(%g, %g)" % anchor)
    h1, h2, delta = "UNKNOWN", "UNKNOWN", "unusable"
    try:
        p1 = Phase("phase1_delete_only", anchor)
        if p1.run("Move the mouse STEADILY TO THE RIGHT for about 5 seconds."):
            n, max_off, nonzero = p1.report()
            h1 = p1.h1_verdict(n, max_off)
            delta = "usable" if nonzero else "unusable"
            result("phase1 H1=%s DELTA_FIELDS=%s" % (h1, delta))

        p2 = Phase("phase2_decoupled", anchor)
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        try:
            ran = p2.run("Mouse decoupled from cursor. Move STEADILY TO THE RIGHT again, ~5 s.")
        finally:
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        if ran:
            n, max_off, nonzero = p2.report()
            result("phase2 location_stays_at_anchor=%s deltas_still_arrive=%s"
                   % (n > 0 and max_off <= 5, nonzero > 0))

        p3 = Phase("phase3_warp_in_callback", anchor, warp_back=True)
        if p3.run("Move the mouse QUICKLY in all directions for about 5 seconds."):
            n, _, _ = p3.report()
            if p3.timeouts > 0:
                h2 = "CONFIRMED"
            elif n > 0:
                h2 = "REFUTED"
            result("phase3 H2=%s" % h2)
    finally:
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        result("SUMMARY H1=%s H2=%s DELTA_FIELDS=%s" % (h1, h2, delta))


if __name__ == "__main__":
    main()
