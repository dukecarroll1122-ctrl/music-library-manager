"""Draws the app icon: assets/AppIcon.icns (for the .app) and mlm/gui/icon.png (window/Dock icon when run from source)."""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

S = 1024
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# gradient tile (blue -> violet) with Apple's usual margin, rounded corners
tile = Image.new("RGB", (824, 824))
d = ImageDraw.Draw(tile)
for y in range(824):
    t = y / 823
    d.line([(0, y), (824, y)], fill=(int(91 + 77 * t), int(140 - 56 * t), int(255 - 0 * t)))
mask = Image.new("L", (824, 824), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, 823, 823), radius=185, fill=255)
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(tile, (100, 100), mask)

# a beamed pair of eighth notes, drawn at 2x and scaled down for smooth edges
big = Image.new("RGBA", (S * 2, S * 2), (0, 0, 0, 0))
g = ImageDraw.Draw(big)
white = (255, 255, 255, 255)
for hx, hy in ((410, 700), (700, 640)):
    g.ellipse(((hx - 95) * 2, (hy - 70) * 2, (hx + 95) * 2, (hy + 70) * 2), fill=white)
    g.rectangle(((hx + 62) * 2, 300 * 2 if hx == 410 else 245 * 2, (hx + 98) * 2, hy * 2), fill=white)
g.polygon([(472 * 2, 300 * 2), (798 * 2, 245 * 2), (798 * 2, 345 * 2), (472 * 2, 400 * 2)], fill=white)
icon.alpha_composite(big.resize((S, S), Image.LANCZOS))

icon.save(os.path.join(root, "mlm", "gui", "icon.png"))
with tempfile.TemporaryDirectory() as tmp:
    iconset = os.path.join(tmp, "AppIcon.iconset")
    os.mkdir(iconset)
    for base in (16, 32, 128, 256, 512):
        icon.resize((base, base), Image.LANCZOS).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
        icon.resize((base * 2, base * 2), Image.LANCZOS).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(root, "assets", "AppIcon.icns")], check=True)
print("icon written")
