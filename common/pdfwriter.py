"""Minimal dependency-free PDF 1.4 writer.

Only supports what the seeded invoice documents need: a single page of Helvetica
text lines. Hand-rolled so the repo does not need a PDF *generation* dependency
(pypdf is still used for text extraction).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

PAGE_WIDTH = 595  # A4 at 72dpi
PAGE_HEIGHT = 842
MARGIN = 56
LEADING = 15


def _escape(text: str) -> str:
    out = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in out)


@dataclass
class PdfDocument:
    title: str = "Invoice"
    lines: list[str] | None = None

    def __post_init__(self) -> None:
        if self.lines is None:
            self.lines = []

    def add_line(self, text: str, size: float = 10.0, bold: bool = False, gap: float = 0.0) -> None:
        self.lines.append((text, size, bold, gap))  # type: ignore[arg-type]

    def add_paragraph(self, text: str, size: float = 10.0, width: int = 78) -> None:
        for chunk in textwrap.wrap(text, width=width) or [""]:
            self.add_line(chunk, size=size)

    def add_rule(self, gap: float = 6.0) -> None:
        self.add_line("_" * 78, size=10.0, gap=gap)

    def add_space(self, amount: float = 10.0) -> None:
        self.add_line("", size=amount)


def _build_content_stream(doc: PdfDocument) -> bytes:
    chunks: list[str] = ["BT"]
    y = PAGE_HEIGHT - MARGIN
    for raw_line in doc.lines or []:
        text, size, bold, gap = raw_line  # type: ignore[misc]
        y -= (gap + size + 4)
        if y < MARGIN:
            break
        font = "/F2" if bold else "/F1"
        chunks.append(f"{font} {size:.1f} Tf")
        chunks.append(f"1 0 0 1 {MARGIN} {y:.1f} Tm")
        chunks.append(f"({_escape(str(text))}) Tj")
    chunks.append("ET")
    return "\n".join(chunks).encode("latin-1", errors="replace")


def render(doc: PdfDocument) -> bytes:
    content = _build_content_stream(doc)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Title (" + _escape(doc.title).encode("latin-1", "replace") + b") /Producer (ai-worker) >>",
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def write_pdf(path: Path, doc: PdfDocument) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(doc))
    return path