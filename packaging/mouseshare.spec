# PyInstaller spec: one-file executable for macOS and Windows.
# Build with:  pyinstaller packaging/mouseshare.spec
import os

block_cipher = None

a = Analysis(
    [os.path.join("packaging", "entry.py")],
    pathex=[os.getcwd()],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pynput.keyboard._win32", "pynput.mouse._win32",
        "pynput.keyboard._darwin", "pynput.mouse._darwin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="mouseshare",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
