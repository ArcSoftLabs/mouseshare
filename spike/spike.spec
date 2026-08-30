# PyInstaller spec for the v2 stack spike.
#
# Mirrors what the real app will need: bundled web assets, windowed build,
# and a real .app BUNDLE on macOS -- because a console EXE would not test
# the packaging question that matters.
import os
import sys

a = Analysis(
    [os.path.join(SPECPATH, "spike.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[(os.path.join(SPECPATH, "web"), "web")],
    hiddenimports=[
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "webview.platforms.cocoa",
        "pynput.keyboard._win32", "pynput.mouse._win32",
        "pynput.keyboard._darwin", "pynput.mouse._darwin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="spike",
    debug=False,
    strip=False,
    upx=False,
    # Console stays on so --smoke output is visible in CI logs; the real
    # app ships console=False.
    console=True,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="MouseShareSpike.app",
        bundle_identifier="com.arcsoftlabs.mouseshare.spike",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
            "NSAccessibilityUsageDescription":
                "MouseShare forwards your keyboard and mouse to your other computer.",
            "NSInputMonitoringUsageDescription":
                "MouseShare reads keyboard and mouse input to forward it to your other computer.",
        },
    )
