# PyInstaller spec: windowed app for macOS and Windows.
# Build with:  pyinstaller packaging/mouseshare.spec
import os
import sys

ROOT = os.path.join(SPECPATH, "..")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=[],
    # The UI ships as data and is resolved through sys._MEIPASS at runtime.
    datas=[(os.path.join(ROOT, "mouseshare", "ui", "web"), "mouseshare/ui/web")],
    hiddenimports=[
        "pynput.keyboard._win32", "pynput.mouse._win32",
        "pynput.keyboard._darwin", "pynput.mouse._darwin",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "webview.platforms.cocoa",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="MouseShare" if sys.platform == "darwin" else "mouseshare",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # it has a window of its own now
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="MouseShare.app",
        # Stable, because macOS ties Accessibility and Input Monitoring
        # approval to the bundle identity. Changing it means re-approving.
        bundle_identifier="com.arcsoftlabs.mouseshare",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.2.0",
            "NSAccessibilityUsageDescription":
                "MouseShare forwards your keyboard and mouse to your other computer.",
            "NSInputMonitoringUsageDescription":
                "MouseShare reads keyboard and mouse input so it can forward it "
                "to your other computer.",
            "NSLocalNetworkUsageDescription":
                "MouseShare finds your other computer on the local network.",
            "NSBonjourServices": ["_mouseshare._tcp"],
        },
    )
