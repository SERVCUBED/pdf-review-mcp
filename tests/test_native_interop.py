"""Reading and working with native PDF objects written by viewers/other tools (replies, Review
markers with icons and popups, 'N 255 R' references, signed grey style) through the CURRENT public
API only. Fixtures reproduce the structure of the user's two manuals; the real PDFs are never used.
No annotation is ever created or removed by the server."""

from __future__ import annotations

import json

import pymupdf
import pytest

from pdf_review_mcp import appearance as style
from pdf_review_mcp.pdfstore import ReviewError, parse_document
from pdf_review_mcp.service import ReviewService

from conftest import _add_raw_annot, add_iconic_child, annots_snapshot, core_obj, use_generation_255

LABEL = "review-agent"


def rev(svc, f):
    return svc.review_summary(f)["revision"]


def th(svc, f, cid):
    return svc.get_comment(f, cid, 0)["thread"]


def crop(f, page, rect):
    d = pymupdf.open(f)
    try:
        pix = d[page].get_pixmap(clip=pymupdf.Rect(rect), dpi=144, annots=True)
        return pix.samples, pix.n
    finally:
        d.close()


def dark(sample):
    s, n = sample
    return sum(1 for i in range(0, len(s), n) if min(s[i:i + 3]) < 200)


def active(f):
    d = pymupdf.open(f)
    try:
        return {p.number: [x for x, _, _ in p.annot_xrefs()] for p in d}
    finally:
        d.close()


# ----------------------------------------------------------------- appearance on native shapes


def test_completion_creates_no_annotation_unsupported_type(svc, root):
    """An unsupported type is not restyled but gets the [DONE] cue and private state; the page
    renders identically and the annotation list is unchanged."""
    f = str(root / "m.pdf")
    d = pymupdf.open()
    pg = d.new_page()
    sq = pg.add_rect_annot((72, 300, 200, 360))
    sq.set_info(content="Check box")
    sq.update()
    d.save(f)
    d.close()
    pix0, ann0 = crop(f, 0, (0, 0, 595, 842)), annots_snapshot(f)
    r = svc.mark_comment_done(f, f"xref:{sq.xref}", "done", rev(svc, f))
    assert r["appearance"]["reason"] == "unsupported_type" and r["text"] == "[DONE] Check box"
    assert annots_snapshot(f) == ann0 and crop(f, 0, (0, 0, 595, 842)) == pix0
    assert svc.review_summary(f)["done"] == 1 and svc.review_summary(f)["status_markers"] == 0
    o = svc.reopen_comment(f, f"xref:{sq.xref}", "again", r["revision"])
    assert svc.review_summary(f)["open"] == 1 and o["text"] == "Check box"


def test_raw_24x24_note_without_ap_grey_visible_and_restored(svc, root):
    """Viewer-made 24x24 note with no appearance: the grey icon must stay visible (normal form
    BBox, raw /Rect kept) and reopen restores the exact original."""
    f = str(root / "n.pdf")
    d = pymupdf.open()
    pg = d.new_page()
    x = _add_raw_annot(d, pg, "<< /Type /Annot /Subtype /Text /Rect [104.84 18.95 128.84 42.95] "
                              "/T (Unknown) /Contents (Mark this as done) /C [1 1 0] >>")
    d.save(f)
    d.close()
    region = (95, 790, 135, 830)
    core0, before = core_obj(f, x), crop(f, 0, region)
    assert dark(before) > 50
    dn = svc.mark_comment_done(f, f"xref:{x}", "done", rev(svc, f))
    assert dn["appearance"]["action"] == "apply" and dn["text"] == "[DONE] Mark this as done"
    done = crop(f, 0, region)
    assert dark(done) > 50, "grey note must stay visible"
    s, n = done
    assert all(abs(s[i] - s[i + 1]) < 8 and abs(s[i + 1] - s[i + 2]) < 8 for i in range(0, len(s), n))
    doc = pymupdf.open(f)
    assert doc.xref_get_key(x, "Rect") == ("array", "[104.84 18.95 128.84 42.95]")
    form = int(doc.xref_get_key(x, "AP")[1].split()[1])
    assert doc.xref_get_key(form, "BBox") == ("array", "[0 0 16 16]")
    doc.close()
    svc.reopen_comment(f, f"xref:{x}", "x", dn["revision"])
    assert core_obj(f, x) == core0 and crop(f, 0, region) == before
    assert th(svc, f, f"xref:{x}")["text"] == "Mark this as done"


# ----------------------------------------------------------------- manual-shaped fixtures


@pytest.fixture
def orig_manual_like(root):
    """Shape of pdf-review-user-manual.pdf: p1 EMPTY highlight root (no /NM, popup) + native
    Completed marker; p2 24x24 note with inline answer + native reply with the same text +
    marker; p3 highlight with text + marker. Viewer objects referenced as 'N 255 R'."""
    f = root / "orig_manual_like.pdf"
    d = pymupdf.open()
    for _ in range(3):
        d.new_page().insert_text((72, 120), "Body text of the manual page.", fontsize=12)
    hl = _add_raw_annot(d, d[0], "<< /Type /Annot /Subtype /Highlight /Rect [78 449 316 468] "
                                 "/QuadPoints [78 468 316 468 78 449 316 449] /T (Unknown) /C [1 1 0] /Contents () >>")
    hp = _add_raw_annot(d, d[0], f"<< /Type /Annot /Subtype /Popup /Rect [318 309 518 459] /Parent {hl} 0 R >>")
    d.xref_set_key(hl, "Popup", f"{hp} 0 R")
    note = _add_raw_annot(d, d[1], "<< /Type /Annot /Subtype /Text /Rect [113 105.32 137 129.32] /T (Unknown) "
                                   "/Contents (Where is this?) /C [1 1 0] >>")
    np_ = _add_raw_annot(d, d[1], f"<< /Type /Annot /Subtype /Popup /Rect [137 -44 337 105] /Parent {note} 0 R >>")
    d.xref_set_key(note, "Popup", f"{np_} 0 R")
    h3 = _add_raw_annot(d, d[2], "<< /Type /Annot /Subtype /Highlight /Rect [134 442 292 461] "
                                 "/QuadPoints [134 461 292 461 134 442 292 442] /T (Unknown) /C [1 1 0] "
                                 "/Contents (Comment on highlighted text) >>")
    d.save(f)
    d.close()
    d = pymupdf.open(f)
    answer = "The configuration belongs in your MCP client."
    add_iconic_child(d, d[0], hl, pymupdf.Point(73.5, 322), "Addressed: storage.", state="Completed", nm="rv-a")
    reply = add_iconic_child(d, d[1], note, pymupdf.Point(113, 712.7), answer, nm="rv-r")
    add_iconic_child(d, d[1], note, pymupdf.Point(113, 712.7), "Addressed: config.", state="Completed", nm="rv-b")
    add_iconic_child(d, d[2], h3, pymupdf.Point(129.5, 329), "Addressed: prep.", state="Completed", nm="rv-c")
    inline = f"Where is this?\n\n[{LABEL}] {answer}"
    d.xref_set_key(note, "Contents", pymupdf.get_pdf_str(inline))
    d.saveIncr()
    d.close()
    f = str(f)
    use_generation_255(f, 0, [hl, hp])
    use_generation_255(f, 1, [note, np_])
    use_generation_255(f, 2, [h3])
    return {"f": f, "hl": hl, "note": note, "h3": h3, "reply": reply, "inline": inline, "hp": hp, "np": np_}


def test_orig_manual_shape_is_read_correctly(svc, orig_manual_like):
    o = orig_manual_like
    f = o["f"]
    s = svc.review_summary(f)
    assert (s["root_comments"], s["done"], s["open"], s["replies"], s["status_markers"]) == (3, 3, 0, 1, 3)
    t = th(svc, f, f"xref:{o['note']}")
    assert t["effective_source"] == "native" and t["done"] and t["text"] == o["inline"]
    assert [r["id"] for r in t["replies"]] == ["rv-r"] and t["state_history"][0]["id"] == "rv-b"
    assert th(svc, f, f"xref:{o['hl']}")["text"] == "" and th(svc, f, f"xref:{o['hl']}")["done"]
    for x in (o["hl"], o["note"]):
        assert th(svc, f, f"xref:{x}")["id"] == f"xref:{x}"  # no /NM: stable xref ids


def test_orig_manual_shape_reopen_and_complete_keep_every_native_object(svc, orig_manual_like):
    """Current API on a natively-completed file: reopen + answer + complete never create, remove or
    re-reference annotations ('N 255 R' tokens, popups, the native reply and markers all kept)."""
    o = orig_manual_like
    f = o["f"]
    a0, act0 = annots_snapshot(f), active(f)
    r = svc.reopen_comment(f, f"xref:{o['note']}", "follow-up needed", rev(svc, f))
    assert r["text"] == o["inline"]  # no server-owned prefix existed: nothing stripped
    a = svc.append_agent_response(f, f"xref:{o['note']}", "Now in section 2.", o["inline"], r["revision"], actor=LABEL)
    d = svc.mark_comment_done(f, f"xref:{o['note']}", "section 2 added", a["revision"])
    assert d["text"] == "[DONE] " + o["inline"] + f"\n\n[{LABEL}] Now in section 2."
    assert annots_snapshot(f) == a0 and active(f) == act0
    t = th(svc, f, f"xref:{o['note']}")
    assert [h["source"] for h in t["state_history"]] == ["native", "server", "server"]
    assert t["reply_count"] == 1 and t["appearance"]["color"] == [0.55, 0.55, 0.55]
    # the empty natively-completed highlight can be synced to the visible cue, adopting the state
    sy = svc.sync_comment_appearance(f, f"xref:{o['hl']}", d["revision"])
    assert sy["adopted"] == "rv-a" and sy["text"] == "[DONE]" and annots_snapshot(f) == a0


def _old_style_bbox(doc, x, rect_before, new_ap, subtype="Highlight"):
    """Reproduces a grey Text appearance whose form /BBox is in page space (clipped icon), the shape
    present in the user's v0.2 manual."""
    if doc.xref_get_key(x, "Rect") == rect_before:
        return
    n = int(style._AP_REFS.findall(new_ap)[0])
    doc.xref_set_key(n, "BBox", rect_before[1])
    doc.xref_set_key(x, "Rect", rect_before[1])


@pytest.fixture
def v02_manual_like(root, monkeypatch):
    """Shape of pdf-review-user-manual-v0.2.pdf p8: 24x24 note, native Completed marker with icon
    and popup, signed grey style (clipped), no private review-state record."""
    f = root / "v02_manual_like.pdf"
    d = pymupdf.open()
    pg = d.new_page()
    note = _add_raw_annot(d, pg, "<< /Type /Annot /Subtype /Text /Rect [104.836467 18.946686 "
                                 "128.83647 42.946687] /T (Unknown) /Contents (Mark this as done) /C [1 1 0] >>")
    add_iconic_child(d, pg, note, pymupdf.Point(104.84, 799), "Marked Completed (test)", state="Completed",
                     nm="rv-v")
    d.save(f)
    d.close()
    f = str(f)
    with monkeypatch.context() as mp:
        mp.setattr(style, "_keep_rect", _old_style_bbox)
        doc = pymupdf.open(f)
        r = parse_document(doc).annots[note]
        assert style.execute(doc, r, style.plan(doc, r, True), LABEL)["action"] == "apply"
        doc.saveIncr()
        doc.close()
    return {"f": f, "note": note}


def test_v02_manual_shape_read_and_reopen_restores_authenticated_original(svc, v02_manual_like):
    f, x = v02_manual_like["f"], v02_manual_like["note"]
    t = th(svc, f, f"xref:{x}")
    assert t["done"] and t["effective_source"] == "native" and t["appearance"]["completion_style"] == "completed"
    a0 = annots_snapshot(f)
    o = svc.reopen_comment(f, f"xref:{x}", "reopen", rev(svc, f))
    assert o["appearance"]["action"] == "restore" and o["text"] == "Mark this as done"
    doc = pymupdf.open(f)
    keys = set(doc.xref_get_keys(x))
    assert doc.xref_get_key(x, "C") == ("array", "[1 1 0]") and "AP" not in keys and "PDFReviewStyle" not in keys
    doc.close()
    # completing again uses the corrected appearance logic: visible
    d = svc.mark_comment_done(f, f"xref:{x}", "done", o["revision"])
    assert d["text"] == "[DONE] Mark this as done" and dark(crop(f, 0, (95, 790, 135, 830))) > 50
    assert annots_snapshot(f) == a0


def test_unknown_record_version_is_never_upgraded(svc, root):
    f = str(root / "u.pdf")
    d = pymupdf.open()
    x = _add_raw_annot(d, d.new_page(), "<< /Type /Annot /Subtype /Text /Rect [60 680 84 704] /Contents (T) >>")
    d.xref_set_key(x, "PDFReviewState", pymupdf.get_pdf_str(json.dumps({"schema": "pdf-review-mcp/review-state", "v": 0})))
    d.save(f)
    d.close()
    data = open(f, "rb").read()
    for call in (lambda: svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f)),
                 lambda: svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))):
        with pytest.raises(ReviewError, match="invalid"):
            call()
    assert open(f, "rb").read() == data and not ReviewService().get_comment(f, f"xref:{x}", 0)["thread"]["done"]
