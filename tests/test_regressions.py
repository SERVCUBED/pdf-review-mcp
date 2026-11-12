"""Regression tests for the independent-review blockers: status ordering, external-save race,
edit authorization, orphan/malformed annotations and post-write verification."""

from __future__ import annotations

import os

import pymupdf
import pytest

from pdf_review_mcp import pdfstore, service as service_mod
from pdf_review_mcp.pdfstore import ReviewError, StaleRevisionError, VerificationError

from conftest import _add_raw_annot, add_native_marker, annots_snapshot


def rev(svc, path):
    return svc.review_summary(path)["revision"]


def thread(svc, path, cid):
    return svc.get_comment(path, cid, context_chars=0)["thread"]


def build(path, markers, extra_pages=0, root_page_markers=True):
    """Root Text comment on page 1 plus raw status markers [(state, model, author, M, page)]."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Body text.")
    for _ in range(extra_pages):
        doc.new_page()
    root = _add_raw_annot(doc, doc[0], "<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] "
                                       "/T (Human) /Contents (Do the thing) >>")
    xs = []
    for state, model, author, m, page in markers:
        body = f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T ({author}) /IRT {root} 0 R /F 30"
        if state is not None:
            body += f" /State /{state}"
        if model is not None:
            body += f" /StateModel /{model}"
        if m is not None:
            body += f" /M ({m})"
        xs.append(_add_raw_annot(doc, doc[page - 1], body + " >>"))
    doc.save(path)
    doc.close()
    return f"xref:{root}", xs


# ----------------------------------------------------------------- (1) status ordering


def test_future_dated_existing_marker_does_not_mask_done(svc, root):
    rid, _ = build(root / "f.pdf", [("Accepted", "Review", "Alice", "D:20990101000000Z", 1)])
    assert thread(svc, str(root / "f.pdf"), rid)["status"] == "Accepted"
    d = svc.mark_comment_done(str(root / "f.pdf"), rid, "Did the thing in section 2", rev(svc, str(root / "f.pdf")))
    t = thread(svc, str(root / "f.pdf"), rid)
    assert d["state"] == "Completed" and t["status"] == "Completed" and t["done"]
    assert t["effective_source"] == "private" and t["effective_event_seq"] == d["event_seq"] == 1
    assert svc.list_comments(str(root / "f.pdf"), status="open")["total_matching"] == 0
    o = svc.reopen_comment(str(root / "f.pdf"), rid, "not yet", d["revision"])
    t = thread(svc, str(root / "f.pdf"), rid)
    assert t["status"] == "None" and not t["done"] and t["effective_event_seq"] == o["event_seq"] == 2


@pytest.mark.parametrize("dates", [
    (None, None),                                   # missing
    ("garbage", "D:2025-13-45"),                    # malformed
    ("D:20250101000000Z", "D:20250101000000Z"),     # equal
    ("D:20990101000000Z", "D:19990101000000Z"),     # later-positioned marker has older date
    ("D:99991231235959-12'00'", "D:00000000"),      # overflow / invalid
])
def test_ordering_uses_append_position_not_dates(svc, root, dates):
    rid, xs = build(root / "o.pdf", [("Completed", "Review", "A", dates[0], 1),
                                     ("Accepted", "Review", "B", dates[1], 1)])
    t = thread(svc, str(root / "o.pdf"), rid)
    assert t["status"] == "Accepted" and t["effective_marker_id"] == f"xref:{xs[1]}"
    d = svc.mark_comment_done(str(root / "o.pdf"), rid, "evidence", rev(svc, str(root / "o.pdf")))
    t = thread(svc, str(root / "o.pdf"), rid)
    assert t["done"] and t["effective_source"] == "private" and t["effective_marker_id"] is None
    assert [h["source"] for h in t["state_history"]] == ["native", "native", "server"]


def test_multiple_reviewers_reported_per_reviewer(svc, root):
    rid, xs = build(root / "m.pdf", [
        ("Completed", "Review", "Alice", "D:20990101000000Z", 1),
        ("Rejected", "Review", "Bob", "D:20200101000000Z", 1),
        ("Accepted", "Review", "Alice", None, 1),
    ])
    t = thread(svc, str(root / "m.pdf"), rid)
    assert t["status"] == "Accepted" and not t["done"]
    assert t["per_reviewer_state"] == {"Alice": "Accepted", "Bob": "Rejected"}
    d = svc.mark_comment_done(str(root / "m.pdf"), rid, "fixed", rev(svc, str(root / "m.pdf")), actor="Carol")
    t = thread(svc, str(root / "m.pdf"), rid)
    assert t["per_reviewer_state"]["Carol"] == "Completed" and t["status"] == "Completed"
    assert [h["actor"] for h in t["state_history"]] == ["Alice", "Bob", "Alice", "Carol"]


def test_existing_later_page_marker_is_accounted_then_new_native_events_supersede(svc, root):
    """0.6: a foreign marker on a later page made completion unenforceable. 0.7: markers that exist
    at a managed write are snapshotted (baseline); only NEW or EDITED native markers (e.g. set in a
    viewer afterwards) supersede the private state. Dates are never consulted."""
    path = root / "late.pdf"
    f = str(path)
    rid, (mx,) = build(path, [("Accepted", "Review", "X", "D:20990101000000Z", 2)], extra_pages=1)
    d = svc.mark_comment_done(f, rid, "evidence", rev(svc, f))
    t = thread(svc, f, rid)
    assert t["done"] and t["effective_source"] == "private"
    # A viewer adds a NEW native marker (old date on purpose): it supersedes.
    rx = int(rid.split(":")[1])
    nx = add_native_marker(f, rx, "None", "viewer reopened", author="Viewer")
    doc = pymupdf.open(f)
    doc.xref_set_key(nx, "M", "(D:19990101000000Z)")
    doc.saveIncr()
    doc.close()
    t = thread(svc, f, rid)
    assert t["status"] == "None" and t["effective_source"] == "native" and t["effective_marker_id"] == f"xref:{nx}"
    # sync adopts it: prefix removed, history extended, no annotation created or removed
    before = annots_snapshot(f)
    sy = svc.sync_comment_appearance(f, rid, rev(svc, f))
    assert sy["adopted"] == f"xref:{nx}" and annots_snapshot(f) == before
    t = thread(svc, f, rid)
    assert t["effective_source"] == "private" and t["text"] == "Do the thing" and not t["done"]
    # EDITING an accounted marker in place (viewer changes its state) also supersedes.
    doc = pymupdf.open(f)
    doc.xref_set_key(mx, "State", "/Completed")
    doc.saveIncr()
    doc.close()
    t = thread(svc, f, rid)
    assert t["done"] and t["effective_source"] == "native" and t["effective_marker_id"] == f"xref:{mx}"
    del d


# ----------------------------------------------------------------- (2) external save race


def test_external_save_during_write_is_not_overwritten(svc, plain, root, monkeypatch):
    original_add_root = service_mod.add_root
    external = plain.read_bytes() + b"\n% saved by a non-cooperating viewer\n"

    def add_root_then_external_save(*a, **k):
        out = original_add_root(*a, **k)
        plain.write_bytes(external)  # ignores our lock, after the first revision check
        return out

    monkeypatch.setattr(service_mod, "add_root", add_root_then_external_save)
    with pytest.raises(StaleRevisionError, match="changed on disk during the write"):
        svc.add_comment(str(root / "plain.pdf"), 1, "x", rev(svc, str(root / "plain.pdf")))
    assert plain.read_bytes() == external
    assert not [n for n in os.listdir(root) if n.endswith(".tmp")]


# ----------------------------------------------------------------- orphans & diagnostics


def test_orphan_irt_roots_are_actionable(svc, root):
    doc = pymupdf.open()
    page = doc.new_page()
    link = _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] >>")
    o1 = _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [60 600 80 620] /T (H) "
                                   f"/Contents (reply to link) /IRT {link} 0 R >>")
    o2 = _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Text /Rect [60 500 80 520] /T (H) "
                                   "/Contents (reply to missing) /IRT 9999 0 R >>")
    doc.save(root / "orph.pdf")
    doc.close()
    s = svc.review_summary(str(root / "orph.pdf"))
    assert s["root_comments"] == 2 and s["open"] == 2 and s["orphan_roots"] == 2
    assert sum(d["kind"] == "orphan_irt" for d in s["diagnostics"]) == 2
    for x in (o1, o2):
        cid = f"xref:{x}"
        t = thread(svc, str(root / "orph.pdf"), cid)
        assert "orphan_missing_irt_xref" in t
        d = svc.mark_comment_done(str(root / "orph.pdf"), cid, "handled", rev(svc, str(root / "orph.pdf")))
        assert thread(svc, str(root / "orph.pdf"), cid)["done"]
        r = svc.append_agent_response(str(root / "orph.pdf"), cid, "note", thread(svc, str(root / "orph.pdf"), cid)["text"],
                                      d["revision"])
        assert thread(svc, str(root / "orph.pdf"), cid)["text"].endswith("[test-agent] note") and r["changed"]
    assert svc.review_summary(str(root / "orph.pdf"))["open"] == 0


def test_irt_cycle_is_surfaced(svc, root):
    doc = pymupdf.open()
    page = doc.new_page()
    a = _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Text /Rect [60 600 80 620] /Contents (a) >>")
    b = _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [60 600 80 620] /Contents (b) /IRT {a} 0 R >>")
    doc.xref_set_key(a, "IRT", f"{b} 0 R")
    doc.save(root / "cyc.pdf")
    doc.close()
    s = svc.review_summary(str(root / "cyc.pdf"))
    assert s["root_comments"] >= 1
    assert any(d["kind"] == "irt_cycle" for d in s["diagnostics"])
    seen = {c["id"] for c in svc.list_comments(str(root / "cyc.pdf"))["comments"]}
    seen |= {r["id"] for c in svc.list_comments(str(root / "cyc.pdf"))["comments"] for r in c["replies"]}
    assert seen == {f"xref:{a}", f"xref:{b}"}  # nothing hidden


def test_orphan_and_malformed_status_markers_reported(svc, root):
    doc = pymupdf.open()
    page = doc.new_page()
    r = _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Text /Rect [60 600 80 620] /T (H) /Contents (root) >>")
    rep = _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [60 600 80 620] /Contents (reply) /IRT {r} 0 R >>")
    _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /State /Completed /StateModel /Review /IRT 9999 0 R >>")
    _add_raw_annot(doc, page, "<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /State /Completed /StateModel /Review >>")
    _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /State /Completed /StateModel /Review /IRT {rep} 0 R >>")
    _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /T (Q) /State /Completed /IRT {r} 0 R >>")
    _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /T (Z) /StateModel /Review /IRT {r} 0 R >>")
    _add_raw_annot(doc, page, f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /State /Weird /StateModel /Review /IRT {rep} 0 R >>")
    doc.save(root / "bad.pdf")
    doc.close()
    s = svc.review_summary(str(root / "bad.pdf"))
    kinds = [d["kind"] for d in s["diagnostics"]]
    assert kinds.count("orphan_status_marker") == 2
    assert "status_marker_targets_non_root" in kinds
    assert "status_marker_missing_state_model" in kinds
    assert "status_marker_missing_state" in kinds
    assert "unknown_review_state" in kinds
    assert s["root_comments"] == 1  # no marker is counted as a task
    t = thread(svc, str(root / "bad.pdf"), f"xref:{r}")
    # Completed (model inferred) then StateModel-only marker -> implicit None, effective & open.
    assert [h["state"] for h in t["state_history"]] == ["Completed", "None"]
    assert t["state_history"][1]["implicit_state"] is True
    assert t["status"] == "None" and not t["done"]
    g = svc.get_comment(str(root / "bad.pdf"), f"xref:{r}", 0)
    assert any(d["kind"] == "status_marker_missing_state" for d in g["diagnostics"])


# ----------------------------------------------------------------- runtime verification


def test_state_write_aborts_if_annotation_list_changes(svc, native, monkeypatch, root):
    """Zero-delta guarantee is enforced at runtime: any change to any page's /Annots aborts."""
    path, ids = native
    f = str(root / "native.pdf")
    real = service_mod.ReviewService._style_step

    def adding_style_step(doc, rootannot, *a, **k):
        x = doc.get_new_xref()
        doc.update_object(x, "<< /Type /Annot /Subtype /Popup /Rect [0 0 1 1] >>")
        pg = doc[rootannot.page - 1]
        t, v = doc.xref_get_key(pg.xref, "Annots")
        doc.xref_set_key(pg.xref, "Annots", v[:-1] + f" {x} 0 R]")
        return real(doc, rootannot, *a, **k)

    monkeypatch.setattr(service_mod.ReviewService, "_style_step", staticmethod(adding_style_step))
    before = path.read_bytes()
    with pytest.raises(VerificationError, match="annotation list changed"):
        svc.mark_comment_done(f, f"xref:{ids['F']}", "done", rev(svc, f))
    assert path.read_bytes() == before


def test_verification_detects_unrelated_annotation_change(svc, native, monkeypatch, root):
    path, ids = native
    f = str(root / "native.pdf")
    real = service_mod.ReviewService._style_step

    def tampering(doc, rootannot, *a, **k):
        doc.xref_set_key(ids["E"], "Contents", "(tampered)")
        return real(doc, rootannot, *a, **k)

    monkeypatch.setattr(service_mod.ReviewService, "_style_step", staticmethod(tampering))
    before = path.read_bytes()
    with pytest.raises(ReviewError, match="unrelated annotation object"):
        svc.mark_comment_done(f, f"xref:{ids['F']}", "done", rev(svc, f))
    assert path.read_bytes() == before


def test_state_verification_checks_persisted_record(svc, plain, monkeypatch, root):
    f = str(root / "plain.pdf")
    a = svc.add_comment(f, 1, "task", rev(svc, f))
    real_dump = service_mod.rs.dump
    calls = []

    def wrong_dump(r):
        calls.append(1)
        if len(calls) == 2:  # the persisted copy (1st call is the schema self-check)
            r = {**r, "events": [{**r["events"][0], "state": "None"}]}
        return real_dump(r)

    monkeypatch.setattr(service_mod.rs, "dump", wrong_dump)
    with pytest.raises(VerificationError, match="review-state record not persisted"):
        svc.mark_comment_done(f, a["id"], "done", a["revision"])
    monkeypatch.undo()
    assert not thread(svc, f, a["id"])["done"]
