"""Fixtures that generate sample PDFs, including 'native' pre-existing human annotations
written as raw PDF objects (the way Acrobat-like viewers store them)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_review_mcp.service import ReviewService


def _utf16(s: str) -> str:
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _add_raw_annot(doc: pymupdf.Document, page: pymupdf.Page, body: str) -> int:
    xref = doc.get_new_xref()
    doc.update_object(xref, body)
    t, v = doc.xref_get_key(page.xref, "Annots")
    refs = v.strip("[] ").split() if t == "array" else []
    items = [" ".join(refs[i : i + 3]) for i in range(0, len(refs), 3)]
    items.append(f"{xref} 0 R")
    doc.xref_set_key(page.xref, "Annots", "[" + " ".join(items) + "]")
    return xref


def make_native_pdf(path: Path) -> dict[str, int]:
    """Two-page PDF with human annotations: popup, nested replies, native Review states,
    a Marked-model state, a Group member, a Link, a bare highlight and duplicate /NM."""
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.", fontsize=12)
    p1.insert_text((72, 300), "Second paragraph about budgets and totals.", fontsize=12)
    p2 = doc.new_page()
    p2.insert_text((72, 100), "Page two conclusion text.", fontsize=12)
    p1, p2 = doc[0], doc[1]
    hits = p1.search_for("quick brown")
    q = hits[0]
    h = p1.rect.height
    quad = f"[{q.x0} {h - q.y0} {q.x1} {h - q.y0} {q.x0} {h - q.y1} {q.x1} {h - q.y1}]"
    ids: dict[str, int] = {}
    ids["A"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (Human Alice) "
        f"/Contents {_utf16('Fix the typo — señor')} /M (D:20250101120000Z) "
        f"/CreationDate (D:20250101120000Z) /F 4 >>"))
    ids["P"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Popup /Rect [100 600 300 700] /Parent {ids['A']} 0 R /Open false >>"))
    doc.xref_set_key(ids["A"], "Popup", f"{ids['P']} 0 R")
    ids["B"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (Bob) /Contents (Which one?) "
        f"/IRT {ids['A']} 0 R /M (D:20250102120000Z) /F 28 >>"))
    ids["C"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (Human Alice) /Contents (The fox one) "
        f"/IRT {ids['B']} 0 R /RT /R /M (D:20250103120000Z) /F 28 >>"))
    ids["S1"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (Human Alice) "
        f"/Contents (Accepted set by Human Alice) /IRT {ids['A']} 0 R /State /Accepted "
        f"/StateModel /Review /M (D:20250104120000Z) /F 30 >>"))
    ids["S2"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (Human Alice) /Contents (Marked) "
        f"/IRT {ids['A']} 0 R /State /Marked /StateModel /Marked /M (D:20250105120000Z) /F 30 >>"))
    ids["D"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Highlight /Rect [{q.x0} {h - q.y1} {q.x1} {h - q.y0}] "
        f"/QuadPoints {quad} /T (Carol) /Contents (Reword this) /NM (human-nm-1) "
        f"/M (D:20250101090000+02'00') /C [1 1 0] /F 4 >>"))
    ids["S3"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /T (Carol) /Contents (Completed set by Carol) "
        f"/IRT {ids['D']} 0 R /State /Completed /StateModel /Review /M (D:20250106120000Z) /F 30 >>"))
    ids["G"] = _add_raw_annot(doc, p1, (
        f"<< /Type /Annot /Subtype /Square /Rect [50 470 90 510] /T (Carol) /IRT {ids['D']} 0 R /RT /Group >>"))
    ids["L"] = _add_raw_annot(doc, p1, (
        "<< /Type /Annot /Subtype /Link /Rect [70 480 200 500] /Border [0 0 0] "
        "/A << /S /URI /URI (https://example.invalid) >> >>"))
    ids["E"] = _add_raw_annot(doc, p2, (
        "<< /Type /Annot /Subtype /Square /Rect [60 600 200 700] /T (Dave) /NM (dup) >>"))
    ids["F"] = _add_raw_annot(doc, p2, (
        "<< /Type /Annot /Subtype /Text /Rect [300 600 320 620] /T (Dave) /Contents (Check conclusion) /NM (dup) >>"))
    doc.save(path)
    doc.close()
    return ids


def make_plain_pdf(path: Path, pages: int = 2) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 100), f"Page {i + 1}: The quick brown fox jumps over the lazy dog.")
        pg.insert_text((72, 400), "Lorem ipsum " * 40)
    doc.save(path)
    doc.close()


@pytest.fixture(autouse=True)
def isolated_style_key(tmp_path_factory):
    """Never touch the real ~/.config: every test gets its own XDG_CONFIG_HOME (key dir).
    Uses its OWN MonkeyPatch so a test's monkeypatch.undo() cannot remove the isolation."""
    cfg = tmp_path_factory.mktemp("xdg-config")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CONFIG_HOME", str(cfg))
        yield cfg


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def native(root: Path) -> tuple[Path, dict[str, int]]:
    p = root / "native.pdf"
    return p, make_native_pdf(p)


@pytest.fixture
def plain(root: Path) -> Path:
    p = root / "plain.pdf"
    make_plain_pdf(p)
    return p


@pytest.fixture
def svc() -> ReviewService:
    return ReviewService(default_actor="test-agent")


# ----------------------------------------------------------------- native objects written by OTHER tools
# (the server never creates replies or status markers; tests that need them write them like a viewer.)


def add_native_reply(path, parent_xref: int, text: str, author: str = "Human", rt: bool = True) -> int:
    doc = pymupdf.open(str(path))
    page = next(p for p in doc if parent_xref in [x for x, _, _ in p.annot_xrefs()])
    body = (f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T ({author}) "
            f"/Contents {_utf16(text)} /IRT {parent_xref} 0 R {'/RT /R ' if rt else ''}/F 28 >>")
    x = _add_raw_annot(doc, page, body)
    doc.saveIncr()
    doc.close()
    return x


def add_native_marker(path, parent_xref: int, state: str, note: str = "", author: str = "Viewer",
                      model: str = "Review") -> int:
    doc = pymupdf.open(str(path))
    page = next(p for p in doc if parent_xref in [x for x, _, _ in p.annot_xrefs()])
    x = _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /T ({author}) "
                                  f"/Contents {_utf16(note)} /IRT {parent_xref} 0 R /State /{state} "
                                  f"/StateModel /{model} /F 30 >>")
    doc.saveIncr()
    doc.close()
    return x


STATE_MANAGED_KEYS = ("M", "PDFReviewState", "Contents")


def core_obj(path, xref: int) -> dict:
    """Annotation keys except those the review-state machinery manages (/M, /PDFReviewState,
    /Contents); used to assert byte-exact STYLE restoration."""
    doc = pymupdf.open(str(path))
    try:
        return {k: doc.xref_get_key(xref, k) for k in doc.xref_get_keys(xref) if k not in STATE_MANAGED_KEYS}
    finally:
        doc.close()


def annots_snapshot(path) -> list:
    doc = pymupdf.open(str(path))
    try:
        return [(p.number, doc.xref_get_key(p.xref, "Annots")) for p in doc]
    finally:
        doc.close()


def add_iconic_child(doc, page, parent_xref: int, corner, text: str, *, state=None, nm: str,
                     author: str = "review-agent") -> int:
    """A visible-icon Text child (reply, or Review status marker when `state`) with appearance and
    a closed popup, the way many viewers and tools write them. Used to test native READING."""
    a = page.add_text_annot(corner, text, icon="Comment")
    a.set_info(title=author, content=text, subject="Status" if state else "Reply")
    a.update()
    x = a.xref
    doc.xref_set_key(x, "NM", pymupdf.get_pdf_str(nm))
    doc.xref_set_key(x, "M", "(D:20261111055857+00'00')")
    doc.xref_set_key(x, "IRT", f"{parent_xref} 0 R")
    if state:
        doc.xref_set_key(x, "State", f"/{state}")
        doc.xref_set_key(x, "StateModel", "/Review")
        doc.xref_set_key(x, "F", "26")
    else:
        doc.xref_set_key(x, "RT", "/R")
        doc.xref_set_key(x, "F", "24")
    p = doc.xref_get_key(x, "Popup")
    if p[0] == "xref":
        doc.xref_set_key(int(p[1].split()[0]), "Open", "false")
    return x


def use_generation_255(path, page_no: int, xrefs) -> str:
    """Rewrite /Annots so the given objects are referenced as 'N 255 R' (as real viewers do)."""
    d = pymupdf.open(str(path))
    pg = d[page_no]
    t, v = d.xref_get_key(pg.xref, "Annots")
    for x in xrefs:
        v = v.replace(f"{x} 0 R", f"{x} 255 R")
    d.xref_set_key(pg.xref, "Annots", v)
    d.saveIncr()
    d.close()
    return v
