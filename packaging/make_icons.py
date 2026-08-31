"""Draw the application icon at every size the two platforms ask for.

The mark is two rounded panels overlapping, with the region they share cut
back to the tile so the shapes read as joined rather than stacked -- one
machine, another machine, and the input passing between them.

Each size is drawn at eight times its final resolution and reduced, rather
than one large drawing being scaled down repeatedly. At 16 pixels a panel
is only seven across, and geometry that was rounded once at 1024 does not
land on that grid cleanly.

The two panels are deliberately far apart in lightness, not just in hue.
Side by side at 16 pixels, two colours of similar value merge into one
shape and the whole idea of the mark is lost; --check prints the contrast
so that stays a measured property rather than an opinion.

Usage:
    python packaging/make_icons.py          # write packaging/icons/
    python packaging/make_icons.py --check  # print the contrast figures
"""
import argparse
import os
import struct
import sys
from io import BytesIO

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icons")

# From the application's own stylesheet, with the two panels pushed apart
# in lightness: the accent deepened, the good-state green lifted.
TILE = (13, 14, 18, 255)        # --bg #0d0e12
BLUE = (47, 91, 224, 255)       # deepened from --accent #5b8cff
GREEN = (86, 230, 165, 255)     # lifted from --good #3ddc97

SUPERSAMPLE = 8

# Geometry in units of a 128-wide tile, so every size is the same drawing.
TILE_RADIUS = 28
PANEL = 60
PANEL_RADIUS = 15
# Together these span 16..112 of 128 -- about three quarters of the tile.
# Much less and the icon looks timid beside everything else in a dock.
BACK_AT = (16, 16)
FRONT_AT = (52, 52)
GAP = 5  # dark band left between the panels where they meet

ICNS_NAMES = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PNG_SIZES = sorted({s for s, _ in ICNS_NAMES} | set(ICO_SIZES))


def _rounded(size: int, box, radius: float) -> Image.Image:
    """A white-on-black mask of one rounded rectangle."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def render(size: int) -> Image.Image:
    """One icon, drawn large and reduced."""
    big = size * SUPERSAMPLE
    u = big / 128.0  # one unit of the 128-wide design

    def box(at, extent):
        x, y = at
        return (x * u, y * u, (x + extent) * u - 1, (y + extent) * u - 1)

    icon = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    tile = _rounded(big, (0, 0, big - 1, big - 1), TILE_RADIUS * u)
    icon.paste(TILE, (0, 0), tile)

    back = _rounded(big, box(BACK_AT, PANEL), PANEL_RADIUS * u)
    front = _rounded(big, box(FRONT_AT, PANEL), PANEL_RADIUS * u)
    icon.paste(BLUE, (0, 0), back)
    icon.paste(GREEN, (0, 0), front)

    # Cut the shared region back to the tile, widened by the gap, but only
    # where it falls inside the back panel -- so the front panel keeps its
    # own outline and a dark band separates the two.
    widened = _rounded(
        big,
        box((FRONT_AT[0] - GAP, FRONT_AT[1] - GAP), PANEL + GAP * 2),
        (PANEL_RADIUS + GAP) * u,
    )
    seam = Image.composite(widened, Image.new("L", (big, big), 0), back)
    icon.paste(TILE, (0, 0), seam)

    return icon.resize((size, size), Image.LANCZOS)


def _luminance(rgba) -> float:
    """WCAG relative luminance, for judging whether two panels separate."""
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgba[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check() -> int:
    """Print the contrast the mark actually has."""
    pairs = [
        ("panel against panel", BLUE, GREEN),
        ("back panel on tile", BLUE, TILE),
        ("front panel on tile", GREEN, TILE),
    ]
    worst = min(_ratio(a, b) for _, a, b in pairs)
    for name, a, b in pairs:
        print(f"{name:24s} {_ratio(a, b):5.2f} : 1")
    print(f"\nweakest separation: {worst:.2f} : 1")
    # Two panels below about 2:1 merge into a single blob at 16 pixels,
    # which is the whole failure this mark has to avoid.
    print("PASS" if worst >= 2.0 else "FAIL - the panels will merge when small")
    return 0 if worst >= 2.0 else 1


def write_ico(images, path: str) -> None:
    """A .ico with a PNG payload per size.

    Pillow's own ICO writer resamples from a single image; these are the
    per-size drawings instead, which is the point of the exercise.
    """
    payloads = []
    for size in ICO_SIZES:
        buf = BytesIO()
        images[size].save(buf, format="PNG")
        payloads.append((size, buf.getvalue()))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = len(header) + 16 * len(payloads)
    entries, blobs = b"", b""
    for size, data in payloads:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size, 0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset,
        )
        blobs += data
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="print contrast figures and exit")
    args = parser.parse_args()
    if args.check:
        return check()

    os.makedirs(OUT, exist_ok=True)
    iconset = os.path.join(OUT, "MouseShare.iconset")
    os.makedirs(iconset, exist_ok=True)

    images = {size: render(size) for size in PNG_SIZES}
    for size, name in ICNS_NAMES:
        images[size].save(os.path.join(iconset, name))
    images[1024].save(os.path.join(OUT, "icon.png"))
    write_ico(images, os.path.join(OUT, "MouseShare.ico"))

    print(f"wrote {OUT}")
    print(f"  MouseShare.ico      ({len(ICO_SIZES)} sizes, for Windows)")
    print(f"  MouseShare.iconset/ ({len(ICNS_NAMES)} files; iconutil turns "
          f"this into MouseShare.icns on a Mac)")
    print(f"  icon.png            (1024, for anywhere else)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
