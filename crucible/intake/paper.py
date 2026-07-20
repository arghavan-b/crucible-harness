"""Paper ingestion — PDF -> text, tables, and figure images (design §6.1).

The results tables are where reproducible claims live, so we extract them
structurally (pdfplumber) rather than as flat text, and pull figure images
(PyMuPDF) so a vision-capable model can read plots. Both libraries are optional
and imported lazily; if a library is missing, that modality degrades to empty
rather than failing the whole parse.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class Table:
    page: int
    index: int
    markdown: str          # pipe-delimited rendering for the LLM
    rows: list[list[str]]


@dataclass
class Figure:
    page: int
    index: int
    image_b64: str         # base64 PNG, for a vision model
    media_type: str = "image/png"


@dataclass
class ParsedPaper:
    path: str
    page_text: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(self.page_text))

    def tables_markdown(self) -> str:
        return "\n\n".join(
            f"[table p{t.page} #{t.index}]\n{t.markdown}" for t in self.tables
        )


def _rows_to_markdown(rows: list[list[str]]) -> str:
    clean = [["" if c is None else str(c).replace("\n", " ").strip() for c in r] for r in rows]
    clean = [r for r in clean if any(cell for cell in r)]
    if not clean:
        return ""
    width = max(len(r) for r in clean)
    clean = [r + [""] * (width - len(r)) for r in clean]
    out = ["| " + " | ".join(clean[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(out)


def _extract_text_and_tables(path: str) -> tuple[list[str], list[Table]]:
    try:
        import pdfplumber
    except ImportError:
        return [], []
    page_text: list[str] = []
    tables: list[Table] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            page_text.append(page.extract_text() or "")
            for ti, raw in enumerate(page.extract_tables() or []):
                md = _rows_to_markdown(raw)
                if md:
                    tables.append(Table(page=pno, index=ti, markdown=md, rows=raw))
    return page_text, tables


def _extract_figures(path: str, max_figures: int = 12) -> list[Figure]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []
    figures: list[Figure] = []
    doc = fitz.open(path)
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            for idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:  # CMYK/other -> convert to RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    png = pix.tobytes("png")
                except Exception:
                    continue
                figures.append(
                    Figure(page=pno + 1, index=idx, image_b64=base64.b64encode(png).decode())
                )
                if len(figures) >= max_figures:
                    return figures
    finally:
        doc.close()
    return figures


def parse_pdf(path: str, include_figures: bool = True) -> ParsedPaper:
    page_text, tables = _extract_text_and_tables(path)
    figures = _extract_figures(path) if include_figures else []
    return ParsedPaper(path=path, page_text=page_text, tables=tables, figures=figures)
