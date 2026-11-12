# Reference

This is the detailed reference for pdf-review-mcp 1.0.0. For a short introduction, see the
[README](../README.md). Installation is in [INSTALL.md](INSTALL.md), the operating rules for
agents are in [AGENT_GUIDE.md](AGENT_GUIDE.md) and [../AGENTS.md](../AGENTS.md), and the threat
model is in [../SECURITY.md](../SECURITY.md).

## Command line

The server speaks MCP over **stdio**. Normally your MCP client starts it.

```bash
pdf-review-mcp                                   # default: no arguments needed
pdf-review-mcp --allow-dir /home/alice/reviews   # optional: only allow PDFs under this directory (repeatable)
pdf-review-mcp --actor review-agent              # optional: label used for inline answers and history
pdf-review-mcp --version
```

* Every tool takes an **absolute** PDF path, for example `/home/alice/reviews/report.pdf`.
  Relative paths are rejected. There is no working directory or project root.
* With no options, the server can open **any PDF your user account can open**. It is **not a
  sandbox**. Use `--allow-dir` (or `PDF_REVIEW_ALLOW_DIRS='["/abs/dir"]'`) to restrict it.
  The directory must exist and be absolute, or the server refuses to start.
* If `PDF_REVIEW_ROOT` is set, the server refuses to start. It is not a supported setting.
* The environment variables are listed in [INSTALL.md](INSTALL.md#configure-your-mcp-client).

## Tools (13)

Read-only:

| Tool | Purpose |
|---|---|
| `list_pdfs(directory, recursive=True, limit=200, max_depth=3)` | PDFs in an explicit absolute directory |
| `review_summary(path)` | counts of open and done comments, replies, native status markers; diagnostics; write blockers; `revision` |
| `list_comments(path, status="all", author=None, page=None, include_replies=True, include_empty=True, limit=50, offset=0)` | comment threads with replies and state history |
| `list_open_comments(path, author=None, page=None, limit=50, offset=0)` | comments that are not done |
| `get_comment(path, comment_id, context_chars=800)` | one full thread plus nearby page text |
| `get_comment_context(path, comment_id, margin=72.0, max_chars=2000)` | the text under a highlight or note and around it |
| `read_page_text(path, page, offset=0, max_chars=4000)` | page text, paginated |

Writes:

| Tool | Effect |
|---|---|
| `add_comment(path, page, text, expected_revision, actor=None, x=None, y=None, highlight_text=None, occurrence=1)` | a new sticky note, or a highlight of matching page text |
| `append_agent_response(path, comment_id, text, expected_content, expected_revision, actor=None)` | **answer inline**: appends `\n\n[actor] text` to the comment (`expected_content` = its exact current text) |
| `mark_comment_done(path, comment_id, resolution_note, expected_revision, actor=None)` | `[DONE] ` prefix and grey; the note is stored privately; no new annotation |
| `reopen_comment(path, comment_id, note, expected_revision, actor=None)` | removes only the server's `[DONE] ` prefix and restores the colour |
| `sync_comment_appearance(path, comment_id, expected_revision, actor=None)` | aligns the prefix and grey with the effective state, for example after someone set a status in another viewer; idempotent |
| `edit_comment(path, comment_id, text, expected_content, expected_revision, actor=None)` | replaces a comment's text; only on explicit user request |

* All writes except `add_comment` are marked `destructiveHint: true`, because they change the
  displayed text of an existing comment.
* There is no delete tool.
* Answering, completing, reopening and syncing never create or remove annotations: no replies,
  status markers or popups. `add_comment` adds only the one note or highlight you ask for; a
  note gets its standard popup.

## Write contract

* **Revision check:** every read returns `revision`, which is `sha256:` of the exact file bytes.
  Every write requires it as `expected_revision`. Under the write lock the file is re-read and
  re-hashed, and the write fails with a stale-revision error if anything changed.
* **Final re-check:** the live file is hashed once more just before the atomic replace. A
  change there also aborts, and the other program's bytes are kept.
* **On a stale revision:** re-read, re-evaluate whether the change is still right, and retry
  with the new values at most twice (see [AGENT_GUIDE.md](AGENT_GUIDE.md)).
* **Incremental and verified:** each write saves to a temporary file in the same directory.
  It checks that the original bytes stay a verbatim prefix and that unrelated annotations are
  byte-identical, then commits with `os.replace()` and an fsync.
* **Write lock:** a transient lock file `.<name>.pdf.review.lock` sits beside the PDF while a
  write runs. It is provided by portalocker, pinned exactly to 4.4.2, and removed afterwards;
  a leftover from a crash is reused and removed by the next write. See
  [SECURITY.md](../SECURITY.md) for the guard, the supported scope and the upgrade policy.
* **Refused:** encrypted, password-protected and signed PDFs, and files that need repair or
  cannot be saved incrementally.

## How the review state works

* The state is a validated private record (`/PDFReviewState`) on the original comment.
* The visible cue is the `[DONE] ` prefix (an empty comment becomes `[DONE]`) and a grey colour on
  sticky notes and highlights.
* A reviewer typing `[DONE]` themselves does **not** mark the comment done, and the server never
  strips text it did not add.
* Other viewers' "Completed" menus **do not show** this private state; the prefix and the grey are
  the cue.
* If someone sets a status in such a viewer, that newer native status takes precedence, and
  `sync_comment_appearance` updates the visible cue.
* Comments, replies and status markers written by other viewers are read and left untouched.
* The grey styling is reversible. It is authenticated with a local key
  (`~/.config/pdf-review-mcp/completion-style.key`, created on the first styling write with mode
  0600). See [SECURITY.md](../SECURITY.md).

## Update and uninstall

See [INSTALL.md](INSTALL.md#update-and-uninstall). After any update, **restart or reconnect your
MCP client**: clients discover tools when they start the server, so a running session keeps the
old server.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q                       # full suite (generated fixtures only)
.venv/bin/python scripts/smoke_test.py              # real stdio round trip with a temp PDF
.venv/bin/python scripts/smoke_test.py --command "$(command -v pdf-review-mcp)"   # installed binary
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) and [CHANGELOG.md](../CHANGELOG.md).

## Release steps (maintainer)

1. These must pass: `python -m pytest -q`, `python scripts/smoke_test.py` and `reuse lint`. CI
   must be green on all matrix versions.
2. Build with `uv build --out-dir dist`. Check that the wheel's `METADATA` shows
   `License-Expression: AGPL-3.0-or-later AND CC-BY-SA-4.0`, and that its `licenses/` contain the
   license files.
3. Tag the commit (`v<version>`), and attach the wheel, sdist and `SHA256SUMS` to the release.
4. Decide on PyPI publication separately; it is not part of these steps.
5. Optionally, pin the GitHub Actions to commit SHAs (`.github/workflows/ci.yml`).
