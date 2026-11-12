# Changelog

## Unreleased

* **Lock:**
  - The custom `posixlock` module is replaced by
    [portalocker](https://github.com/wolph/portalocker) `TemporaryFileLock`, a new dependency
    pinned exactly `portalocker==4.4.2` (BSD-3-Clause), plus a small fail-closed open guard.
  - Any other installed portalocker version refuses writes before the lock path is touched.
  - The lock file still exists only during a write, and hostile lock paths still refuse the
    write. Windows writes remain unsupported.
  - **Narrower guarantee than before (accepted by the maintainer):** it covers a stable,
    owner-controlled directory only. Safety against renames or replacements of directory entries
    during a write is no longer promised, because the pinned-directory semantics are gone and
    release follows symlinks. See `SECURITY.md`.
  - **Upgrade policy:** the pin changes only after a review of portalocker's open path and a
    rerun of `tests/test_lock.py` on the new version.
  - **Validation (2026-11-12, Linux, CPython 3.12, portalocker 4.4.2):** 219 tests passed on
    tmpfs and on ext4; the stdio smoke test against a freshly installed wheel prints
    `SMOKE OK`. This is a separate measurement from the historical 1.0.0rc1 figure (206) below.

## 1.0.0rc1: release candidate

First release candidate.

* **Licensing:** software under AGPL-3.0-or-later and documentation under CC-BY-SA-4.0,
  copyright 2026 Ben Blain. Verbatim license texts are in `LICENSE` and `LICENSES/`; scope is set
  in `REUSE.toml` (REUSE 3.3 compliant) and explained in `LICENSING.md`. Dependency notices are in
  `THIRD_PARTY_NOTICES.md`. The package metadata declares
  `License-Expression: AGPL-3.0-or-later AND CC-BY-SA-4.0`.
* 13 MCP tools over stdio:
  - read: `list_pdfs`, `review_summary`, `list_comments`, `list_open_comments`, `get_comment`,
    `get_comment_context`, `read_page_text`;
  - write: `add_comment`, `append_agent_response`, `mark_comment_done`, `reopen_comment`,
    `sync_comment_appearance`, `edit_comment`.
* **Single original comment:**
  - answers are appended inline (`[actor] …`);
  - completion adds a server-owned `[DONE] ` prefix and authenticated grey styling, with the
    state recorded privately on the comment;
  - no reply, status or popup annotation is ever created.
* **Native interoperability:** comments, replies and Review markers written by viewers are read,
  with date-independent precedence against the private state. Unknown record versions fail closed.
* **Safety:**
  - absolute paths with an optional `--allow-dir` allowlist; `PDF_REVIEW_ROOT` is refused at
    startup;
  - revision checks with a final re-hash; atomic, verified incremental writes;
  - a transient inode-validated POSIX lock (replaced by portalocker; see Unreleased);
  - encrypted and signed PDFs refused;
  - untrusted-content warnings for agents.
* **Platforms:** Linux is supported; macOS is expected to work but untested; Windows is
  unsupported for writes.
* **Fixed during review:** some error messages still pointed to actions that don't exist.
  - An over-long inline answer said "use reply_to_comment instead". It now says how many
    characters fit, or, when no answer fits, that nothing was changed and the user should be
    told.
  - The FreeText and history-full refusals said "reply instead". They now state that nothing was
    changed.
  - A regression test asserts that no error text names a removed tool.
* **Validation (Linux, CPython 3.12), as measured for the 1.0.0rc1 release with the custom lock:** 206 tests passed; the stdio smoke test against the
  installed wheel prints `SMOKE OK`.
