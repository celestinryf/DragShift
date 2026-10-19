"""Read QR codes (and other barcodes) in an image with zxing-cpp (Apache-2.0), and show what
they say in a small window with Copy and Open Link. Links never open by themselves, and only
web links (http, https) can be opened at all."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton

from . import design
from .convert import open_image
from .tools import ToolWindow
from .util import UserError

MAX_SIDE = 4000      # bigger photos are scaled down first: decoding is faster and works as well
NONE_FOUND = "No QR code or barcode was found in this image. Try a sharper, closer picture."


@dataclass
class Code:
    text: str
    kind: str        # "QR Code", "EAN-13"…

    @property
    def link(self) -> str | None:
        return web_link(self.text)


def web_link(text: str) -> str | None:
    """The text when it's a single http(s) address, else None (javascript:, file:, mailto:
    and anything else are never opened)."""
    t = text.strip()
    if any(c.isspace() for c in t):
        return None
    try:
        parts = urlsplit(t)
    except ValueError:
        return None
    return t if parts.scheme.lower() in ("http", "https") and parts.netloc else None


def _kind_name(fmt) -> str:
    name = getattr(fmt, "name", str(fmt))
    return {"QRCode": "QR Code", "MicroQRCode": "Micro QR Code", "DataMatrix": "Data Matrix"}.get(name, name)


def decode(src: str | Path) -> list[Code]:
    """Every code found, top to bottom; the same text twice is listed once."""
    try:
        import zxingcpp
    except ImportError as e:
        raise UserError("QR reading isn't available in this copy of DragShift.") from e
    im = open_image(src).convert("L")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    found = zxingcpp.read_barcodes(im)
    found = sorted((r for r in found if r.valid and r.text), key=lambda r: (r.position.top_left.y, r.position.top_left.x))
    codes, seen = [], set()
    for r in found:
        if r.text not in seen:
            seen.add(r.text)
            codes.append(Code(r.text, _kind_name(r.format)))
    return codes


class QrWindow(ToolWindow):
    tool_name = "Read QR"

    def __init__(self, path, notify):
        self.codes = decode(path)
        if not self.codes:
            raise UserError(NONE_FOUND)
        n = len(self.codes)
        super().__init__("QR Code" if n == 1 else f"{n} Codes", path, notify,
                         size=(480, 220 + 150 * min(n, 3)))
        self.copy_buttons: list[QPushButton] = []
        self.link_buttons: list[QPushButton] = []
        for i, code in enumerate(self.codes):
            g = self.inspector.group(code.kind if n == 1 else f"{i + 1}. {code.kind}", collapsible=False)
            box = QPlainTextEdit(code.text)
            box.setReadOnly(True)
            box.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            box.setAccessibleName(f"{code.kind} text")
            box.setFixedHeight(76)
            g.addWidget(box)
            row = QHBoxLayout()
            row.setSpacing(design.SPACING["s"])
            copy = QPushButton("Copy")
            copy.setAccessibleName(f"Copy the text of code {i + 1}")
            copy.clicked.connect(lambda _=False, c=code, b=copy: self.copy(c, b))
            row.addWidget(copy)
            self.copy_buttons.append(copy)
            if code.link:
                open_btn = QPushButton("Open Link")
                open_btn.setToolTip(code.link)
                open_btn.clicked.connect(lambda _=False, c=code: self.open_link(c))
                row.addWidget(open_btn)
                self.link_buttons.append(open_btn)
            else:
                note = QLabel("Text, not a web link" if not code.text.lower().startswith(("http:", "https:"))
                              else "Not a valid web address")
                note.setObjectName("dim")
                row.addWidget(note)
            row.addStretch(1)
            g.addLayout(row)
        self.inspector.finish()
        done = QHBoxLayout()
        done.addStretch(1)
        ok = QPushButton("Done")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.close)
        done.addWidget(ok)
        self.primary_btn = ok
        self.inspector.set_footer(done)

    def copy(self, code: Code, button: QPushButton | None = None):
        QGuiApplication.clipboard().setText(code.text)
        if button is not None:
            button.setText("Copied")

    def open_link(self, code: Code):
        if code.link:          # checked again: only ever a web address the user clicked
            QDesktopServices.openUrl(QUrl(code.link))
