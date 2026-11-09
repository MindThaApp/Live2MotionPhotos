#!/usr/bin/env python3
"""Generate the app icon, MSIX tile/logo assets and Store listing logos with Pillow.

    python packaging/windows/make_assets.py <new-output-folder>

Writes <out>/Assets/*.png (MSIX, with scale-* / targetsize-* variants for makepri),
<out>/app.ico (exe icon) and <out>/store/*.png (Partner Center logos). The output folder
must not exist yet or be empty, and every file is created exclusively: nothing is ever
overwritten. The logo is drawn in code (the project had none): a rounded photo frame
with a play mark and motion arcs, in the web UI's blue/purple.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (31, 111, 235)      # --accent in index.html
PURPLE = (188, 140, 255)   # --purple in index.html
WHITE = (255, 255, 255, 255)
MASTER = 1024
SCALES = (100, 125, 150, 200, 400)
TARGET_SIZES = (16, 24, 32, 48, 256)


def master_icon(size: int = MASTER) -> Image.Image:
    """Full-bleed square icon, drawn 4x and downsampled for smooth edges."""
    s = size * 4
    g = 256    # diagonal gradient drawn small, then scaled up (smooth, and fast)
    grad = Image.new("RGBA", (g, g))
    px = grad.load()
    for y in range(g):
        for x in range(g):
            t = (x + y) / (2 * (g - 1))
            px[x, y] = tuple(int(BLUE[i] + (PURPLE[i] - BLUE[i]) * t) for i in range(3)) + (255,)
    grad = grad.resize((s, s), Image.BICUBIC)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=255)
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(icon)
    w = int(s * 0.045)
    # photo frame
    fx0, fy0, fx1, fy1 = int(s * 0.20), int(s * 0.24), int(s * 0.74), int(s * 0.76)
    d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=int(s * 0.07), outline=WHITE, width=w)
    # play triangle in the frame
    cx, cy, r = (fx0 + fx1) / 2, (fy0 + fy1) / 2, s * 0.12
    d.polygon([(cx - r * 0.7, cy - r), (cx - r * 0.7, cy + r), (cx + r * 1.0, cy)], fill=WHITE)
    # motion arcs to the right of the frame
    for i, k in enumerate((0.10, 0.17)):
        box = (fx1 - s * 0.30 + s * k, cy - s * (0.16 + 0.06 * i), fx1 + s * k - s * 0.02, cy + s * (0.16 + 0.06 * i))
        d.arc(box, start=-50, end=50, fill=WHITE, width=int(w * 0.8))
    return icon.resize((size, size), Image.LANCZOS)


def place(icon: Image.Image, w: int, h: int, fraction: float) -> Image.Image:
    """The icon centred on a transparent w x h canvas, `fraction` of the shorter side."""
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    side = max(1, int(min(w, h) * fraction))
    small = icon.resize((side, side), Image.LANCZOS)
    canvas.paste(small, ((w - side) // 2, (h - side) // 2), small)
    return canvas


def save_new(img: Image.Image, path: Path, fmt: str = "PNG", **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:       # exclusive create: never overwrite
        img.save(f, fmt, **kw)


# name -> (base width, base height, icon fraction)
TILES = {
    "Square150x150Logo": (150, 150, 0.66),
    "Wide310x150Logo": (310, 150, 0.66),
    "SmallTile": (71, 71, 0.70),
    "LargeTile": (310, 310, 0.60),
    "SplashScreen": (620, 300, 0.60),
    "StoreLogo": (50, 50, 1.0),
    "Square44x44Logo": (44, 44, 1.0),
}


def build(out: Path) -> list:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty: refusing to write into it")
    icon = master_icon()
    written = []
    assets = out / "Assets"
    for name, (w, h, frac) in TILES.items():
        for scale in SCALES:
            p = assets / f"{name}.scale-{scale}.png"
            save_new(place(icon, round(w * scale / 100), round(h * scale / 100), frac), p)
            written.append(p)
    for size in TARGET_SIZES:
        for suffix in ("", "_altform-unplated"):
            p = assets / f"Square44x44Logo.targetsize-{size}{suffix}.png"
            save_new(icon.resize((size, size), Image.LANCZOS), p)
            written.append(p)
    p = out / "app.ico"
    save_new(icon, p, "ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    written.append(p)
    # Partner Center: app tile icon 300x300, 1:1 box art 1080x1080 and 2160x2160, 2:3 poster 720x1080
    for name, (w, h, frac) in {"tile-icon-300x300": (300, 300, 1.0),
                               "box-art-1080x1080": (1080, 1080, 0.8),
                               "box-art-2160x2160": (2160, 2160, 0.8),
                               "poster-art-720x1080": (720, 1080, 0.7)}.items():
        img = place(icon, w, h, frac)
        if frac < 1.0:   # Store box art must not be transparent: dark UI background
            bg = Image.new("RGBA", (w, h), (13, 17, 23, 255))
            bg.alpha_composite(img)
            img = bg
        p = out / "store" / f"{name}.png"
        save_new(img.convert("RGB") if frac < 1.0 else img, p)
        written.append(p)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    files = build(Path(sys.argv[1]))
    print(f"Wrote {len(files)} files to {sys.argv[1]}")
