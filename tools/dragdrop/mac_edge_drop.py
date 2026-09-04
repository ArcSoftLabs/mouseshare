#!/usr/bin/env python3
"""Probe macOS file drops at the main screen's right edge.

Run in Terminal at the repository root (PyObjC is installed with pynput):

    python3 tools/dragdrop/mac_edge_drop.py

First drag one or more files from Finder to the right edge, hover over the
invisible 24-point strip, and release. Then repeat and KEEP HOLDING over the
strip: after one second the probe warps the cursor 48 points beyond the main
screen's right edge. Observe whether Finder's drag image/session survives,
return to the strip if possible, and release. The probe exits after 60 seconds.
"""

import platform
import sys
import time

if sys.platform != "darwin":
    sys.exit("This probe must be run on macOS")

try:
    import objc
    import Quartz
    from AppKit import (
        NSURL,
        NSApp,
        NSApplication,
        NSBackingStoreBuffered,
        NSColor,
        NSDragOperationCopy,
        NSFilenamesPboardType,
        NSMakeRect,
        NSObject,
        NSScreen,
        NSStatusWindowLevel,
        NSView,
        NSWindow,
        NSWindowStyleMaskBorderless,
    )
    from Foundation import NSTimer
except ImportError as exc:
    sys.exit("PyObjC frameworks missing (%s). Install with: python3 -m pip install pynput" % exc)


EDGE_WIDTH = 24.0
RUN_SECONDS = 60.0
WARP_DELAY = 1.0
FILE_URL_TYPE = "public.file-url"


def stamp(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def result(message):
    stamp("RESULT " + message)


class EdgeDropView(NSView):
    window = objc.ivar()
    warp_x = objc.ivar()
    entered_at = objc.ivar()
    warped_this_drag = objc.ivar()
    drops = objc.ivar()

    def initWithFrame_window_warpX_(self, frame, window, warp_x):
        self = objc.super(EdgeDropView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.window = window
        self.warp_x = warp_x
        self.entered_at = 0.0
        self.warped_this_drag = False
        self.drops = 0
        self.registerForDraggedTypes_([NSFilenamesPboardType, FILE_URL_TYPE])
        return self

    def draggingEntered_(self, sender):
        self.entered_at = time.monotonic()
        self.warped_this_drag = False
        point = sender.draggingLocation()
        stamp("DRAGENTER %.1f,%.1f" % (point.x, point.y))
        return NSDragOperationCopy

    def draggingUpdated_(self, sender):
        point = sender.draggingLocation()
        stamp("DRAGOVER %.1f,%.1f" % (point.x, point.y))
        if not self.warped_this_drag and time.monotonic() - self.entered_at >= WARP_DELAY:
            self.warped_this_drag = True
            current = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            requested = (float(self.warp_x), current.y)
            error = Quartz.CGWarpMouseCursorPosition(requested)
            actual = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            result("warp CGWarpMouseCursorPosition=%s requested=(%.1f,%.1f) actual=(%.1f,%.1f)" % (
                error, requested[0], requested[1], actual.x, actual.y
            ))
            result("warp_drag_survival=PENDING (draggingExited alone means target exit, not cancellation)")
        return NSDragOperationCopy

    def draggingExited_(self, _sender):
        stamp("DRAGLEAVE")
        if self.warped_this_drag:
            result("warp caused draggingExited; visually confirm whether Finder drag survived")

    def prepareForDragOperation_(self, _sender):
        return True

    def performDragOperation_(self, sender):
        pasteboard = sender.draggingPasteboard()
        paths = pasteboard.propertyListForType_(NSFilenamesPboardType) or []
        if not paths:
            options = {"NSPasteboardURLReadingFileURLsOnlyKey": True}
            urls = pasteboard.readObjectsForClasses_options_([NSURL], options) or []
            paths = [url.path() or url.absoluteString() for url in urls]
        self.drops += 1
        stamp("DROP %d files: %s" % (len(paths), list(paths)))
        result("performDragOperation=True files=%d" % len(paths))
        if self.warped_this_drag:
            result("warp_drag_survived_to_drop=True")
        return bool(paths)


class ProbeDelegate(NSObject):
    view = objc.ivar()

    def stop_(self, _timer):
        result("SUMMARY drops=%d" % self.view.drops)
        result("hover_without_release_delivers_drop=False (drop requires release)")
        NSApp.stop_(None)


def main():
    app = NSApplication.sharedApplication()
    screen = NSScreen.mainScreen() or NSScreen.screens()[0]
    frame = screen.frame()
    edge_frame = NSMakeRect(
        frame.origin.x + frame.size.width - EDGE_WIDTH,
        frame.origin.y,
        EDGE_WIDTH,
        frame.size.height,
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        edge_frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    window.setLevel_(NSStatusWindowLevel)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setAlphaValue_(0.01)
    window.setIgnoresMouseEvents_(False)
    window.setReleasedWhenClosed_(False)
    content_frame = NSMakeRect(0.0, 0.0, EDGE_WIDTH, frame.size.height)
    warp_x = frame.origin.x + frame.size.width + 48.0
    view = EdgeDropView.alloc().initWithFrame_window_warpX_(content_frame, window, warp_x)
    window.setContentView_(view)
    window.orderFrontRegardless()

    delegate = ProbeDelegate.alloc().init()
    delegate.view = view
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        RUN_SECONDS, delegate, "stop:", None, False
    )
    del timer
    result("macos=%s python=%s" % (platform.mac_ver()[0], platform.python_version()))
    result("main_screen x=%.1f y=%.1f w=%.1f h=%.1f edge_width=%.1f" % (
        frame.origin.x, frame.origin.y, frame.size.width, frame.size.height, EDGE_WIDTH
    ))
    result("registered_types=%s" % [NSFilenamesPboardType, FILE_URL_TYPE])
    stamp("READY: drag from Finder to the main screen's right edge")
    app.run()
    window.close()


if __name__ == "__main__":
    main()
