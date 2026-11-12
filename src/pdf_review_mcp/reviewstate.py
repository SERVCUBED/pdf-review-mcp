"""Private, root-only review state.

Completion is recorded ON THE ROOT annotation only, in the private key /PDFReviewState
(a JSON string). No status, reply or popup annotation is ever created. /State is NOT written to
the root (non-standard; spec-following parsers would treat the root as an orphan status
marker).

Record (strictly validated; any deviation => invalid, ignored for reading, writes refused)::

    {"schema": "pdf-review-mcp/review-state", "v": 1,
     "events": [{"seq": 1, "state": "Completed", "actor": "...", "note": "...", "at": "...",
                 "source": "server" | "adopted_native",
                 "native_id": "...optional..."}],
     "baseline": [{"id": "...", "xref": 290, "digest": "<sha256>"}],
     "prefix": {"owned": true|false, "form": "space"|"empty"|null, "reason": "...|null"}}

Precedence (dates are never used):
* ``baseline`` is a snapshot of every native Review-model marker targeting the root, taken at
  the last managed write. Those markers are "accounted for".
* A native marker that is NOT in the baseline, or whose digest (state, model, note, author,
  /M, /NM) changed, is a NEW native event (e.g. a human using a viewer's status menu) and
  supersedes the private state. If several, the last in document append order wins.
* Otherwise the last private event is the effective state.

Visible cue: when done, the root text gets the server-owned prefix "[DONE] " (or exactly
"[DONE]" for an empty comment). Reopen removes the prefix only if the record says the server
owns it AND the text still starts with it; a reviewer's literal "[DONE]" is never stripped,
never treated as done, and is not duplicated (prefix not owned).

The record is convenience metadata, NOT trusted identity or authorization: anyone who can edit
the PDF can edit it (as they can edit text or add native markers).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

KEY_STATE = "PDFReviewState"
SCHEMA = "pdf-review-mcp/review-state"
VERSION = 1
PREFIX = "[DONE] "
PREFIX_EMPTY = "[DONE]"
SOURCES = {"server", "adopted_native"}
STATES = {"None", "Accepted", "Rejected", "Cancelled", "Completed"}
MAX_EVENTS = 200
MAX_BASELINE = 200
MAX_NOTE = 20_000
MAX_ACTOR = 200
MAX_RECORD_BYTES = 2_000_000


class RecordError(ValueError):
    pass


def native_digest(m) -> str:
    """Identity+content digest of a native marker (an Annot)."""
    blob = json.dumps([m.nm or "", m.state, m.state_model, m.contents, m.author, m.modified or "",
                       m.created or ""], ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def parse(raw: str | None) -> dict[str, Any] | None:
    """None if absent. Raises RecordError if present but invalid."""
    if raw is None:
        return None
    if len(raw) > MAX_RECORD_BYTES:
        raise RecordError("record too large")
    try:
        r = json.loads(raw)
    except ValueError:
        raise RecordError("not valid JSON") from None
    if not isinstance(r, dict) or r.get("schema") != SCHEMA or r.get("v") != VERSION:
        raise RecordError("unknown schema/version")
    if set(r) - {"schema", "v", "events", "baseline", "prefix"}:
        raise RecordError("unknown top-level fields")
    ev, bl, px = r.get("events"), r.get("baseline"), r.get("prefix")
    if not isinstance(ev, list) or not ev or len(ev) > MAX_EVENTS:
        raise RecordError("events must be a non-empty list (bounded)")
    for i, e in enumerate(ev):
        if not isinstance(e, dict) or set(e) - {"seq", "state", "actor", "note", "at", "source", "native_id"}:
            raise RecordError(f"event {i} has unknown fields")
        if e.get("seq") != i + 1 or e.get("state") not in STATES or e.get("source") not in SOURCES:
            raise RecordError(f"event {i} has an invalid seq/state/source")
        for k, lim in (("actor", MAX_ACTOR), ("note", MAX_NOTE), ("at", 64)):
            if not isinstance(e.get(k), str) or len(e[k]) > lim:
                raise RecordError(f"event {i} field {k} invalid")
        if "native_id" in e and (not isinstance(e["native_id"], str) or len(e["native_id"]) > 300):
            raise RecordError(f"event {i} native_id invalid")
    if not isinstance(bl, list) or len(bl) > MAX_BASELINE:
        raise RecordError("baseline must be a bounded list")
    for b in bl:
        if (not isinstance(b, dict) or set(b) != {"id", "xref", "digest"} or not isinstance(b["id"], str)
                or not isinstance(b["xref"], int) or not isinstance(b["digest"], str) or len(b["digest"]) != 64):
            raise RecordError("baseline entry invalid")
    if (not isinstance(px, dict) or set(px) != {"owned", "form", "reason"} or not isinstance(px["owned"], bool)
            or px["form"] not in ("space", "empty", None) or (px["owned"] and px["form"] is None)
            or not (px["reason"] is None or (isinstance(px["reason"], str) and len(px["reason"]) <= 200))):
        raise RecordError("prefix invalid")
    return r


def dump(r: dict[str, Any]) -> str:
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


def baseline_of(natives) -> list[dict[str, Any]]:
    return [{"id": m.id, "xref": m.xref, "digest": native_digest(m)} for m in natives][:MAX_BASELINE]


# ------------------------------------------------------------------ prefix


def has_prefix(text: str) -> bool:
    return text == PREFIX_EMPTY or text.startswith(PREFIX) or text.startswith(PREFIX_EMPTY + "\n")


def add_prefix(text: str) -> tuple[str, dict[str, Any]]:
    """Return (new text, prefix record). Never duplicates a literal prefix."""
    if has_prefix(text):
        return text, {"owned": False, "form": None, "reason": "text already starts with a literal [DONE]"}
    if text == "":
        return PREFIX_EMPTY, {"owned": True, "form": "empty", "reason": None}
    return PREFIX + text, {"owned": True, "form": "space", "reason": None}


def strip_prefix(text: str, prefix: dict[str, Any]) -> tuple[str, str | None]:
    """Return (new text, reason-if-not-stripped). Only an OWNED prefix still present is removed."""
    if not prefix["owned"]:
        return text, "prefix not owned by the server"
    if prefix["form"] == "empty":
        if text == PREFIX_EMPTY:
            return "", None
        if text.startswith(PREFIX_EMPTY + "\n\n"):
            return text[len(PREFIX_EMPTY) + 2:], None  # inline answer appended while done
        if text.startswith(PREFIX):
            return text[len(PREFIX):], None
        return text, "server prefix no longer present (text edited); left unchanged"
    if text.startswith(PREFIX):
        return text[len(PREFIX):], None
    return text, "server prefix no longer present (text edited); left unchanged"


def prefix_present(text: str, prefix: dict[str, Any]) -> bool:
    if not prefix["owned"]:
        return False
    if prefix["form"] == "empty":
        return text == PREFIX_EMPTY or text.startswith(PREFIX_EMPTY + "\n\n") or text.startswith(PREFIX)
    return text.startswith(PREFIX)
