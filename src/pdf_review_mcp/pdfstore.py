"""PDF annotation parsing and safe, revision-checked, atomic, in-PDF persistence.

Revision contract
-----------------
``revision`` is ``"sha256:" + hex(SHA-256 of the exact PDF file bytes)``. Every read
returns the revision of the snapshot it parsed (bytes are read once, hashed, then
parsed from memory). Every write requires ``expected_revision``; under an exclusive
cross-process lock the file is re-read and re-hashed, and the write is rejected with
``StaleRevisionError`` if it differs. The live file is re-hashed once more immediately
before the atomic ``os.replace``; a change there also aborts with ``StaleRevisionError``
and the external bytes are kept. A successful write returns the new revision. Any byte
change by any program changes the revision, so callers must re-read after a conflict.

Residual risk: the lock is cooperative. A program that ignores it and saves in the tiny
window between the final re-hash and ``os.replace`` can still lose its save; close the PDF
in viewers (or hand the file over exclusively) while agents write.

Status ordering
---------------
The effective Review state of a root comment is the LAST Review-model status marker in
append order = (page index, position in that page's /Annots array). /M and /CreationDate
are reported but never used for ordering. New markers are appended to the end of the root's
/Annots array, and every status write verifies the requested state became effective.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pymupdf
from . import posixlock

TOOL_MARK = "pdf-review-mcp"
# Private (non-standard) keys. KEY_TOOL is an unauthenticated provenance hint (anyone can
# write it); it is never used for authorization. KEY_HISTORY holds the in-PDF edit history.
KEY_TOOL = "PDFReviewTool"
KEY_HISTORY = "PDFReviewHistory"
ID_PREFIX = "rv-"

REVIEW_MODEL = "Review"
MARKED_MODEL = "Marked"
REVIEW_STATES = {"None", "Accepted", "Rejected", "Cancelled", "Completed"}
CLOSED_STATES = {"Completed", "Cancelled", "Rejected"}

# Annotation subtypes that are never review comments.
NON_COMMENT_TYPES = {
    "Popup", "Link", "Widget", "PrinterMark", "TrapNet", "Watermark", "3D",
    "Screen", "Movie", "RichMedia", "Projection",
}

MAX_TEXT = 20_000
MAX_ACTOR = 200
MAX_HISTORY_ENTRIES = 100
LOCK_TIMEOUT_S = float(os.environ.get("PDF_REVIEW_LOCK_TIMEOUT", "10"))
MAX_PDF_BYTES = int(os.environ.get("PDF_REVIEW_MAX_BYTES", str(200 * 1024 * 1024)))

# Flags: NoZoom(8) | NoRotate(16); status markers additionally Hidden(2).


class ReviewError(Exception):
    """User-facing error (bad id, policy refusal, ...)."""


class StaleRevisionError(ReviewError):
    pass


class WriteBlockedError(ReviewError):
    pass


class LockBusyError(ReviewError):
    pass


# --------------------------------------------------------------------------- helpers


def revision_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def now_pdf_date() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")


_DATE_RE = re.compile(
    r"^D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz+\-])?(\d{2})?'?(\d{2})?'?"
)


def parse_pdf_date(s: str | None) -> datetime | None:
    if not s:
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, sign, tzh, tzm = m.groups()
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(sec or 0))
    except ValueError:
        return None
    offset = timedelta(0)
    if sign in ("+", "-") and tzh:
        offset = timedelta(hours=int(tzh), minutes=int(tzm or 0))
        if sign == "-":
            offset = -offset
    try:
        return (dt - offset).replace(tzinfo=timezone.utc)
    except OverflowError:
        return None


def iso(s: str | None) -> str | None:
    dt = parse_pdf_date(s)
    return dt.isoformat().replace("+00:00", "Z") if dt else (s or None)


def clean_text(value: str, what: str, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReviewError(f"{what} must be a string")
    value = value.replace("\x00", "")
    if not allow_empty and not value.strip():
        raise ReviewError(f"{what} must not be empty")
    if len(value) > max_len:
        raise ReviewError(f"{what} is too long ({len(value)} > {max_len} chars)")
    return value


def _get_key(doc: pymupdf.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return ("null", "null")


def _name(doc: pymupdf.Document, xref: int, key: str) -> str | None:
    t, v = _get_key(doc, xref, key)
    if t == "name":
        return v.lstrip("/")
    if t == "string":
        return v
    return None


def _ref(doc: pymupdf.Document, xref: int, key: str) -> int | None:
    t, v = _get_key(doc, xref, key)
    if t == "xref":
        try:
            return int(v.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _string(doc: pymupdf.Document, xref: int, key: str) -> str | None:
    t, v = _get_key(doc, xref, key)
    return v if t == "string" else None


# --------------------------------------------------------------------------- model


@dataclass
class Annot:
    xref: int
    page: int  # 1-based
    order: tuple[int, int]  # (page index, index in that page's /Annots array) = append order
    subtype: str
    nm: str | None
    author: str
    contents: str
    subject: str
    created: str | None
    modified: str | None
    rect: list[float]
    irt: int | None
    rt: str | None  # "R" (reply) or "Group"
    state: str | None
    state_model: str | None
    has_state_key: bool
    has_rich_text: bool
    tool_marker: bool  # unauthenticated provenance hint only; never used for authorization
    history: list[dict[str, Any]]
    color_raw: tuple[str, str] = ("null", "null")
    style_raw: str | None = None
    state_raw: str | None = None
    private: dict[str, Any] | None = None  # validated /PDFReviewState record (roots only)
    private_error: str | None = None
    id: str = ""
    orphan: bool = False  # /IRT points to a missing or non-comment object
    children: list["Annot"] = field(default_factory=list)  # replies
    states: list["Annot"] = field(default_factory=list)  # status markers targeting this
    group_members: list["Annot"] = field(default_factory=list)

    @property
    def is_state(self) -> bool:
        return self.has_state_key or self.state_model is not None

    @property
    def effective_model(self) -> str:
        """/StateModel, or inferred per ISO 32000 when missing (Marked/Unmarked -> Marked)."""
        if self.state_model:
            return self.state_model
        return MARKED_MODEL if self.state in ("Marked", "Unmarked") else REVIEW_MODEL

    @property
    def is_reply(self) -> bool:
        return self.irt is not None and not self.is_state and (self.rt or "R") == "R"

    @property
    def is_group_member(self) -> bool:
        return self.irt is not None and not self.is_state and self.rt == "Group"

    @property
    def is_root(self) -> bool:
        """Actionable root task: no /IRT, or an orphan whose /IRT target is missing."""
        return not self.is_state and (self.irt is None or self.orphan)

    def entry(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "author": self.author,
            "text": self.contents,
            "created": iso(self.created),
            "modified": iso(self.modified),
            "tool_marker": self.tool_marker,
        }
        if self.history:
            d["edit_history"] = self.history
        if self.has_rich_text:
            d["has_rich_text"] = True
        return d


@dataclass
class ReviewModel:
    annots: dict[int, Annot]
    by_id: dict[str, Annot]
    roots: list[Annot]
    page_count: int
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def find(self, comment_id: str) -> Annot:
        a = self.by_id.get(comment_id)
        if a is None and comment_id.startswith("xref:"):
            try:
                a = self.annots.get(int(comment_id[5:]))
            except ValueError:
                a = None
        if a is None:
            raise ReviewError(f"Unknown comment id: {comment_id!r}")
        return a

    def root_of(self, a: Annot) -> Annot:
        seen = set()
        cur = a
        while not cur.is_root and cur.irt in self.annots and cur.xref not in seen:
            seen.add(cur.xref)
            cur = self.annots[cur.irt]
        return cur

    @staticmethod
    def review_status(root: Annot) -> dict[str, Any]:
        """Effective review state of a root.

        * Native Review-model markers (written by viewers/other tools) are read in document
          append order; dates are never used for ordering.
        * If the root carries a valid private /PDFReviewState record, its last event
          is effective UNLESS a native marker not in the record's baseline snapshot (new or
          edited since the last managed write) exists; then the last such native marker wins.
        """
        natives = sorted((s for s in root.states if s.effective_model == REVIEW_MODEL),
                         key=lambda s: s.order)
        rec = root.private
        known = {b["id"]: b["digest"] for b in rec["baseline"]} if rec else {}

        def native_entry(s, accounted):
            st = s.state if s.state else "None"
            h = {"source": "native", "id": s.id, "state": st, "actor": s.author, "note": s.contents,
                 "at": iso(s.modified or s.created), "accounted_in_private_baseline": accounted}
            if not s.state:
                h["implicit_state"] = True  # /State missing: treated as None
            return h

        if rec is None:
            history = [native_entry(s, False) for s in natives]
            for h in history:
                h.pop("accounted_in_private_baseline")
            eff = history[-1] if history else None
            current = eff["state"] if eff else "None"
            src = "native" if eff else "none"
        else:
            from .reviewstate import native_digest
            sup = [s for s in natives if known.get(s.id) != native_digest(s)]
            base = [native_entry(s, True) for s in natives if s not in sup]
            priv = [{"source": e["source"], "seq": e["seq"], "state": e["state"], "actor": e["actor"],
                     "note": e["note"], "at": e["at"], **({"native_id": e["native_id"]} if "native_id" in e else {})}
                    for e in rec["events"]]
            new = [native_entry(s, False) for s in sup]
            history = base + priv + new
            eff = new[-1] if new else priv[-1]
            current = eff["state"]
            src = "native" if new else "private"
        per_reviewer: dict[str, str] = {}
        for h in history:
            per_reviewer[h["actor"] or "(unknown)"] = h["state"]
        return {
            "state": current,
            "done": current in CLOSED_STATES,
            "effective_source": src,
            "effective_marker_id": eff["id"] if eff and eff["source"] == "native" else None,
            "effective_event_seq": eff.get("seq") if eff and src == "private" else None,
            "per_reviewer": per_reviewer,
            "state_history": history,
        }


def _decode_history(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def parse_document(doc: pymupdf.Document) -> ReviewModel:
    annots: dict[int, Annot] = {}
    diagnostics: list[dict[str, Any]] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        for idx, (xref, _atype, _aid) in enumerate(page.annot_xrefs()):
            subtype = _name(doc, xref, "Subtype") or "Unknown"
            if subtype in NON_COMMENT_TYPES:
                continue
            try:
                pa = page.load_annot(xref)
            except Exception:
                pa = None
                diagnostics.append({"kind": "annotation_load_failed", "xref": xref, "page": pno + 1})
            info = pa.info if pa is not None else {}
            rect = list(pa.rect) if pa is not None else [0.0, 0.0, 0.0, 0.0]
            has_state = _get_key(doc, xref, "State")[0] != "null"
            annots[xref] = Annot(
                xref=xref,
                page=pno + 1,
                order=(pno, idx),
                subtype=subtype,
                nm=(info.get("id") or _string(doc, xref, "NM")) or None,
                author=info.get("title", "") or "",
                contents=info.get("content", "") or "",
                subject=info.get("subject", "") or "",
                created=info.get("creationDate") or None,
                modified=info.get("modDate") or None,
                rect=[round(v, 2) for v in rect],
                irt=_ref(doc, xref, "IRT"),
                rt=_name(doc, xref, "RT"),
                state=_name(doc, xref, "State") if has_state else None,
                state_model=_name(doc, xref, "StateModel"),
                has_state_key=has_state,
                has_rich_text=_get_key(doc, xref, "RC")[0] != "null",
                tool_marker=_string(doc, xref, KEY_TOOL) == TOOL_MARK,
                color_raw=_get_key(doc, xref, "C"),
                style_raw=_string(doc, xref, "PDFReviewStyle"),
                state_raw=_string(doc, xref, "PDFReviewState"),
                history=_decode_history(_string(doc, xref, KEY_HISTORY)),
            )
    # Stable ids: unique /NM if present, else "xref:<n>".
    nm_counts: dict[str, int] = {}
    for a in annots.values():
        if a.nm:
            nm_counts[a.nm] = nm_counts.get(a.nm, 0) + 1
    by_id: dict[str, Annot] = {}
    for a in annots.values():
        a.id = a.nm if a.nm and nm_counts[a.nm] == 1 and not a.nm.startswith("xref:") else f"xref:{a.xref}"
        by_id[a.id] = a
    for nm, n in nm_counts.items():
        if n > 1:
            diagnostics.append({"kind": "duplicate_nm", "nm": nm, "count": n,
                                "detail": "ids fall back to xref:<n>"})
    # Orphans first, so is_root is final before attaching children/states.
    for a in annots.values():
        if a.irt is not None and a.irt not in annots and not a.is_state:
            a.orphan = True
            diagnostics.append({"kind": "orphan_irt", "id": a.id, "page": a.page,
                                "missing_irt_xref": a.irt,
                                "detail": "parent missing; treated as an actionable root"})
    # /IRT cycles (A->B->A) would hide every member; promote them to orphan roots.
    for a in annots.values():
        if a.is_state or a.orphan or a.irt is None:
            continue
        seen, cur = {a.xref}, annots.get(a.irt)
        while cur is not None and not cur.is_state and cur.irt is not None and not cur.orphan:
            if cur.xref in seen:
                a.orphan = True
                diagnostics.append({"kind": "irt_cycle", "id": a.id, "page": a.page,
                                    "detail": "reply chain loops; treated as an actionable root"})
                break
            seen.add(cur.xref)
            cur = annots.get(cur.irt)
    roots: list[Annot] = []
    for a in sorted(annots.values(), key=lambda x: x.order):
        target = annots.get(a.irt) if a.irt is not None else None
        if a.is_state:
            d = {"id": a.id, "page": a.page, "state": a.state, "state_model": a.state_model}
            if target is None:
                diagnostics.append({"kind": "orphan_status_marker", **d, "missing_irt_xref": a.irt,
                                    "detail": "status marker with no valid target; ignored"})
                continue
            if not a.has_state_key:
                diagnostics.append({"kind": "status_marker_missing_state", **d,
                                    "detail": "/State missing; treated as None"})
            if not a.state_model:
                diagnostics.append({"kind": "status_marker_missing_state_model", **d,
                                    "detail": f"/StateModel missing; inferred {a.effective_model}"})
            if a.effective_model == REVIEW_MODEL and a.state and a.state not in REVIEW_STATES:
                diagnostics.append({"kind": "unknown_review_state", **d,
                                    "detail": "not a standard Review state; counts as open"})
            if not target.is_root:
                diagnostics.append({"kind": "status_marker_targets_non_root", **d, "target_id": target.id,
                                    "detail": "only markers on root comments affect status"})
            target.states.append(a)
        elif a.is_root:
            roots.append(a)
        elif a.is_group_member:
            target.group_members.append(a)
        elif a.is_reply:
            target.children.append(a)
        else:  # e.g. unknown /RT value
            diagnostics.append({"kind": "unknown_reply_type", "id": a.id, "rt": a.rt,
                                "detail": "treated as a reply"})
            target.children.append(a)
    from .reviewstate import RecordError, parse as parse_state
    for a in annots.values():
        if a.state_raw is None:
            continue
        if not a.is_root:
            diagnostics.append({"kind": "review_state_record_on_non_root", "id": a.id,
                                "detail": "private review-state record ignored on a non-root annotation"})
            continue
        try:
            a.private = parse_state(a.state_raw)
        except RecordError as exc:
            a.private_error = str(exc)
            diagnostics.append({"kind": "invalid_review_state_record", "id": a.id, "page": a.page,
                                "detail": f"{exc}; ignored for status (native markers only); "
                                          "state changes on this root are refused"})
    roots.sort(key=lambda r: (r.page, r.rect[1] if r.rect else 0, r.rect[0] if r.rect else 0, r.xref))
    return ReviewModel(annots=annots, by_id=by_id, roots=roots, page_count=doc.page_count,
                       diagnostics=diagnostics)


def thread_dict(model: ReviewModel, root: Annot, include_replies: bool = True) -> dict[str, Any]:
    status = model.review_status(root)

    def walk(a: Annot, depth: int, seen: set[int]) -> list[dict[str, Any]]:
        out = []
        for c in a.children:  # already in append order
            if c.xref in seen:
                continue
            seen.add(c.xref)
            e = c.entry()
            e["depth"] = depth
            e["in_reply_to"] = a.id
            out.append(e)
            out.extend(walk(c, depth + 1, seen))
        return out

    replies = walk(root, 1, {root.xref})
    d: dict[str, Any] = {
        "id": root.id,
        "page": root.page,
        "type": root.subtype,
        "rect": root.rect,
        **root.entry(),
        "subject": root.subject,
        "status": status["state"],
        "done": status["done"],
        "reply_count": len(replies),
    }
    if root.orphan:
        d["orphan_missing_irt_xref"] = root.irt
    from .appearance import describe_appearance  # local import: appearance imports pdfstore

    d["appearance"] = describe_appearance(root, status["done"])
    if include_replies:
        d["replies"] = replies
        d["state_history"] = status["state_history"]
        d["effective_marker_id"] = status["effective_marker_id"]
        d["effective_source"] = status["effective_source"]
        d["effective_event_seq"] = status["effective_event_seq"]
        if root.private is not None:
            d["managed_prefix"] = root.private["prefix"]
        if root.private_error:
            d["review_state_record_error"] = root.private_error
        d["per_reviewer_state"] = status["per_reviewer"]
        if root.group_members:
            d["group_member_ids"] = [g.id for g in root.group_members]
    return d


# --------------------------------------------------------------------------- safety


def security_info(doc: pymupdf.Document) -> dict[str, Any]:
    sigflags = -1
    try:
        sigflags = doc.get_sigflags()
    except Exception:
        pass
    has_sig_field = False
    try:
        for page in doc:
            for w in page.widgets() or []:
                if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                    has_sig_field = True
                    break
            if has_sig_field:
                break
    except Exception:
        pass
    encrypted = bool(doc.is_encrypted or doc.needs_pass or (doc.metadata or {}).get("encryption"))
    signed = sigflags > 0 or has_sig_field
    reasons = []
    if encrypted:
        reasons.append("encrypted")
    if signed:
        reasons.append("signed or has signature fields")
    if getattr(doc, "is_repaired", False):
        reasons.append("file needed repair when opened")
    return {"encrypted": encrypted, "signed": signed, "writable": not reasons, "write_blockers": reasons}


# --------------------------------------------------------------------------- store


def _annot_objects(doc: pymupdf.Document) -> dict[int, str]:
    """Raw source of every annotation object (all subtypes, incl. popups/links/widgets)."""
    out: dict[int, str] = {}
    for page in doc:
        for xref, _t, _i in page.annot_xrefs():
            try:
                out[xref] = doc.xref_object(xref, compressed=True)
            except Exception:
                out[xref] = "<unreadable>"
    return out


class VerificationError(ReviewError):
    pass


def require(cond: bool, what: str) -> None:
    if not cond:
        raise VerificationError(f"Post-write verification failed: {what}; aborted, file unchanged")


@dataclass
class Snapshot:
    path: Path
    data: bytes
    revision: str


class PdfStore:
    """Reads snapshots and performs locked, atomic, revision-checked incremental writes."""

    def lock_path(self, path: Path) -> Path:
        return path.with_name(f".{path.name}.review.lock")

    def read(self, path: Path) -> Snapshot:
        size = path.stat().st_size
        if size > MAX_PDF_BYTES:
            raise ReviewError(f"PDF too large ({size} bytes > {MAX_PDF_BYTES})")
        data = path.read_bytes()
        return Snapshot(path=path, data=data, revision=revision_of(data))

    @contextmanager
    def open_snapshot(self, path: Path) -> Iterator[tuple[Snapshot, pymupdf.Document]]:
        snap = self.read(path)
        try:
            doc = pymupdf.open(stream=snap.data, filetype="pdf")
        except Exception as exc:
            raise ReviewError(f"Cannot open PDF: {exc}") from None
        try:
            if doc.needs_pass:
                raise ReviewError("PDF is password protected; reading is not supported.")
            yield snap, doc
        finally:
            doc.close()

    def write(
        self,
        path: Path,
        expected_revision: str,
        mutate: Callable[[pymupdf.Document, ReviewModel], dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or not expected_revision.startswith("sha256:"):
            raise ReviewError("expected_revision must be the 'sha256:...' revision from a prior read")
        try:
            lock = posixlock.acquire(self.lock_path(path), LOCK_TIMEOUT_S)
        except posixlock.LockTimeout:
            raise LockBusyError(f"PDF is locked by another writer (waited {LOCK_TIMEOUT_S}s)") from None
        except posixlock.LockUnsupported as exc:
            raise WriteBlockedError(f"Writes are not supported here: {exc}") from None
        except posixlock.LockUnsafe as exc:
            raise WriteBlockedError(f"Refusing to write: {exc}") from None
        tmp_name = None
        try:
            snap = self.read(path)
            if snap.revision != expected_revision:
                raise StaleRevisionError(
                    f"Stale revision: expected {expected_revision}, current {snap.revision}. "
                    "Re-read the PDF and retry."
                )
            check = pymupdf.open(stream=snap.data, filetype="pdf")
            try:
                sec = security_info(check)
                if not sec["writable"]:
                    raise WriteBlockedError(
                        "Refusing to modify PDF: " + ", ".join(sec["write_blockers"])
                    )
                if not check.can_save_incrementally():
                    raise WriteBlockedError("PDF cannot be saved incrementally; refusing to rewrite it.")
            finally:
                check.close()

            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(snap.data)
            shutil.copymode(path, tmp_name)

            doc = pymupdf.open(tmp_name)
            try:
                before_objs = _annot_objects(doc)
                model = parse_document(doc)
                result = mutate(doc, model)
                no_change = bool(result.pop("_no_change", False))
                if not no_change:
                    # Incremental update: original bytes are kept verbatim as a prefix.
                    doc.save(tmp_name, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            finally:
                doc.close()
            if no_change:
                # Nothing to write (e.g. idempotent appearance sync): file untouched.
                result.pop("_verify", None)
                result.pop("_modified_xrefs", None)
                result["changed"] = False
                result["previous_revision"] = snap.revision
                result["revision"] = snap.revision
                return result

            with open(tmp_name, "rb") as fh:
                new_data = fh.read()
                os.fsync(fh.fileno())
            if not new_data.startswith(snap.data):
                raise ReviewError("Internal error: incremental save did not preserve original bytes")
            verify_fn = result.pop("_verify", None)
            allowed = set(result.pop("_modified_xrefs", ()))
            vdoc = pymupdf.open(stream=new_data, filetype="pdf")
            try:
                after_objs = _annot_objects(vdoc)
                for x, obj in before_objs.items():
                    if x not in allowed and after_objs.get(x) != obj:
                        raise ReviewError(
                            f"Post-write verification failed: unrelated annotation object {x} changed; aborted"
                        )
                vmodel = parse_document(vdoc)
                if verify_fn is not None:
                    verify_fn(vdoc, vmodel)
            finally:
                vdoc.close()

            # Final live check right before the swap: a non-cooperating program (e.g. a viewer
            # that ignores our lock) may have saved since the first check. Abort, keep its bytes.
            # A residual window between this re-read and os.replace() remains (see README).
            live = self.read(path)
            if live.revision != snap.revision:
                raise StaleRevisionError(
                    f"PDF changed on disk during the write (now {live.revision}); aborted without "
                    "overwriting. Re-read the PDF and retry."
                )
            os.replace(tmp_name, path)
            tmp_name = None
            try:
                dfd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
            result["changed"] = True
            result["previous_revision"] = snap.revision
            result["revision"] = revision_of(new_data)
            return result
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)
            lock.release()


# --------------------------------------------------------------------------- mutations


def new_id() -> str:
    return ID_PREFIX + uuid.uuid4().hex


def _stamp(doc: pymupdf.Document, annot: pymupdf.Annot, *, nm: str, actor: str, text: str, subject: str) -> None:
    annot.set_info(title=actor, content=text, subject=subject)
    annot.update()
    x = annot.xref
    doc.xref_set_key(x, "NM", pymupdf.get_pdf_str(nm))
    doc.xref_set_key(x, KEY_TOOL, pymupdf.get_pdf_str(TOOL_MARK))
    date = now_pdf_date()
    doc.xref_set_key(x, "M", pymupdf.get_pdf_str(date))
    if _get_key(doc, x, "CreationDate")[0] == "null":
        doc.xref_set_key(x, "CreationDate", pymupdf.get_pdf_str(date))


def add_root(
    doc: pymupdf.Document,
    page_no: int,
    *,
    actor: str,
    text: str,
    x: float | None,
    y: float | None,
    highlight_text: str | None,
    occurrence: int,
) -> tuple[str, list[float]]:
    if not 1 <= page_no <= doc.page_count:
        raise ReviewError(f"page must be in 1..{doc.page_count}")
    page = doc[page_no - 1]
    if highlight_text:
        hits = page.search_for(highlight_text, quads=True)
        if not hits:
            raise ReviewError(f"highlight_text not found on page {page_no}")
        if not 1 <= occurrence <= len(hits):
            raise ReviewError(f"occurrence must be in 1..{len(hits)}")
        annot = page.add_highlight_annot(hits[occurrence - 1])
    else:
        r = page.rect
        px = 36.0 if x is None else float(x)
        py = 36.0 if y is None else float(y)
        if not (r.x0 <= px <= r.x1 and r.y0 <= py <= r.y1):
            raise ReviewError(f"x/y must lie within the page rect {list(r)}")
        annot = page.add_text_annot(pymupdf.Point(px, py), text, icon="Comment")
    cid = new_id()
    _stamp(doc, annot, nm=cid, actor=actor, text=text, subject="Comment")
    popup = _ref(doc, annot.xref, "Popup")
    if popup:
        doc.xref_set_key(popup, "Open", "false")
    return cid, [round(v, 2) for v in annot.rect]


NOT_EDITABLE_TYPES = {"FreeText"}  # text is rendered in the page appearance stream


def edit_contents(
    doc: pymupdf.Document, target: Annot, *, actor: str, text: str, expected_content: str,
    kind: str = "edit",
) -> dict[str, Any]:
    """Explicit, intentional replacement of an ordinary comment's /Contents.

    Authorization is NOT derived from anything inside the PDF (any marker is forgeable).
    The caller must prove it saw the exact current text (expected_content) in addition to the
    file revision. The previous text/author/time are appended to the in-PDF edit history.
    /T (author) is left unchanged; the editor is recorded in the history entry.
    """
    if target.is_state:
        raise ReviewError("Status markers cannot be edited; use mark_comment_done/reopen_comment.")
    if target.is_group_member:
        raise ReviewError("Group member annotations cannot be edited; edit the group's primary comment.")
    if target.subtype in NOT_EDITABLE_TYPES:
        raise ReviewError(f"{target.subtype} text is drawn in the page appearance; editing is not "
                          "supported (neither edits nor inline answers). Nothing was changed.")
    if not isinstance(expected_content, str) or expected_content != target.contents:
        raise ReviewError(
            "expected_content does not exactly match the comment's current text; re-read it with "
            "get_comment and pass its exact 'text' to confirm an intentional edit."
        )
    if text == target.contents:
        raise ReviewError("New text is identical to the current text; nothing to edit.")
    history = list(target.history)
    if len(history) >= MAX_HISTORY_ENTRIES:
        raise ReviewError(f"Edit history is full ({MAX_HISTORY_ENTRIES} entries); this comment cannot "
                          "be edited or answered inline any further. Nothing was changed.")
    entry: dict[str, Any] = {
        "at": iso(now_pdf_date()),
        "kind": kind,
        "edited_by": actor,
        "previous_text": target.contents,
        "previous_author": target.author,
        "previous_modified": iso(target.modified),
    }
    x = target.xref
    rc = _get_key(doc, x, "RC")
    if rc[0] != "null":
        # Viewers prefer /RC over /Contents; keep the old rich text in history and drop it.
        entry["previous_rich_text"] = rc[1]
        doc.xref_set_key(x, "RC", "null")
    history.append(entry)
    # Set /Contents directly (no appearance regeneration, other keys untouched).
    doc.xref_set_key(x, "Contents", pymupdf.get_pdf_str(text))
    doc.xref_set_key(x, "M", pymupdf.get_pdf_str(now_pdf_date()))
    doc.xref_set_key(x, KEY_HISTORY, pymupdf.get_pdf_str(json.dumps(history, ensure_ascii=False)))
    return entry


# --------------------------------------------------------------------------- inline responses

INLINE_SEPARATOR = "\n\n"
MAX_LABEL = 64
MAX_INLINE_TOTAL = 100_000
_LABEL_LINE = re.compile(r"^\s*\[[^\]\n]*\]")


def _is_control(ch: str) -> bool:
    import unicodedata

    return unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")


def validate_label(actor: str) -> str:
    """Validate an inline response label. Labels are display text, NOT authenticated identity."""
    if not isinstance(actor, str):
        raise ReviewError("actor must be a string")
    label = actor.strip()
    if not label:
        raise ReviewError("actor label must not be empty")
    if len(label) > MAX_LABEL:
        raise ReviewError(f"actor label is too long ({len(label)} > {MAX_LABEL} chars)")
    if any(_is_control(c) for c in label):
        raise ReviewError("actor label must be a single line without control/format characters")
    if "[" in label or "]" in label:
        raise ReviewError("actor label must not contain '[' or ']'")
    return label


def validate_response(text: str) -> str:
    """Response body: non-blank, no control chars except newline/tab, and no continuation line
    that starts with a '[label]' (which could impersonate another inline response)."""
    if not isinstance(text, str):
        raise ReviewError("text must be a string")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ReviewError("response text must not be empty or whitespace-only")
    if len(text) > MAX_TEXT:
        raise ReviewError(f"response text is too long ({len(text)} > {MAX_TEXT} chars)")
    if any(_is_control(c) and c not in "\n\t" for c in text):
        raise ReviewError("response text contains control/format characters")
    for line in text.split("\n")[1:]:
        if _LABEL_LINE.match(line):
            raise ReviewError("a response line may not start with '[...]' (would look like another "
                              "labelled response)")
    return text


def inline_text(original: str, label: str, response: str) -> str:
    """original + separator + "[label] " + response. The separator is omitted only when the
    original text is empty (e.g. a bare highlight)."""
    sep = INLINE_SEPARATOR if original else ""
    return f"{original}{sep}[{label}] {response}"
