"""Render assets/create.icns: three camera lanes, one mid-transfer. Run: uv run --with pillow assets/make_icon.py"""
import subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024 * 4  # supersample
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
m, r = int(S * 0.098), int(S * 0.225)  # macOS icon grid: 824/1024 body, continuous-ish corners
d.rounded_rectangle((m, m, S - m, S - m), radius=r, fill=(26, 26, 28, 255), outline=(58, 58, 62, 255), width=S // 256)
w, gap = int(S * 0.118), int(S * 0.092)
x0 = (S - (3 * w + 2 * gap)) // 2
top, bottom = int(S * 0.27), int(S * 0.73)
fills = [1.0, 0.58, 1.0]
colors = [(236, 236, 237, 255), (240, 162, 59, 255), (236, 236, 237, 255)]
for i in range(3):
    x = x0 + i * (w + gap)
    d.rounded_rectangle((x, top, x + w, bottom), radius=w // 2, fill=(52, 52, 56, 255))
    y = bottom - int((bottom - top) * fills[i])
    d.rounded_rectangle((x, y, x + w, bottom), radius=w // 2, fill=colors[i])
img = img.resize((1024, 1024), Image.LANCZOS)
out = Path(__file__).parent
with tempfile.TemporaryDirectory() as t:
    iconset = Path(t) / "icon.iconset"; iconset.mkdir()
    for n in (16, 32, 128, 256, 512):
        img.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png")
        img.resize((n * 2, n * 2), Image.LANCZOS).save(iconset / f"icon_{n}x{n}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "create.icns")], check=True)
img.resize((256, 256), Image.LANCZOS).save(out / "icon-preview.png")
