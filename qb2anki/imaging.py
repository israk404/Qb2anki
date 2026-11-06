"""Image handling: rotation, page-box validation, crops, debug overlay.

The model proposes where the pages are; this module checks the boxes and refuses to continue (LayoutError) when they look
wrong, instead of guessing.
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw, ImageOps

from .schemas import LayoutModel

log = logging.getLogger("qb2anki.imaging")


class LayoutError(RuntimeError):
    pass


_TRANSPOSE = {
    90: Image.Transpose.ROTATE_90,    # PIL's ROTATE_90 is counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def load_image(path) -> Image.Image:
    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")                 # a transparent background would turn black; put it on white paper
        paper = Image.new("RGB", im.size, "white")
        paper.paste(im, mask=im.getchannel("A"))
        return paper
    return im.convert("RGB")


def rotate_ccw(img: Image.Image, degrees: int) -> Image.Image:
    d = int(degrees) % 360
    return img if d == 0 else img.transpose(_TRANSPOSE[d])


def shrink(img: Image.Image, max_width: int) -> Image.Image:
    if img.width <= max_width:
        return img
    return img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- boxes
def _norm_box(b):
    """-> [ymin, xmin, ymax, xmax] floats in 0..1000, or None if unusable."""
    if b is None or len(b) != 4:
        return None
    try:
        y0, x0, y1, x1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    y0, y1 = sorted((y0, y1))
    x0, x1 = sorted((x0, x1))
    c = lambda v: max(0.0, min(1000.0, v))  # noqa: E731
    return [c(y0), c(x0), c(y1), c(x1)]


def _w(b): return b[3] - b[1]
def _h(b): return b[2] - b[0]


def to_px(box, W, H):
    """normalised [ymin,xmin,ymax,xmax] (0-1000) -> pixel (x0, y0, x1, y1)."""
    y0, x0, y1, x1 = box
    return (int(round(x0 * W / 1000)), int(round(y0 * H / 1000)),
            int(round(x1 * W / 1000)), int(round(y1 * H / 1000)))


WHOLE_PAGE = {"printed_page": None, "page_box": [0.0, 0.0, 1000.0, 1000.0]}


def validate_layout(layout: LayoutModel, W: int, H: int):
    """-> (pages, problems). pages are {'printed_page', 'page_box'} dicts, left to right."""
    problems: list[str] = []
    if not layout.text_upright:
        problems.append("model says the text is not upright (wrong rotation)")
    raw = []
    for p in layout.pages:
        pb = _norm_box(p.page_box)
        if pb is None:
            problems.append("a page has an unusable page_box")
            continue
        raw.append({"printed_page": p.printed_page, "page_box": pb})

    def aspect(b): return (_w(b) * W) / max(1e-6, _h(b) * H)
    if raw and all(aspect(r["page_box"]) > 1.15 for r in raw):      # some models answer [xmin, ymin, xmax, ymax]
        for r in raw:
            b = r["page_box"]
            r["page_box"] = [b[1], b[0], b[3], b[2]]
        log.info("layout boxes looked x/y-swapped; swapped them back")
    if not raw:
        problems.append("no pages found")
        return [], problems
    raw.sort(key=lambda r: r["page_box"][1])
    for i, r in enumerate(raw):
        pb = r["page_box"]
        if _w(pb) < 180 or _h(pb) < 400:
            problems.append(f"page {i + 1}: page box implausibly small")
    return raw, problems


def crop_px(img: Image.Image, box_px, pad: int = 0, upscale: float = 1.0) -> Image.Image:
    x0, y0, x1, y1 = box_px
    c = img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))
    if upscale and abs(upscale - 1.0) > 1e-3:
        c = c.resize((int(c.width * upscale), int(c.height * upscale)), Image.Resampling.LANCZOS)
    return c


def crop_norm(img: Image.Image, box, pad_frac: float = 0.0) -> Image.Image:
    """Crop with a normalised 0-1000 [ymin,xmin,ymax,xmax] box relative to `img`."""
    nb = _norm_box(box)
    if nb is None:
        return img
    x0, y0, x1, y1 = to_px(nb, img.width, img.height)
    return crop_px(img, (x0, y0, x1, y1), pad=int(pad_frac * max(img.size)))


def valid_box(box, min_frac: float = 0.02) -> bool:
    nb = _norm_box(box)
    return nb is not None and _w(nb) > 1000 * min_frac and _h(nb) > 1000 * min_frac


def draw_overlay(img: Image.Image, pages: list, max_width: int = 1800) -> Image.Image:
    """Debug picture of what the layout step decided; shown in the review tool."""
    ov = img.copy()
    d = ImageDraw.Draw(ov)
    W, H = ov.size
    lw = max(3, W // 600)
    for page in pages:
        d.rectangle(to_px(page["page_box"], W, H), outline=(0, 160, 0), width=lw)
        x0, y0, _, _ = to_px(page["page_box"], W, H)
        d.text((x0 + 8, y0 + 8), f"page {page.get('printed_page')}", fill=(0, 120, 0))
    return shrink(ov, max_width)
