"""Draw the product icon (a pulse line on a dark rounded square) with Pillow, so it can be regenerated.

    python scripts/make_icon.py <output-folder>

Writes icon.png (512 px, for the Unraid template and the Docker tab) and favicon.png (64 px). The drawing is
supersampled 4x and downscaled, so edges are smooth. Colours match the console theme (blue to green).
"""

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = 512
SS = 4  # supersampling factor
BLUE, GREEN = (77, 163, 255), (54, 196, 127)
PULSE = [(0.13, 0.55), (0.33, 0.55), (0.41, 0.31), (0.51, 0.77), (0.59, 0.46), (0.65, 0.55), (0.80, 0.55)]


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def gradient(size: int, top, bottom, horizontal=False) -> Image.Image:
    strip = Image.new("RGB", (size, 1) if horizontal else (1, size))
    for i in range(size):
        strip.putpixel((i, 0) if horizontal else (0, i), lerp(top, bottom, i / (size - 1)))
    return strip.resize((size, size))


def draw() -> Image.Image:
    n = SIZE * SS
    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # the rounded square, with a faint edge
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.22), fill=255)
    body = gradient(n, (19, 27, 41), (7, 10, 16)).convert("RGBA")
    canvas.paste(body, (0, 0), mask)
    edge = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(edge)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.22), outline=255, width=int(n * 0.012))
    canvas.paste(Image.new("RGBA", (n, n), (44, 53, 66, 255)), (0, 0), edge)

    # a faint grid, like a monitoring graph
    grid = Image.new("L", (n, n), 0)
    g = ImageDraw.Draw(grid)
    for i in range(1, 5):
        g.line((int(n * 0.1), int(n * i / 5), int(n * 0.9), int(n * i / 5)), fill=26, width=max(1, SS))
    canvas.paste(Image.new("RGBA", (n, n), (160, 171, 186, 255)), (0, 0), ImageChops.multiply(grid, mask))

    # the pulse line as a mask, drawn with round joins
    pts = [(int(x * n), int(y * n)) for x, y in PULSE]
    width = int(n * 0.058)
    line = Image.new("L", (n, n), 0)
    ld = ImageDraw.Draw(line)
    ld.line(pts, fill=255, width=width, joint="curve")
    for x, y in (pts[0], pts[-1]):
        ld.ellipse((x - width // 2, y - width // 2, x + width // 2, y + width // 2), fill=255)

    colour = gradient(n, BLUE, GREEN, horizontal=True).convert("RGBA")
    glow = line.filter(ImageFilter.GaussianBlur(n * 0.03))
    canvas.paste(colour, (0, 0), glow.point(lambda v: int(v * 0.55)))
    canvas.paste(colour, (0, 0), line)

    # the status dot at the end of the line
    ex, ey = pts[-1]
    r = int(n * 0.062)
    dot_glow = Image.new("L", (n, n), 0)
    ImageDraw.Draw(dot_glow).ellipse((ex - r * 2, ey - r * 2, ex + r * 2, ey + r * 2), fill=120)
    canvas.paste(
        Image.new("RGBA", (n, n), GREEN + (255,)), (0, 0), dot_glow.filter(ImageFilter.GaussianBlur(r * 0.8))
    )
    ImageDraw.Draw(canvas).ellipse(
        (ex - r, ey - r, ex + r, ey + r), fill=GREEN + (255,), outline=(231, 255, 240, 255), width=SS
    )

    final = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    final.paste(canvas, (0, 0), mask)
    return final.resize((SIZE, SIZE), Image.LANCZOS)


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    icon = draw()
    icon.save(out / "icon.png", optimize=True)
    icon.resize((64, 64), Image.LANCZOS).save(out / "favicon.png", optimize=True)
    print(f"wrote {out / 'icon.png'} and {out / 'favicon.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
