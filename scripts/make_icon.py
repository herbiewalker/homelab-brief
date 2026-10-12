"""Draw the product icon (a small newspaper front page with a pulse line) with Pillow, to regenerate it.

    python scripts/make_icon.py <output-folder>

Writes icon.png (512 px, for the Unraid template and the Docker tab) and favicon.png (64 px). Needs the DejaVu
fonts that the image ships. The drawing is supersampled 4x and downscaled, so edges are smooth.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 512
SS = 4  # supersampling factor
PAPER, PAPER_EDGE = (244, 237, 220), (214, 205, 184)
INK, GREY, RED = (22, 19, 17), (150, 143, 128), (214, 52, 43)
SERIF_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"


def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(SERIF_BOLD, size)
    except OSError:
        return ImageFont.load_default()


def draw() -> Image.Image:
    n = SIZE * SS

    def p(v: float) -> int:  # a fraction of the icon to pixels
        return int(v * n)

    body = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=p(0.2), fill=255)
    page = Image.new("RGBA", (n, n), PAPER + (255,))
    body.paste(page, (0, 0), mask)
    d = ImageDraw.Draw(body)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=p(0.2), outline=PAPER_EDGE + (255,), width=p(0.012))

    left, right = p(0.1), p(0.9)
    # masthead: a thick rule, a thin rule, the name, a thin rule, a thick red rule
    d.rectangle((left, p(0.105), right, p(0.125)), fill=INK)
    d.rectangle((left, p(0.14), right, p(0.147)), fill=INK)
    title = "Brief"
    f = font(p(0.235))
    box = d.textbbox((0, 0), title, font=f)
    tw = box[2] - box[0]
    d.text(((n - tw) // 2 - box[0], p(0.165) - box[1]), title, font=f, fill=INK)
    d.rectangle((left, p(0.425), right, p(0.432)), fill=INK)
    d.rectangle((left, p(0.447), right, p(0.465)), fill=RED)

    # the picture: a dark box with a pulse line
    pic = (left, p(0.51), p(0.47), p(0.87))
    d.rectangle(pic, fill=INK)
    px0, py0, px1, py1 = pic
    w, h = px1 - px0, py1 - py0
    pulse = [(0.08, 0.55), (0.30, 0.55), (0.40, 0.24), (0.52, 0.80), (0.62, 0.42), (0.70, 0.55), (0.92, 0.55)]
    pts = [(px0 + int(x * w), py0 + int(y * h)) for x, y in pulse]
    lw = p(0.024)
    d.line(pts, fill=RED, width=lw, joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - lw // 2, y - lw // 2, x + lw // 2, y + lw // 2), fill=RED)

    # the story: two heavy headline bars and lines of text
    x0 = p(0.51)
    d.rectangle((x0, p(0.51), right, p(0.545)), fill=INK)
    d.rectangle((x0, p(0.565), p(0.80), p(0.60)), fill=INK)
    for i in range(5):
        y = p(0.635 + i * 0.047)
        d.rectangle((x0, y, right - (p(0.05) if i % 2 else 0), y + p(0.016)), fill=GREY)

    final = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    final.paste(body, (0, 0), mask)
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
