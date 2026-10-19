"""SVG input: rasterise with QtSvg (already part of PySide6, so no cairo) to any image format,
or draw it into a PDF as vectors.

QtSvg implements SVG Tiny 1.2: shapes, paths, gradients, text and embedded pictures render;
CSS stylesheets, filters and masks don't. It never fetches anything over the network, but it does
read local files a picture points at, so those links are removed before drawing (`safe_svg`)."""
from __future__ import annotations

from pathlib import Path

from .convert import IMAGE_TARGETS, HEIF_OK, atomic_save, save_image
from .util import UserError, sibling

# SVG has no pixel size of its own: a 24 px icon rasterised as-is is useless, a poster-sized
# drawing would be gigapixels. Scale so the long side lands in this range.
MIN_SIDE = 1024
MAX_SIDE = 8192
PX_TO_PT = 0.75        # CSS pixels are 1/96 inch, PDF points 1/72
PICTURE_TAGS = {"image", "feImage"}       # elements QtSvg draws a linked file for
HREF_ATTRS = ("{http://www.w3.org/1999/xlink}href", "href")


def targets_for() -> list[str]:
    return [t for t in IMAGE_TARGETS if HEIF_OK or t != "HEIC"]


def raster_size(width: float, height: float) -> tuple[int, int]:
    """Output pixels for a drawing `width` x `height` (its own units), keeping the shape."""
    long_side = max(width, height)
    scale = min(max(1.0, MIN_SIDE / long_side), MAX_SIDE / long_side)
    return max(1, round(width * scale)), max(1, round(height * scale))


def safe_svg(src: str | Path) -> bytes:
    """The SVG with every link to an outside file removed. An SVG from the web could otherwise
    draw any local file into the result (`<image href="/Users/me/...">`, "../", a symlink).
    Pictures (<image>, filter <feImage>) keep only data: URIs; other elements keep only links to
    their own parts ("#id")."""
    from lxml import etree
    # "internal": Illustrator declares its namespaces as DTD entities, so those must expand;
    # an external entity (a file or URL) is never read and makes the parse fail instead
    parser = etree.XMLParser(resolve_entities="internal", no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(Path(src).read_bytes(), parser)
    except (etree.XMLSyntaxError, ValueError) as e:
        raise UserError("This SVG is damaged, or uses features DragShift can't draw.") from e
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        picture = etree.QName(el).localname in PICTURE_TAGS
        for attr in HREF_ATTRS:
            href = (el.get(attr) or "").strip()
            if not href or href.startswith("#") or (picture and href.lower().startswith("data:")):
                continue
            if picture and el.getparent() is not None:
                el.getparent().remove(el)
                break
            del el.attrib[attr]
    return etree.tostring(root)


def _renderer(src: str | Path):
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer
    r = QSvgRenderer(QByteArray(safe_svg(src)))
    if not r.isValid():
        raise UserError("This SVG is damaged, or uses features DragShift can't draw.")
    size = r.defaultSize()
    if size.width() > 0 and size.height() > 0:
        return r, float(size.width()), float(size.height())
    box = r.viewBoxF()
    if box.width() > 0 and box.height() > 0:
        return r, box.width(), box.height()
    raise UserError("This SVG has no size (no width, height or viewBox), so it can't be drawn.")


def render(src: str | Path, size: tuple[int, int] | None = None):
    """The drawing as a transparent RGBA PIL image (raster_size when `size` isn't given)."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter

    from .glass import pil_from_qimage
    r, w, h = _renderer(src)
    width, height = size or raster_size(w, h)
    img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    r.render(p, QRectF(0, 0, width, height))
    p.end()
    return pil_from_qimage(img)


def to_pdf(src: str | Path, out: Path) -> None:
    """One page the drawing's own size, drawn as vectors (text stays text)."""
    from PySide6.QtCore import QMarginsF, QRectF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter

    r, w, h = _renderer(src)

    def write(dst):
        pdf = QPdfWriter(str(dst))
        pdf.setPageLayout(QPageLayout(QPageSize(QSizeF(w * PX_TO_PT, h * PX_TO_PT), QPageSize.Point),
                                      QPageLayout.Portrait, QMarginsF(0, 0, 0, 0)))
        p = QPainter(pdf)
        r.render(p, QRectF(0, 0, pdf.width(), pdf.height()))
        p.end()

    atomic_save(write, out)


def convert(src: str | Path, target: str) -> list[Path]:
    target = target.upper()
    if target not in IMAGE_TARGETS:
        raise ValueError(f"Can't convert SVG to {target}")
    out = sibling(src, ext="." + target.lower())
    if target == "PDF":
        to_pdf(src, out)
    else:
        save_image(render(src), out, target)
    return [out]
