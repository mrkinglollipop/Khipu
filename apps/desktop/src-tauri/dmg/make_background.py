"""Generate the DMG installer background (660x400 pt window, rendered @2x).

Palette comes from App.css: warm paper #faf9f7, ink #201d1a, terracotta
accent #a63a1e. Icon centers in release_macos.sh / tauri.conf.json are
Khipu.app (180,170) and Applications (480,170); the arrow spans between
them and text stays clear of the 128px icons. DPI is stamped 144 so Finder
draws the 2x bitmap at window size on Retina.

Run: python3 make_background.py   (writes dmg-background@2x.png here)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2  # render scale
W, H = 660 * S, 400 * S
PAPER = (250, 249, 247)
PAPER_EDGE = (243, 240, 235)
INK = (32, 29, 26)
MUTED = (128, 120, 112)
ACCENT = (166, 58, 30)


def font(size_pt: int, bold: bool = False):
    for path, idx in [
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1 if bold else 0),
    ]:
        try:
            return ImageFont.truetype(path, size_pt * S, index=idx)
        except OSError:
            continue
    return ImageFont.load_default()


img = Image.new("RGB", (W, H), PAPER)
d = ImageDraw.Draw(img)

# Soft vertical gradient so the window doesn't read as dead white.
for y in range(H):
    t = y / H
    c = tuple(int(PAPER[i] + (PAPER_EDGE[i] - PAPER[i]) * t) for i in range(3))
    d.line([(0, y), (W, y)], fill=c)

# Knot-cord motif, top-left: a quipu is pendant cords knotted along a
# horizontal primary cord — the entire reason the app is called Khipu.
FADED = (196, 128, 108)
prim_y = 18 * S
d.line([(24 * S, prim_y), (170 * S, prim_y)], fill=ACCENT, width=3 * S)
for x_pt, length_pt, knots in [
    (40, 70, (34, 52)),
    (64, 104, (40, 62, 86)),
    (88, 56, (38,)),
    (112, 88, (36, 68)),
    (136, 64, (30, 48)),
]:
    x = x_pt * S
    d.line([(x, prim_y), (x, length_pt * S)], fill=FADED, width=2 * S)
    for ky_pt in knots:
        r = 3 * S
        ky = ky_pt * S
        d.ellipse([x - r, ky - r, x + r, ky + r], fill=ACCENT)

# Wordmark + tagline, centered.
wm = font(34, bold=True)
tg = font(13)
d.text((W // 2, 52 * S), "Khipu", font=wm, fill=INK, anchor="mm")
d.text((W // 2, 82 * S), "Memory for your coding agents", font=tg, fill=MUTED, anchor="mm")

# Dashed arrow between the icon centers (icons are 128 pt wide at y=170).
y = 170 * S
x0, x1 = 262 * S, 392 * S
dash, gap = 10 * S, 7 * S
x = x0
while x < x1 - 12 * S:
    d.line([(x, y), (min(x + dash, x1 - 12 * S), y)], fill=ACCENT, width=3 * S)
    x += dash + gap
d.polygon([(x1, y), (x1 - 14 * S, y - 8 * S), (x1 - 14 * S, y + 8 * S)], fill=ACCENT)

# Install hint, low center, clear of icon labels.
d.text((W // 2, 320 * S), "Drag Khipu into Applications to install",
       font=font(14), fill=MUTED, anchor="mm")

out = Path(__file__).parent / "dmg-background@2x.png"
img.save(out, dpi=(144, 144))
print(f"wrote {out} ({img.size[0]}x{img.size[1]} @144dpi)")
