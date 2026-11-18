"""Generate app icon for PDF Book Manager - original book + PDF badge design."""
from PIL import Image, ImageDraw
import os


def draw_rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    r = radius
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * r, y0 + 2 * r], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * r, y0, x1, y0 + 2 * r], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * r, x0 + 2 * r, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * r, y1 - 2 * r, x1, y1], 0, 90, fill=fill)


def generate_icon(size):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = s * 0.06
    corner = s * 0.22
    bg_color = (0, 150, 120)
    bg_highlight = (0, 172, 138)

    draw_rounded_rect(draw, (margin, margin, s - margin, s - margin), corner, bg_color)
    inner_m = s * 0.08
    draw_rounded_rect(draw, (inner_m, inner_m, s * 0.65, s * 0.55), corner * 0.85, bg_highlight)

    # Draw book cover (white page + spine)
    bx0, by0 = s * 0.24, s * 0.20
    bx1, by1 = s * 0.72, s * 0.78
    draw_rounded_rect(draw, (bx0 + s * 0.04, by0 + s * 0.03, bx1 + s * 0.04, by1 + s * 0.03), s * 0.05, (0, 105, 84))
    draw_rounded_rect(draw, (bx0, by0, bx1, by1), s * 0.05, (255, 255, 255))

    # Spine stripe
    draw.rectangle([bx0 + s * 0.06, by0, bx0 + s * 0.09, by1], fill=(210, 245, 235))

    # Title lines on book
    lw = max(1, int(s * 0.025))
    draw.line([bx0 + s * 0.15, by0 + s * 0.16, bx1 - s * 0.08, by0 + s * 0.16], fill=bg_color, width=lw)
    draw.line([bx0 + s * 0.15, by0 + s * 0.26, bx1 - s * 0.14, by0 + s * 0.26], fill=(0, 180, 145), width=lw)
    draw.line([bx0 + s * 0.15, by0 + s * 0.36, bx1 - s * 0.18, by0 + s * 0.36], fill=(140, 190, 175), width=max(1, int(lw * 0.8)))

    # Checkmark badge on bottom right
    cx, cy, cr = s * 0.73, s * 0.74, s * 0.15
    draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=(0, 115, 92))
    cw = max(2, int(s * 0.032))
    draw.line([cx - cr * 0.45, cy + cr * 0.02, cx - cr * 0.1, cy + cr * 0.38], fill=(255, 255, 255), width=cw)
    draw.line([cx - cr * 0.1, cy + cr * 0.38, cx + cr * 0.5, cy - cr * 0.35], fill=(255, 255, 255), width=cw)

    return img


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [generate_icon(s) for s in sizes]

    base_dir = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(base_dir, "app.ico")
    images[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])

    png_path = os.path.join(base_dir, "app_icon_256.png")
    images[-1].save(png_path, format="PNG")

    print(f"app.ico generado ({len(sizes)} tamanos: {sizes})")
    print("app_icon_256.png generado (preview)")

