"""Build Uhlexa's logo assets from her avatar portrait (the image the Runway Character was made from).

    python3 scripts/make_brand_assets.py   # needs Pillow; writes assets/brand/*.png

Wordmark font: Fredoka (SIL Open Font License, assets/brand/fonts/OFL.txt).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"
PORTRAIT = BRAND / "uhlexa-avatar.png"
FONT = BRAND / "fonts" / "Fredoka.ttf"

NAVY = (34, 31, 68)
LAVENDER = (185, 163, 242)
LILAC = (220, 207, 242)
PEACH = (232, 145, 111)

# Head centre and radius in the 1360x768 portrait (hair included, sweater excluded).
HEAD_CX, HEAD_CY, HEAD_R = 686, 300, 300


def font(size: int, weight: int = 600) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight, 100])
    return f


def mark(size: int, transparent: bool = False) -> Image.Image:
    """Round badge: the portrait's head in a navy disc with a lavender ring and a thin peach inner ring."""
    scale = 4  # draw big, downsample for smooth edges
    big = size * scale
    portrait = Image.open(PORTRAIT).convert("RGB")
    box = (HEAD_CX - HEAD_R, HEAD_CY - HEAD_R, HEAD_CX + HEAD_R, HEAD_CY + HEAD_R)
    ring = int(big * 0.045)
    gap = int(big * 0.018)
    inner = big - 2 * (ring + gap)
    face = portrait.crop(box).resize((inner, inner), Image.LANCZOS)

    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0) if transparent else (*NAVY, 255))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((0, 0, big - 1, big - 1), fill=(*LAVENDER, 255))
    draw.ellipse((ring, ring, big - ring - 1, big - ring - 1), fill=(*PEACH, 255))
    pad = ring + gap
    draw.ellipse((pad - gap // 3, pad - gap // 3, big - pad + gap // 3, big - pad + gap // 3), fill=(*NAVY, 255))
    disc = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    canvas.paste(face, (pad, pad), disc)
    return canvas.resize((size, size), Image.LANCZOS)


def wordmark(text_size: int, color=LILAC) -> Image.Image:
    f = font(text_size, 600)
    left, top, right, bottom = f.getbbox("Uhlexa")
    img = Image.new("RGBA", (right - left + 8, bottom - top + 8), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4 - left, 4 - top), "Uhlexa", font=f, fill=(*color, 255))
    return img


def glow(size: tuple[int, int], center: tuple[int, int], radius: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*LAVENDER, 70))
    return layer.filter(ImageFilter.GaussianBlur(radius // 3))


def lockup(width: int, height: int) -> Image.Image:
    """Horizontal: mark on the left, wordmark on the right, navy background."""
    img = Image.new("RGBA", (width, height), (*NAVY, 255))
    m = mark(int(height * 0.72), transparent=True)
    w = wordmark(int(height * 0.42))
    gap = int(height * 0.12)
    mx = (width - (m.width + gap + w.width)) // 2
    my = (height - m.height) // 2
    img.alpha_composite(glow((width, height), (mx + m.width // 2, my + m.height // 2), int(m.width * 0.62)))
    img.alpha_composite(m, (mx, my))
    img.alpha_composite(w, (mx + m.width + gap, (height - w.height) // 2))
    return img


def card(width: int, height: int, tagline: str) -> Image.Image:
    """Stacked: mark, wordmark, tagline. 3:2 for the Devpost gallery and thumbnail."""
    img = Image.new("RGBA", (width, height), (*NAVY, 255))
    m = mark(int(height * 0.46), transparent=True)
    mx, my = (width - m.width) // 2, int(height * 0.1)
    img.alpha_composite(glow((width, height), (mx + m.width // 2, my + m.height // 2), int(m.width * 0.66)))
    img.alpha_composite(m, (mx, my))
    w = wordmark(int(height * 0.16))
    wy = my + m.height + int(height * 0.04)
    img.alpha_composite(w, ((width - w.width) // 2, wy))
    t = font(int(height * 0.045), 400)
    tw = ImageDraw.Draw(img).textlength(tagline, font=t)
    ImageDraw.Draw(img).text(
        ((width - tw) / 2, wy + w.height + int(height * 0.03)), tagline, font=t, fill=(*LAVENDER, 255)
    )
    return img


def main() -> None:
    mark(1024).save(BRAND / "uhlexa-mark.png")
    mark(1024, transparent=True).save(BRAND / "uhlexa-mark-transparent.png")
    mark(512, transparent=True).save(BRAND / "uhlexa-icon-512.png")
    mark(64, transparent=True).save(BRAND / "favicon-64.png")
    lockup(2400, 800).save(BRAND / "uhlexa-logo-horizontal.png")
    card(1500, 1000, "One family. Many agents. A human in charge.").convert("RGB").save(BRAND / "uhlexa-card-3x2.png")
    for p in sorted(BRAND.glob("*.png")):
        print(p.relative_to(ROOT), Image.open(p).size)


if __name__ == "__main__":
    main()
