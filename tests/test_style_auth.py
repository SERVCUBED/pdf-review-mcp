"""Authenticated completion-style records and local key safety.

All PDFs are generated fixtures; every test has its own XDG_CONFIG_HOME (conftest)."""

from __future__ import annotations

import json
import os
import stat
import threading

import pymupdf
import pytest

from pdf_review_mcp import appearance as style
from pdf_review_mcp import stylekey
from pdf_review_mcp.service import ReviewService

from conftest import _add_raw_annot, add_native_marker, add_native_reply, core_obj


def rev(svc, f):
    return svc.review_summary(f)["revision"]


def obj(path, x):
    """Style-relevant view of the annotation (everything except /M, /PDFReviewState and
    /Contents, which the review-state change manages and tests check separately)."""
    return core_obj(path, x)


def make(path, n_notes=2):
    """Text notes (blue/red), a yellow highlight, plus an unrelated Square (has a Form AP)."""
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.", fontsize=14)
    xs = []
    for i in range(n_notes):
        t = pg.add_text_annot((300 + 40 * i, 200), f"note {i}")
        t.set_colors(stroke=(0, 0, 1) if i == 0 else (1, 0, 0))
        t.update()
        xs.append(t.xref)
    hl = pg.add_highlight_annot(pg.search_for("quick brown")[0])
    sq = pg.add_rect_annot((72, 300, 200, 360))
    content = pg.get_contents()[0]
    d.save(path)
    d.close()
    return {"notes": xs, "hl": hl.xref, "sq": sq.xref, "content": content}


def set_key(f, x, k, v):
    d = pymupdf.open(f)
    d.xref_set_key(x, k, v)
    d.saveIncr()
    d.close()


def record(f, x):
    d = pymupdf.open(f)
    try:
        t, v = d.xref_get_key(x, style.KEY_STYLE)
        return json.loads(v) if t == "string" else None
    finally:
        d.close()


def put_record(f, x, m):
    set_key(f, x, style.KEY_STYLE, pymupdf.get_pdf_str(json.dumps(m) if not isinstance(m, str) else m))


def ap_of(f, x):
    d = pymupdf.open(f)
    try:
        return d.xref_get_key(x, "AP")[1]
    finally:
        d.close()


def key_file():
    return stylekey.key_path()


# ----------------------------------------------------------------- key lifecycle


def test_read_only_and_noop_calls_never_create_key(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    assert not key_file().parent.exists()
    svc.review_summary(f)
    svc.list_comments(f)
    svc.get_comment(f, f"xref:{ids['notes'][0]}")
    svc.get_comment_context(f, f"xref:{ids['hl']}")
    svc.read_page_text(f, 1)
    svc.list_pdfs(str(root))
    r = svc.sync_comment_appearance(f, f"xref:{ids['notes'][0]}", rev(svc, f))  # open, unstyled
    assert r["changed"] is False
    svc.mark_comment_done(f, f"xref:{ids['sq']}", "done", rev(svc, f))  # unsupported type
    svc.append_agent_response(f, f"xref:{ids['notes'][0]}", "hi", "note 0", rev(svc, f))
    assert not key_file().parent.exists()


def test_key_created_lazily_owner_only_and_never_exposed(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    d = svc.mark_comment_done(f, f"xref:{ids['notes'][0]}", "done", rev(svc, f))
    assert d["appearance"]["action"] == "apply"
    kp = key_file()
    assert stat.S_IMODE(os.stat(kp.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(kp).st_mode) == 0o600
    secret = kp.read_text().strip()
    assert len(secret) == 64
    raw = bytes.fromhex(secret)
    blob = open(f, "rb").read()
    assert secret.encode() not in blob and raw not in blob
    o = svc.reopen_comment(f, f"xref:{ids['notes'][0]}", "x", d["revision"])
    outputs = json.dumps([d, o, svc.get_comment(f, f"xref:{ids['notes'][0]}"), svc.review_summary(f)])
    assert secret not in outputs and secret.upper() not in outputs
    assert o["appearance"]["action"] == "restore"


def test_key_persists_across_service_instances(root):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    o0 = obj(f, x)
    s1 = ReviewService()
    d = s1.mark_comment_done(f, f"xref:{x}", "done", rev(s1, f))
    k1 = key_file().read_bytes()
    s2 = ReviewService()  # fresh instance, same local key
    o = s2.reopen_comment(f, f"xref:{x}", "x", d["revision"])
    assert o["appearance"]["action"] == "restore" and obj(f, x) == o0
    assert key_file().read_bytes() == k1


def test_concurrent_key_creation_agrees():
    out, errs = [], []

    def go():
        try:
            out.append(stylekey.load_key(create=True))
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len({k for k, _ in out}) == 1
    assert not [p for p in key_file().parent.iterdir() if p.name.endswith(".tmp")]


# ----------------------------------------------------------------- unsafe / missing key


def _greyed(svc, f, x):
    d = svc.mark_comment_done(f, f"xref:{x}", "done", rev(svc, f))
    assert d["appearance"]["action"] == "apply"
    return d


@pytest.mark.parametrize("breakage", ["missing", "mode644", "dirmode755", "symlink_file",
                                      "symlink_dir", "wrong_size", "garbage", "other_key"])
def test_restore_fails_closed_without_trusted_key(svc, root, tmp_path, breakage):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    d = _greyed(svc, f, x)
    kp = key_file()
    grey_obj = obj(f, x)
    if breakage == "missing":
        kp.unlink()
    elif breakage == "mode644":
        os.chmod(kp, 0o644)
    elif breakage == "dirmode755":
        os.chmod(kp.parent, 0o755)
    elif breakage == "symlink_file":
        real = tmp_path / "elsewhere.key"
        real.write_bytes(kp.read_bytes())
        os.chmod(real, 0o600)
        kp.unlink()
        kp.symlink_to(real)
    elif breakage == "symlink_dir":
        moved = tmp_path / "moved-dir"
        kp.parent.rename(moved)
        kp.parent.symlink_to(moved, target_is_directory=True)
    elif breakage == "wrong_size":
        kp.write_text("00" * 16 + "\n")
    elif breakage == "garbage":
        kp.write_text("zz" * 32 + "\n")
    elif breakage == "other_key":  # "other machine": a different valid key
        kp.write_text(os.urandom(32).hex() + "\n")
    modes = {p: stat.S_IMODE(os.lstat(p).st_mode) for p in (kp.parent,) if os.path.lexists(p)}
    o = svc.reopen_comment(f, f"xref:{x}", "reopen", d["revision"])
    assert o["state"] == "None"  # native state change still works
    a = o["appearance"]
    expected = "style_key_mismatch" if breakage == "other_key" else "style_key_unavailable"
    assert a["action"] == "skip" and a["reason"] == expected and a["warning"]
    assert obj(f, x) == grey_obj  # current style preserved, record not removed
    if breakage == "missing":
        assert not kp.exists()  # restore never creates a key
    assert {p: stat.S_IMODE(os.lstat(p).st_mode) for p in modes} == modes  # never broadened


def test_apply_with_unsafe_key_skips_styling_keeps_state(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    stylekey.load_key(create=True)
    os.chmod(key_file(), 0o640)
    x = ids["notes"][0]
    o0 = obj(f, x)
    d = svc.mark_comment_done(f, f"xref:{x}", "done", rev(svc, f))
    assert d["state"] == "Completed" and d["appearance"]["reason"] == "style_key_unavailable"
    assert obj(f, x) == o0
    assert stat.S_IMODE(os.stat(key_file()).st_mode) == 0o640  # not "fixed" silently


# ----------------------------------------------------------------- forgeries


def _forge(cur_c, cur_ap, prev_ap, prev_c=(1, 0, 0), mac="0" * 64, kid=None, digests=None):
    return {"schema": style.STYLE_SCHEMA, "v": 2, "state": "completed",
            "prev": {"C": list(prev_c), "AP": prev_ap, "digests": digests or {}},
            "applied": {"C": cur_c, "AP": cur_ap, "digests": {}},
            "at": "2026-01-01T00:00:00Z", "by": "attacker", "kid": kid or "0" * 16, "mac": mac}


@pytest.mark.parametrize("with_key", [False, True])
def test_critic_forgery_page_content_ref_on_open_text(svc, root, with_key):
    """Schema-valid forged record on an OPEN Text note pointing prev.AP at the page content
    stream with prev.C red: sync must not restore anything."""
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    if with_key:
        _, kid = stylekey.load_key(create=True)
    else:
        kid = None
    cur_ap = ap_of(f, x)
    put_record(f, x, _forge("[0 0 1]", cur_ap, f"<</N {ids['content']} 0 R>>", kid=kid,
                            digests={str(ids["content"]): "0" * 64}))
    o_before = obj(f, x)
    data = open(f, "rb").read()
    r = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))
    assert r["changed"] is False and r["appearance"]["action"] == "skip"
    assert r["appearance"]["reason"] == ("invalid_signature" if with_key else "style_key_unavailable")
    assert open(f, "rb").read() == data and obj(f, x) == o_before
    if not with_key:
        assert not key_file().exists()


def test_forgery_with_valid_unrelated_form_ref(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    _, kid = stylekey.load_key(create=True)
    x = ids["notes"][0]
    sq_form = int(ap_of(f, ids["sq"]).split()[1])
    d = pymupdf.open(f)
    assert style.form_ok(d, sq_form) is None  # structurally a fine Form -> signature must stop it
    assert "page content" in style.form_ok(d, ids["content"])
    dg = style.stream_digest(d, sq_form)
    d.close()
    put_record(f, x, _forge("[0 0 1]", ap_of(f, x), f"<</N {sq_form} 0 R>>", kid=kid,
                            digests={str(sq_form): dg}))
    o_before = obj(f, x)
    r = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))  # note is open -> would restore
    assert r["appearance"]["reason"] == "invalid_signature" and obj(f, x) == o_before


def test_correctly_signed_record_pointing_at_page_content_still_rejected(svc, root):
    """Defence in depth: even a record that verifies (e.g. stolen key) cannot re-point /AP to
    page content or to a non-Form stream."""
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    key, kid = stylekey.load_key(create=True)
    m = _forge("[0 0 1]", ap_of(f, x), f"<</N {ids['content']} 0 R>>", kid=kid)
    d = pymupdf.open(f)
    m["prev"]["digests"] = {str(ids["content"]): style.stream_digest(d, ids["content"])}
    m["applied"]["digests"] = {n: style.stream_digest(d, int(n))
                               for n in style._AP_REFS.findall(ap_of(f, x))}
    root_annot = next(a for a in __import__("pdf_review_mcp.pdfstore", fromlist=["x"])
                      .parse_document(d).annots.values() if a.xref == x)
    m["mac"] = style._mac(key, m, style.anchor(d, root_annot))
    d.close()
    put_record(f, x, m)
    o_before = obj(f, x)
    r = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))
    assert r["appearance"]["reason"] == "original_appearance_changed"
    assert "page content" in r["appearance"]["detail"] and obj(f, x) == o_before


def test_copied_signed_record_to_other_annotation_rejected(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    a, b = ids["notes"]
    _greyed(svc, f, a)
    m = record(f, a)
    d = pymupdf.open(f)  # make B look exactly like A's applied state, then paste A's record
    d.xref_set_key(b, "C", d.xref_get_key(a, "C")[1])
    d.xref_set_key(b, "AP", d.xref_get_key(a, "AP")[1])
    d.xref_set_key(b, style.KEY_STYLE, pymupdf.get_pdf_str(json.dumps(m)))
    d.saveIncr()
    d.close()
    o_b = obj(f, b)
    r = svc.sync_comment_appearance(f, f"xref:{b}", rev(svc, f))  # B is open
    assert r["appearance"]["reason"] == "invalid_signature" and obj(f, b) == o_b


def test_copied_signed_record_to_other_document_rejected(svc, root):
    f1, f2 = str(root / "one.pdf"), str(root / "two.pdf")
    ids1, ids2 = make(f1), make(f2)
    assert ids1["notes"] == ids2["notes"]  # same xref numbers, different trailer /ID
    _greyed(svc, f1, ids1["notes"][0])
    m = record(f1, ids1["notes"][0])
    x = ids2["notes"][0]
    d1, d2 = pymupdf.open(f1), pymupdf.open(f2)
    assert style._doc_id(d1) != style._doc_id(d2)
    applied = {k: d1.xref_get_key(ids1["notes"][0], k)[1] for k in ("C", "AP")}
    d1.close()
    d2.close()
    set_key(f2, x, "C", applied["C"])
    put_record(f2, x, m)
    o2 = obj(f2, x)
    r = svc.sync_comment_appearance(f2, f"xref:{x}", rev(svc, f2))
    assert r["appearance"]["reason"] == "invalid_signature" and obj(f2, x) == o2


def test_tampered_original_appearance_stream_not_restored(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["hl"]
    d0 = pymupdf.open(f)
    orig_form = int(d0.xref_get_key(x, "AP")[1].split()[1])
    d0.close()
    d = _greyed(svc, f, x)
    doc = pymupdf.open(f)
    doc.update_stream(orig_form, b"1 0 0 rg 0 0 1000 1000 re f")  # attacker edits detached original
    doc.saveIncr()
    doc.close()
    grey = obj(f, x)
    o = svc.reopen_comment(f, f"xref:{x}", "x", rev(svc, f))
    assert o["state"] == "None" and o["appearance"]["reason"] == "original_appearance_changed"
    assert obj(f, x) == grey


@pytest.mark.parametrize("mutate,reason", [
    (lambda m: m.update(mac="zz"), "invalid_signature"),
    (lambda m: m.update(mac=m["mac"][:-1] + ("0" if m["mac"][-1] != "0" else "1")), "invalid_signature"),
    (lambda m: m.pop("mac"), "invalid_metadata"),
    (lambda m: m.update(kid="f" * 16), "style_key_mismatch"),
    (lambda m: m["prev"].update(C=[1, 0, 0]), "invalid_signature"),
    (lambda m: m.update(by="someone else"), "invalid_signature"),
    (lambda m: m.update(v=3), "invalid_metadata"),
])
def test_malformed_or_altered_signatures_cannot_mutate(svc, root, mutate, reason):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][1]
    d = _greyed(svc, f, x)
    m = record(f, x)
    mutate(m)
    put_record(f, x, m)
    grey = obj(f, x)
    s = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))  # done: would be noop/skip
    assert s["changed"] is False
    o = svc.reopen_comment(f, f"xref:{x}", "x", rev(svc, f))
    assert o["appearance"]["reason"] == reason and obj(f, x) == grey


def test_unknown_or_unsigned_record_version_fails_closed_not_removed(svc, root):
    """Records with any version other than the signed v2 (e.g. an unsigned v1) are not
    special-cased: they are invalid_metadata, never trusted, never removed."""
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    v1 = {"schema": style.STYLE_SCHEMA, "v": 1, "state": "completed",
          "prev": {"C": [1, 0, 0], "AP": None}, "applied": {"C": "[0 0 1]", "AP": ap_of(f, x)}}
    put_record(f, x, v1)
    t = svc.get_comment(f, f"xref:{x}", 0)["thread"]
    assert t["appearance"]["completion_style"] == "invalid" and t["appearance"]["in_sync"] is None
    assert t["id"] in svc.review_summary(f)["appearance_out_of_sync_ids"]  # flagged, not trusted
    o_before = obj(f, x)
    s = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))
    assert s["changed"] is False and s["appearance"]["reason"] == "invalid_metadata"
    d = svc.mark_comment_done(f, f"xref:{x}", "done", rev(svc, f))
    assert d["appearance"]["reason"] == "invalid_metadata" and d["appearance"]["warning"]
    assert record(f, x) == v1  # record kept
    assert obj(f, x) == o_before


def test_manual_recolour_with_signed_record_released_safely(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    _greyed(svc, f, x)
    set_key(f, x, "C", "[0 1 0]")
    o = svc.reopen_comment(f, f"xref:{x}", "x", rev(svc, f))
    assert o["appearance"]["action"] == "release"
    doc = pymupdf.open(f)
    assert doc.xref_get_key(x, "C") == ("array", "[0 1 0]") and doc.xref_get_key(x, style.KEY_STYLE)[0] == "null"
    doc.close()


def test_appearance_stream_edited_after_apply_counts_as_manual_change(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    x = ids["notes"][0]
    _greyed(svc, f, x)
    applied = int(ap_of(f, x).split()[1])
    doc = pymupdf.open(f)
    doc.update_stream(applied, b"0 1 0 rg 0 0 16 16 re f")
    doc.saveIncr()
    doc.close()
    s = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))
    assert s["appearance"]["reason"] == "modified_since_applied" and s["changed"] is False


def test_signed_happy_path_byte_exact(svc, root):
    f = str(root / "a.pdf")
    ids = make(f)
    before = {x: obj(f, x) for x in (*ids["notes"], ids["hl"])}
    r = rev(svc, f)
    for x in (*ids["notes"], ids["hl"]):
        d = svc.mark_comment_done(f, f"xref:{x}", "done", r)
        assert d["appearance"]["action"] == "apply"
        r = d["revision"]
        m = record(f, x)
        assert m["v"] == 2 and len(m["mac"]) == 64 and "key" not in json.dumps(m).lower()
    for x in (*ids["notes"], ids["hl"]):
        o = svc.reopen_comment(f, f"xref:{x}", "x", r)
        assert o["appearance"]["action"] == "restore"
        r = o["revision"]
    assert {x: obj(f, x) for x in before} == before
