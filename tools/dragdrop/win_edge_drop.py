#!/usr/bin/env python3
"""Probe Windows file drops at the primary monitor's right edge.

Run from a Windows terminal at the repository root:

    py.exe tools/dragdrop/win_edge_drop.py

For the first attempt, drag one or more files from Explorer to the right edge,
hover over the invisible 24 px strip, and release. For the second attempt, drag
again, hover at the edge, and KEEP HOLDING: after one second the probe warps the
cursor 48 px beyond the primary monitor's right edge. Observe whether Explorer's
drag image/session survives, move back to the strip if possible, and release.

The window exists for at most 60 seconds and makes no persistent changes.
"""

import ctypes
import platform
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("This probe must be run on Windows with py.exe")

try:
    from screeninfo import get_monitors
except ImportError as exc:
    raise SystemExit("screeninfo is required: py.exe -m pip install screeninfo") from exc


EDGE_WIDTH = 24
RUN_SECONDS = 60.0
WARP_DELAY = 1.0
CF_HDROP = 15
DVASPECT_CONTENT = 1
TYMED_HGLOBAL = 1
DROPEFFECT_NONE = 0
DROPEFFECT_COPY = 1
S_OK = 0
E_NOINTERFACE = 0x80004002
E_NOTIMPL = 0x80004001
WM_DESTROY = 0x0002
WM_DROPFILES = 0x0233
WM_TIMER = 0x0113
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x00000002
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
shell32.DragAcceptFiles.restype = None
shell32.DragFinish.argtypes = [wintypes.HANDLE]
shell32.DragFinish.restype = None
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
ole32.OleInitialize.restype = ctypes.c_long
ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.c_void_p]
ole32.RegisterDragDrop.restype = ctypes.c_long
ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
ole32.RevokeDragDrop.restype = ctypes.c_long


def stamp(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def result(message):
    stamp("RESULT " + message)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


IID_IUNKNOWN = GUID(0x00000000, 0x0000, 0x0000, (ctypes.c_ubyte * 8)(
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46
))
IID_IDROPTARGET = GUID(0x00000122, 0x0000, 0x0000, (ctypes.c_ubyte * 8)(
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46
))


class POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", wintypes.WORD),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", wintypes.DWORD),
        ("lindex", wintypes.LONG),
        ("tymed", wintypes.DWORD),
    ]


class STGMEDIUM_UNION(ctypes.Union):
    _fields_ = [
        ("hBitmap", wintypes.HBITMAP),
        ("hMetaFilePict", ctypes.c_void_p),
        ("hEnhMetaFile", ctypes.c_void_p),
        ("hGlobal", wintypes.HGLOBAL),
        ("lpszFileName", wintypes.LPWSTR),
        ("pstm", ctypes.c_void_p),
        ("pstg", ctypes.c_void_p),
    ]


class STGMEDIUM(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("tymed", wintypes.DWORD),
        ("u", STGMEDIUM_UNION),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
ole32.ReleaseStgMedium.restype = None


QueryInterfaceType = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
)
AddRefType = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
ReleaseType = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
DragEnterType = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    POINTL, ctypes.POINTER(wintypes.DWORD)
)
DragOverType = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, wintypes.DWORD, POINTL, ctypes.POINTER(wintypes.DWORD)
)
DragLeaveType = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
DropType = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    POINTL, ctypes.POINTER(wintypes.DWORD)
)


class IDropTargetVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", QueryInterfaceType),
        ("AddRef", AddRefType),
        ("Release", ReleaseType),
        ("DragEnter", DragEnterType),
        ("DragOver", DragOverType),
        ("DragLeave", DragLeaveType),
        ("Drop", DropType),
    ]


class IDropTarget(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(IDropTargetVTable))]


def guid_equal(left, right):
    return ctypes.string_at(left, ctypes.sizeof(GUID)) == bytes(right)


def hdrop_files(handle):
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR,
                                      wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    paths = []
    for index in range(count):
        length = shell32.DragQueryFileW(handle, index, None, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        shell32.DragQueryFileW(handle, index, buffer, len(buffer))
        paths.append(buffer.value)
    return paths


def data_object_files(data_object):
    """Request CF_HDROP from IDataObject and release the returned medium."""
    if not data_object:
        return []
    vtable = ctypes.cast(data_object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    get_data = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(FORMATETC),
        ctypes.POINTER(STGMEDIUM)
    )(vtable[3])
    fmt = FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
    medium = STGMEDIUM()
    hr = get_data(data_object, ctypes.byref(fmt), ctypes.byref(medium))
    result("IDataObject.GetData(CF_HDROP)=0x%08X" % (hr & 0xFFFFFFFF))
    if hr != S_OK:
        return []
    try:
        return hdrop_files(medium.hGlobal) if medium.tymed == TYMED_HGLOBAL else []
    finally:
        ole32.ReleaseStgMedium(ctypes.byref(medium))


class DropTarget:
    def __init__(self, app):
        self.app = app
        self.refcount = 1
        self._callbacks = (
            QueryInterfaceType(self.query_interface), AddRefType(self.add_ref),
            ReleaseType(self.release), DragEnterType(self.drag_enter),
            DragOverType(self.drag_over), DragLeaveType(self.drag_leave),
            DropType(self.drop),
        )
        self.vtable = IDropTargetVTable(*self._callbacks)
        self.interface = IDropTarget(ctypes.pointer(self.vtable))

    def query_interface(self, this, iid, output):
        if guid_equal(iid, IID_IUNKNOWN) or guid_equal(iid, IID_IDROPTARGET):
            output[0] = ctypes.cast(ctypes.pointer(self.interface), ctypes.c_void_p)
            self.add_ref(this)
            return S_OK
        output[0] = None
        return E_NOINTERFACE

    def add_ref(self, _this):
        self.refcount += 1
        return self.refcount

    def release(self, _this):
        self.refcount = max(0, self.refcount - 1)
        return self.refcount

    def drag_enter(self, _this, _data, _keys, point, effect):
        self.app.drag_active = True
        self.app.warped_this_drag = False
        self.app.drag_enter_at = time.monotonic()
        effect[0] = DROPEFFECT_COPY
        stamp("DRAGENTER")
        return S_OK

    def drag_over(self, _this, _keys, point, effect):
        effect[0] = DROPEFFECT_COPY
        stamp("DRAGOVER %d,%d" % (point.x, point.y))
        self.app.maybe_warp(point.y)
        return S_OK

    def drag_leave(self, _this):
        self.app.drag_active = False
        stamp("DRAGLEAVE")
        if self.app.warped_this_drag:
            result("warp caused DragLeave; visually confirm whether Explorer drag survived")
        return S_OK

    def drop(self, _this, data, _keys, point, effect):
        files = data_object_files(data)
        effect[0] = DROPEFFECT_COPY if files else DROPEFFECT_NONE
        self.app.drag_active = False
        self.app.com_drops += 1
        stamp("DROP %d files: %s" % (len(files), files))
        result("IDropTarget_drop_received=True files=%d" % len(files))
        if self.app.warped_this_drag:
            result("warp_drag_survived_to_drop=True")
        return S_OK


class App:
    def __init__(self, monitor):
        self.monitor = monitor
        self.hwnd = None
        self.drag_active = False
        self.drag_enter_at = 0.0
        self.warped_this_drag = False
        self.legacy_drops = 0
        self.com_drops = 0
        self.com_registered = False
        self.target = DropTarget(self)

    def maybe_warp(self, y):
        if self.warped_this_drag or time.monotonic() - self.drag_enter_at < WARP_DELAY:
            return
        self.warped_this_drag = True
        requested_x = self.monitor.x + self.monitor.width + 48
        ok = bool(user32.SetCursorPos(requested_x, int(y)))
        actual = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(actual))
        result("warp SetCursorPos=%s requested=(%d,%d) actual=(%d,%d)" % (
            ok, requested_x, y, actual.x, actual.y
        ))
        result("warp_drag_survival=PENDING (DragLeave alone means target exit, not cancellation)")


APP = None
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


@WNDPROC
def window_proc(hwnd, message, wparam, lparam):
    if message == WM_DROPFILES:
        files = hdrop_files(wparam)
        APP.legacy_drops += 1
        stamp("DROP %d files: %s" % (len(files), files))
        result("WM_DROPFILES_received=True files=%d" % len(files))
        shell32.DragFinish(wparam)
        return 0
    if message == WM_TIMER:
        user32.KillTimer(hwnd, 1)
        if APP.com_registered:
            revoke_hr = ole32.RevokeDragDrop(hwnd)
            result("RevokeDragDrop=0x%08X" % (revoke_hr & 0xFFFFFFFF))
            APP.com_registered = False
        shell32.DragAcceptFiles(hwnd, False)
        user32.DestroyWindow(hwnd)
        return 0
    if message == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


def primary_monitor():
    monitors = list(get_monitors())
    for monitor in monitors:
        if getattr(monitor, "is_primary", False):
            return monitor
    if not monitors:
        raise RuntimeError("screeninfo returned no monitors")
    return monitors[0]


def main():
    global APP
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    else:
        user32.SetProcessDPIAware()
    monitor = primary_monitor()
    APP = App(monitor)
    result("windows=%s python=%s" % (platform.version(), platform.python_version()))
    result("primary x=%d y=%d w=%d h=%d edge_width=%d" % (
        monitor.x, monitor.y, monitor.width, monitor.height, EDGE_WIDTH
    ))

    hr = ole32.OleInitialize(None)
    result("OleInitialize=0x%08X" % (hr & 0xFFFFFFFF))
    if hr not in (S_OK, 1):
        sys.exit("OleInitialize failed: 0x%08X" % (hr & 0xFFFFFFFF))

    instance = kernel32.GetModuleHandleW(None)
    class_name = "MouseShareEdgeDropProbe"
    wc = WNDCLASSW(0, window_proc, 0, 0, instance, None, None, None, None, class_name)
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        raise ctypes.WinError()
    x = monitor.x + monitor.width - EDGE_WIDTH
    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
        class_name, class_name, WS_POPUP, x, monitor.y, EDGE_WIDTH, monitor.height,
        None, None, instance, None
    )
    if not hwnd:
        raise ctypes.WinError()
    APP.hwnd = hwnd
    user32.SetLayeredWindowAttributes(hwnd, 0, 1, LWA_ALPHA)
    user32.SetWindowPos(
        hwnd, HWND_TOPMOST, x, monitor.y, EDGE_WIDTH, monitor.height,
        SWP_NOACTIVATE | SWP_SHOWWINDOW
    )
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    shell32.DragAcceptFiles(hwnd, True)
    result("DragAcceptFiles_registered=True")
    target_pointer = ctypes.cast(ctypes.pointer(APP.target.interface), ctypes.c_void_p)
    register_hr = ole32.RegisterDragDrop(hwnd, target_pointer)
    result("RegisterDragDrop=0x%08X" % (register_hr & 0xFFFFFFFF))
    if register_hr != S_OK:
        result("IDropTarget_registered=False (WM_DROPFILES remains active)")
    else:
        APP.com_registered = True
        result("IDropTarget_registered=True")

    user32.SetTimer(hwnd, 1, int(RUN_SECONDS * 1000), None)
    stamp("READY: drag from Explorer to the primary monitor's right edge")
    message = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if APP.com_registered:
            revoke_hr = ole32.RevokeDragDrop(hwnd)
            result("RevokeDragDrop=0x%08X" % (revoke_hr & 0xFFFFFFFF))
            APP.com_registered = False
        shell32.DragAcceptFiles(hwnd, False)
        ole32.OleUninitialize()
    result("SUMMARY WM_DROPFILES=%d IDropTarget=%d" % (APP.legacy_drops, APP.com_drops))
    result("hover_without_release_delivers_drop=False (drop requires release)")


if __name__ == "__main__":
    main()
