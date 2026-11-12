"""Visible completion styling with authenticated (signed) style records.

The native Review state (/State /Completed etc. via /IRT status markers) stays the single
source of truth. Styling is a *derived, reversible* visual cue on the root annotation only:

* apply (root is done): set /C to neutral grey and let PyMuPDF generate a NEW appearance
  stream; the old /AP is detached, so the original appearance stream objects stay untouched in
  the file. The exact previous /C and /AP, hashes of every referenced appearance object
  (dictionary + decoded stream), the applied grey state and a document/annotation anchor are
  bound by an HMAC-SHA256 made with a LOCAL owner-only key (see stylekey.py). The record
  (without the key) is stored in the private key /PDFReviewStyle.
* restore (root is open again): only if the record's HMAC verifies with this installation's
  key, the anchor matches THIS annotation, the current look is still exactly the applied grey,
  and every remembered appearance object is unchanged and a structurally valid Form XObject
  that is not page content. Then the remembered /C and /AP are put back without regenerating.

Anything else (no key, other installation's key, unknown/unsigned record versions, bad
signature, tampered streams, copied records) is SKIPPED with a reason; the annotation is left
as is and the record is not removed. Supported subtypes: Text and Highlight.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any

import pymupdf

from .pdfstore import Annot, ReviewError, _get_key, iso, now_pdf_date
from .stylekey import KeyUnavailable, load_key

KEY_STYLE = "PDFReviewStyle"
STYLE_SCHEMA = "pdf-review-mcp/completion-style"
STYLE_VERSION = 2
MAC_DOMAIN = b"pdf-review-mcp/completion-style/v2\0"
GREY = (0.55, 0.55, 0.55)
GREY_PDF = "[0.55 0.55 0.55]"
SUPPORTED_SUBTYPES = ("Text", "Highlight")
# Keys that styling is allowed to change on the root annotation; everything else is verified.
STYLE_KEYS = {"C", "AP", KEY_STYLE}
MAX_COORD = 1e6

_AP_DICT = re.compile(r"^<<\s*(?:/[NRD]\s+\d+\s+0\s+R\s*)+>>$")
_AP_REFS = re.compile(r"/[NRD]\s+(\d+)\s+0\s+R")
_NUM = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")


class StyleSkip(Exception):
    """Styling not applicable/safe; carries a machine-readable reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


# --------------------------------------------------------------------------- primitives


def _parse_color(raw: tuple[str, str]) -> list[float] | None:
    """Return list of components for an /C array (0,1,3,4 comps, each 0..1); None if absent."""
    t, v = raw
    if t == "null":
        return None
    if t != "array":
        raise StyleSkip("unsupported_color", f"/C is a {t}, not an array")
    toks = v.strip()[1:-1].split()
    if len(toks) not in (0, 1, 3, 4) or not all(_NUM.match(x) for x in toks):
        raise StyleSkip("unsupported_color", f"/C {v!r} is not a 0/1/3/4-component numeric array")
    vals = [float(x) for x in toks]
    if not all(0.0 <= c <= 1.0 for c in vals):
        raise StyleSkip("unsupported_color", f"/C {v!r} has components outside 0..1")
    return vals


def _color_pdf(vals: list[float] | None) -> str:
    if vals is None:
        return "null"
    return "[" + " ".join(f"{c:g}" for c in vals) + "]"


def _norm_c(src: str) -> str:
    if src == "null":
        return src
    try:
        return _color_pdf([float(t) for t in src.strip()[1:-1].split()])
    except ValueError:
        return src


def _nums(raw: tuple[str, str], n: int) -> list[float] | None:
    t, v = raw
    if t != "array":
        return None
    toks = v.strip()[1:-1].split()
    if len(toks) != n or not all(_NUM.match(x) for x in toks):
        return None
    vals = [float(x) for x in toks]
    if not all(math.isfinite(x) and abs(x) <= MAX_COORD for x in vals):
        return None
    return vals


def _page_content_xrefs(doc: pymupdf.Document) -> set[int]:
    out: set[int] = set()
    for page in doc:
        try:
            out.update(page.get_contents())
        except Exception:
            pass
    return out


def form_ok(doc: pymupdf.Document, n: int, page_contents: set[int] | None = None) -> str | None:
    """Return None if object n is a sane appearance Form XObject, else a reason string."""
    if not doc.xref_is_stream(n):
        return f"object {n} is not a stream"
    if page_contents is None:
        page_contents = _page_content_xrefs(doc)
    if n in page_contents:
        return f"object {n} is page content, not an appearance"
    if doc.xref_get_key(n, "Subtype") != ("name", "/Form"):
        return f"object {n} is not a Form XObject"
    t = doc.xref_get_key(n, "Type")
    if t[0] != "null" and t != ("name", "/XObject"):
        return f"object {n} has /Type {t[1]}"
    bbox = _nums(doc.xref_get_key(n, "BBox"), 4)
    if bbox is None:
        return f"object {n} has no finite 4-number /BBox"
    m = doc.xref_get_key(n, "Matrix")
    if m[0] != "null":
        mv = _nums(m, 6)
        if mv is None or abs(mv[0] * mv[3] - mv[1] * mv[2]) < 1e-9:
            return f"object {n} has an invalid /Matrix"
    r = doc.xref_get_key(n, "Resources")
    if r[0] not in ("null", "dict"):
        if r[0] == "xref":
            try:
                rx = int(r[1].split()[0])
            except (ValueError, IndexError):
                return f"object {n} has an invalid /Resources reference"
            if not doc.xref_object(rx, compressed=True).lstrip().startswith("<<"):
                return f"object {n} /Resources is not a dictionary"
        else:
            return f"object {n} /Resources is not a dictionary"
    return None


def stream_digest(doc: pymupdf.Document, n: int) -> str:
    blob = doc.xref_object(n, compressed=True).encode() + b"\0" + (doc.xref_stream(n) or b"")
    return hashlib.sha256(blob).hexdigest()


def _ap_snapshot(doc: pymupdf.Document, raw: tuple[str, str], reason: str) -> tuple[str | None, dict]:
    """(/AP source or None, {xref: digest}) for a simple dict of valid appearance Forms."""
    t, v = raw
    if t == "null":
        return None, {}
    if t != "dict" or not _AP_DICT.match(v.strip()):
        raise StyleSkip(reason, "/AP is not a simple dictionary of stream references "
                                "(e.g. has appearance states); left untouched")
    pc = _page_content_xrefs(doc)
    digests = {}
    for n in _AP_REFS.findall(v):
        n = int(n)
        bad = form_ok(doc, n, pc)
        if bad:
            raise StyleSkip(reason, bad)
        digests[str(n)] = stream_digest(doc, n)
    return v.strip(), digests


def _drop_keys(doc: pymupdf.Document, x: int, keys: tuple[str, ...]) -> None:
    """Really remove top-level keys (xref_set_key(..., "null") leaves a literal '/K null')."""
    for k in keys:
        doc.xref_set_key(x, k, "null")
    src = doc.xref_object(x, compressed=False)
    for k in keys:
        src = re.sub(rf"\n  /{re.escape(k)} null(?=\n)", "", src)
    doc.update_object(x, src)


def _current(doc: pymupdf.Document, x: int) -> tuple[str, str]:
    c = _get_key(doc, x, "C")
    ap = _get_key(doc, x, "AP")
    return (c[1] if c[0] != "null" else "null", ap[1] if ap[0] != "null" else "null")


def _doc_id(doc: pymupdf.Document) -> str:
    t, v = doc.xref_get_key(-1, "ID")
    if t != "array":
        return ""
    m = re.match(r"\[\s*(<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))", v)
    return m.group(1) if m else ""


def anchor(doc: pymupdf.Document, root: Annot) -> dict[str, Any]:
    """Identity the record is bound to (computed live, never stored)."""
    return {"doc_id": _doc_id(doc), "xref": root.xref, "page": root.page, "subtype": root.subtype,
            "nm": root.nm or "", "rect": doc.xref_get_key(root.xref, "Rect")[1]}


def _mac(key: bytes, record: dict[str, Any], anc: dict[str, Any]) -> str:
    body = {k: record[k] for k in ("schema", "v", "state", "prev", "applied", "at", "by", "kid")}
    payload = json.dumps({"record": body, "anchor": anc}, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode()
    return hmac.new(key, MAC_DOMAIN + payload, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- records


def _record(doc: pymupdf.Document, xref: int) -> dict[str, Any] | None:
    """Parse the record's shape. Raises StyleSkip(invalid/unauthenticated). Not yet verified."""
    t, v = _get_key(doc, xref, KEY_STYLE)
    if t == "null":
        return None
    if t != "string":
        raise StyleSkip("invalid_metadata", f"/{KEY_STYLE} is a {t}, not a string")
    try:
        m = json.loads(v)
    except ValueError:
        raise StyleSkip("invalid_metadata", f"/{KEY_STYLE} is not valid JSON") from None
    if not isinstance(m, dict) or m.get("schema") != STYLE_SCHEMA:
        raise StyleSkip("invalid_metadata", f"/{KEY_STYLE} has an unknown schema/shape")
    ok = (m.get("v") == STYLE_VERSION and m.get("state") == "completed"
          and isinstance(m.get("prev"), dict) and isinstance(m.get("applied"), dict)
          and isinstance(m.get("kid"), str) and isinstance(m.get("mac"), str)
          and isinstance(m["prev"].get("digests", None), dict)
          and isinstance(m["applied"].get("digests", None), dict))
    if not ok:
        raise StyleSkip("invalid_metadata", f"/{KEY_STYLE} has an unknown version/shape")
    return m




def _verify(doc: pymupdf.Document, root: Annot, m: dict[str, Any]) -> None:
    """Authenticate a parsed record for THIS annotation with THIS installation's key."""
    try:
        key, kid = load_key(create=False)
    except KeyUnavailable as exc:
        raise StyleSkip("style_key_unavailable", f"local style key unavailable: {exc}") from None
    if not hmac.compare_digest(m["kid"], kid):
        raise StyleSkip("style_key_mismatch",
                        "record was signed by another installation's key; left as is")
    if not hmac.compare_digest(m["mac"], _mac(key, m, anchor(doc, root))):
        raise StyleSkip("invalid_signature",
                        "record signature does not verify for this annotation (forged, copied or "
                        "altered); left as is")


def _matches_applied(doc: pymupdf.Document, x: int, m: dict[str, Any]) -> bool:
    c, ap = _current(doc, x)
    if _norm_c(c) != _norm_c(m["applied"].get("C", "")) or ap != m["applied"].get("AP"):
        return False
    for n, dg in m["applied"]["digests"].items():
        if not doc.xref_is_stream(int(n)) or stream_digest(doc, int(n)) != dg:
            return False
    return True


def _validate_prev(doc: pymupdf.Document, m: dict[str, Any]) -> tuple[str, str | None]:
    prev = m["prev"]
    c = prev.get("C")
    if c is None:
        c_pdf = "null"
    elif isinstance(c, list) and len(c) in (0, 1, 3, 4) and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1 for x in c):
        c_pdf = _color_pdf([float(x) for x in c])
    else:
        raise StyleSkip("invalid_metadata", "remembered colour is malformed; not restored")
    ap = prev.get("AP")
    if ap is None:
        if prev["digests"]:
            raise StyleSkip("invalid_metadata", "remembered appearance digests without /AP")
        return c_pdf, None
    if not isinstance(ap, str) or not _AP_DICT.match(ap):
        raise StyleSkip("invalid_metadata", "remembered appearance is malformed; not restored")
    refs = {str(int(n)) for n in _AP_REFS.findall(ap)}
    if refs != set(prev["digests"]):
        raise StyleSkip("invalid_metadata", "remembered appearance digests do not cover /AP")
    pc = _page_content_xrefs(doc)
    for n in refs:
        bad = form_ok(doc, int(n), pc)
        if bad:
            raise StyleSkip("original_appearance_changed", f"remembered appearance: {bad}; not restored")
        if stream_digest(doc, int(n)) != prev["digests"][n]:
            raise StyleSkip("original_appearance_changed",
                            f"remembered appearance object {n} was modified; not restored")
    return c_pdf, ap


# --------------------------------------------------------------------------- plan / execute


def plan(doc: pymupdf.Document, root: Annot, want_completed: bool) -> dict[str, Any]:
    """Decide what to do without modifying anything (never creates the key)."""
    base = {"subtype": root.subtype, "target": "completed" if want_completed else "open"}
    try:
        if root.subtype not in SUPPORTED_SUBTYPES:
            raise StyleSkip("unsupported_type",
                            f"{root.subtype} annotations are not restyled (supported: "
                            f"{', '.join(SUPPORTED_SUBTYPES)})")
        meta = _record(doc, root.xref)
        if want_completed:
            if meta is not None:
                _verify(doc, root, meta)
                if _matches_applied(doc, root.xref, meta):
                    return {**base, "action": "noop", "reason": "already_applied"}
                raise StyleSkip("modified_since_applied",
                                "colour/appearance was changed after completion styling; left as is")
            _parse_color(root.color_raw)
            _ap_snapshot(doc, _get_key(doc, root.xref, "AP"), "complex_appearance")
            return {**base, "action": "apply"}
        if meta is None:
            return {**base, "action": "noop", "reason": "not_styled"}
        _verify(doc, root, meta)
        if not _matches_applied(doc, root.xref, meta):
            return {**base, "action": "release", "reason": "modified_since_applied"}
        _validate_prev(doc, meta)
        return {**base, "action": "restore"}
    except StyleSkip as s:
        return {**base, "action": "skip", "reason": s.reason, "detail": s.detail}


def _keep_rect(doc: pymupdf.Document, x: int, rect_before: tuple[str, str], new_ap: str,
               subtype: str = "Highlight") -> None:
    """Keep the raw /Rect exactly.

    * Text (sticky note): PyMuPDF draws the icon in a LOCAL form space (/BBox [0 0 16 16]) and
      resizes /Rect to 16x16. Only /Rect is put back; the form /BBox stays as generated, and the
      spec's appearance algorithm maps BBox onto Rect (the icon scales to e.g. 24x24). Setting
      the BBox to the page-space Rect would clip the icon away (invisible note).
    * Text markup (Highlight): PyMuPDF draws in PAGE coordinates and pads /Rect. Put the original
      /Rect back and set the new form's /BBox to it (identity /Matrix required), so the shape is
      drawn 1:1 and only clipped to the original box, never scaled."""
    if doc.xref_get_key(x, "Rect") == rect_before:
        return
    if rect_before[0] != "array":
        raise ReviewError("annotation has no usable /Rect")
    if subtype == "Text":
        refs = _AP_REFS.findall(new_ap)
        for n in refs:
            bbox = _nums(doc.xref_get_key(int(n), "BBox"), 4)
            if bbox is None or bbox[2] - bbox[0] <= 0 or bbox[3] - bbox[1] <= 0:
                raise ReviewError("regenerated note appearance has no usable /BBox")
        doc.xref_set_key(x, "Rect", rect_before[1])
        return
    refs = _AP_REFS.findall(new_ap)
    if rect_before[0] != "array" or len(refs) != 1:
        raise ReviewError("regenerated appearance changed the annotation rectangle")
    n = int(refs[0])
    m = doc.xref_get_key(n, "Matrix")
    ident = m[0] == "null" or [float(t) for t in m[1].strip("[] ").split()] == [1, 0, 0, 1, 0, 0]
    if not ident:
        raise ReviewError("regenerated appearance has a non-identity matrix; rectangle not kept")
    doc.xref_set_key(n, "BBox", rect_before[1])
    doc.xref_set_key(x, "Rect", rect_before[1])


def execute(doc: pymupdf.Document, root: Annot, p: dict[str, Any], actor: str) -> dict[str, Any]:
    """Carry out a plan on the open document. On any unexpected error during the change the
    root object is restored byte-for-byte and the result reports reason 'appearance_error'."""
    action = p["action"]
    if action in ("noop", "skip"):
        return {**p, "changed": False}
    x = root.xref
    if action == "apply":
        try:
            key, kid = load_key(create=True)  # the ONLY place the key may be created
        except KeyUnavailable as exc:
            return {**p, "action": "skip", "reason": "style_key_unavailable",
                    "detail": f"local style key unavailable: {exc}", "changed": False}
    original = doc.xref_object(x, compressed=False)
    try:
        if action == "apply":
            prev_c = _parse_color(_get_key(doc, x, "C"))
            prev_ap, prev_dg = _ap_snapshot(doc, _get_key(doc, x, "AP"), "complex_appearance")
            rect_before = doc.xref_get_key(x, "Rect")
            doc.xref_set_key(x, "C", GREY_PDF)
            doc.xref_set_key(x, "AP", "null")  # detach: original stream objects stay intact
            page = doc[root.page - 1]
            page.load_annot(x).update()
            new_c, new_ap = _current(doc, x)
            if new_ap == "null" or not _AP_DICT.match(new_ap):
                raise ReviewError("appearance regeneration produced no usable /AP")
            _keep_rect(doc, x, rect_before, new_ap, root.subtype)
            _, applied_dg = _ap_snapshot(doc, ("dict", new_ap), "appearance_error")
            meta = {"schema": STYLE_SCHEMA, "v": STYLE_VERSION, "state": "completed",
                    "prev": {"C": prev_c, "AP": prev_ap, "digests": prev_dg},
                    "applied": {"C": new_c, "AP": new_ap, "digests": applied_dg},
                    "at": iso(now_pdf_date()), "by": actor, "kid": kid}
            meta["mac"] = _mac(key, meta, anchor(doc, root))
            doc.xref_set_key(x, KEY_STYLE, pymupdf.get_pdf_str(json.dumps(meta)))
            return {**p, "changed": True, "color": list(GREY), "previous_color": prev_c}
        meta = _record(doc, x)
        _verify(doc, root, meta)  # re-check right before mutating
        if action == "restore":
            c_pdf, ap = _validate_prev(doc, meta)
            drop = [KEY_STYLE]
            if c_pdf == "null":
                drop.append("C")
            else:
                doc.xref_set_key(x, "C", c_pdf)
            if ap is None:
                drop.append("AP")
            else:
                doc.xref_set_key(x, "AP", ap)
            _drop_keys(doc, x, tuple(drop))
            return {**p, "changed": True, "color": meta["prev"].get("C")}
        if action == "release":  # user restyled after completion: keep their look, drop record
            _drop_keys(doc, x, (KEY_STYLE,))
            return {**p, "changed": True, "detail": "current colour kept; styling record removed"}
        raise ReviewError(f"unknown appearance action {action!r}")
    except StyleSkip as s:
        doc.update_object(x, original)
        return {**p, "action": "skip", "reason": s.reason, "detail": s.detail, "changed": False}
    except Exception as exc:  # roll the root back exactly; state change (if any) still stands
        doc.update_object(x, original)
        return {**p, "action": "skip", "reason": "appearance_error", "detail": str(exc)[:200],
                "changed": False}


def root_keys(doc: pymupdf.Document, x: int, extra: tuple[str, ...] = ()) -> dict[str, tuple[str, str]]:
    skip = STYLE_KEYS | set(extra)
    return {k: doc.xref_get_key(x, k) for k in doc.xref_get_keys(x) if k not in skip}


def ap_streams(doc: pymupdf.Document, x: int) -> dict[int, bytes]:
    t, v = _get_key(doc, x, "AP")
    out: dict[int, bytes] = {}
    if t == "dict":
        for n in _AP_REFS.findall(v):
            n = int(n)
            if doc.xref_is_stream(n):
                out[n] = doc.xref_object(n, compressed=True).encode() + b"\0" + (doc.xref_stream_raw(n) or b"")
    return out


def describe_appearance(root: Annot, done: bool) -> dict[str, Any]:
    """Read-only description for list/get results (no key access, no document access)."""
    t, v = root.color_raw
    try:
        color = _parse_color(root.color_raw)
    except StyleSkip:
        color = v
    styled = None
    if root.style_raw is not None:
        try:
            m = json.loads(root.style_raw)
            if not (isinstance(m, dict) and m.get("schema") == STYLE_SCHEMA):
                styled = "invalid"
            elif m.get("v") == STYLE_VERSION and isinstance(m.get("mac"), str):
                styled = "completed"  # signed record (verified only when sync/reopen acts)
            else:
                styled = "invalid"
        except ValueError:
            styled = "invalid"
    supported = root.subtype in SUPPORTED_SUBTYPES
    if not supported:
        in_sync = True
    elif styled == "invalid":
        in_sync = None  # unknown: the record is not trusted; sync will skip it
    else:
        in_sync = done == (styled == "completed")
    return {"color": color, "completion_style": styled, "style_supported": supported,
            "in_sync": in_sync}
