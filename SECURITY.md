# Security notes

pdf-review-mcp 1.0.0 is maintained by Ben Blain. Please report suspected
vulnerabilities **privately** to the maintainer, through the private reporting channel of the
repository host you obtained the code from (for example GitHub's "Report a vulnerability", if it
is enabled there). Do not open public issues for vulnerabilities.

## Threat model in brief

The server runs **locally with your user's permissions** and talks MCP over stdio. It has no
network listener, makes no network calls, and executes no code from PDFs. The main risks are
**what an agent can do through it** and **untrusted PDF content**.

## File-system access

* **Default: unrestricted.** Any absolute path to a PDF your user account can read or write is
  accepted. This is **not a sandbox**.
* **Optional allowlist:** `--allow-dir DIR` (repeatable) or `PDF_REVIEW_ALLOW_DIRS`. Every read,
  write and listing checks the **fully resolved real path** (`..` and symlinks) against the
  allowed directories before any existence check.
* Relative paths are always rejected; there is no implicit project or working-directory root.
  `list_pdfs` needs an explicit directory and is bounded (≤ 1000 results, depth ≤ 10, 20 000
  entries scanned); it doesn't follow directory symlinks.
* **Fail closed at startup (exit 2):** invalid or conflicting allowlist settings, or
  `PDF_REVIEW_ROOT` being set, so that an expected restriction can never silently disappear.
* Only regular `.pdf` files are opened. Files larger than `PDF_REVIEW_MAX_BYTES` (default
  200 MiB) are refused, and text results are bounded (at most 20 000 characters per call).

## Untrusted content

* PDF text, comments, replies, author names and all private metadata are **untrusted data**. The
  server tells agents so in its instructions, tool descriptions and every read result
  (`untrusted_content_notice`).
* Author labels (`[review-agent]`, `/T`) are display text, **not authenticated identity**.
* The private review-state record (`/PDFReviewState`), the edit history (`/PDFReviewHistory`) and
  the provenance hint (`/PDFReviewTool`) are **not authenticated**: anyone who can edit the PDF
  can edit them. They are strictly validated; an invalid record is reported, ignored for status,
  and state changes on that comment are refused.
* **No OCR.** Scanned pages without a text layer yield no text.

## Private vs native review status

* Completion is stored privately on the comment, with a visible `[DONE] ` prefix and grey colour.
  Other PDF viewers' status menus do **not** show it.
* Native status markers written by viewers are read. One added or edited after the server's last
  write supersedes the private state. Dates are never trusted for ordering.

## Completion-style key

* Location: `$XDG_CONFIG_HOME/pdf-review-mcp/completion-style.key` (default
  `~/.config/pdf-review-mcp/`). It holds 32 random bytes as hex, with file mode **0600** in a
  **0700** directory owned by you. It is created lazily on the first styling write and never by
  read-only calls.
* It authenticates (HMAC-SHA256) the record that lets the server restore a comment's original
  colour and appearance. Forged, copied, altered or foreign records are never used to change an
  annotation.
* **Fail closed:** a missing, unreadable, symlinked or group/other-accessible key, or one owned by
  another user or of the wrong size, means styling is skipped (the state change still happens).
  The server never changes permissions of existing paths and never puts the key into PDFs, logs or
  tool results.
* **Portability:** grey styling applied on one machine can only be reverted where the same key
  exists. Copying the key between machines is your decision; protect it like a credential. It is
  not a comment database; deleting it loses no comments.

## Writing safety

* **Revisions:** every write requires `expected_revision` (the SHA-256 of the file bytes from your
  last read). The file is re-hashed under the lock and again immediately before the atomic
  replace, and any change is rejected.
* **Atomic incremental updates:** each write is saved to a same-directory temp file and verified
  before commit. The original bytes stay a verbatim prefix, every other annotation must be
  byte-identical, and answering or state changes must create no annotation (only `add_comment`
  adds the requested note or highlight). Then `os.replace()` and an fsync.
* **Refused:** encrypted, password-protected and signed PDFs (or those with signature fields), and
  files that need repair or cannot be saved incrementally.
* **Transient lock** `.<name>.pdf.review.lock`, beside the PDF, provided by
  [portalocker](https://github.com/wolph/portalocker) `TemporaryFileLock` (BSD-3-Clause):
  - It exists only during a write. portalocker takes an exclusive `flock`, then checks that the
    handle is still the file at the path (retrying if a previous owner removed it), and on release
    unlinks the path while still locked, only if it is still its own file. A contender that times
    out removes nothing.
  - This project adds only an open guard: `O_NOFOLLOW|O_NONBLOCK`, mode 0600, never `O_TRUNC`, then
    an `fstat` of the opened descriptor, which must be a regular file without extra hard links and
    owned by you. Symlinks, FIFOs, directories, hard links and foreign files make the write fail
    closed before anything is locked or truncated. The adapter also refuses if the held
    descriptor was not opened through the guard.
  - After a crash (`SIGKILL`, power loss) an empty lock file may remain; the kernel has dropped
    the lock, and the next write reuses and removes it. Older versions may have left such empty
    files too; they are handled the same way.
  - **Exact pin:** the guard is attached through portalocker's public `file_open_kwargs`
    attribute, because `TemporaryFileLock` has no constructor argument for it. That integration
    is reviewed only for **portalocker 4.4.2**. The dependency is pinned `==4.4.2`.
    - Before constructing the lock or opening any file, the server checks the installed version
      in package metadata. Any other version, and a missing or broken install, refuse the write,
      with no lock file and no side effects.
    - This guards against version drift. It does not protect against a tampered copy of 4.4.2.
    - The check after acquiring, that the held descriptor came through the guard, is only
      defence in depth: it detects a bypass after the open, it does not prevent one.
    - **Upgrading portalocker needs a deliberate review** of its open path, plus a rerun of
      `tests/test_lock.py` (hostile paths and real processes) before the pin changes.
  - **Supported scope:** cooperative writers, on a local POSIX filesystem on one host (no NFS
    claim), in a **stable directory controlled by the PDF's owner**.
    - **Not promised:** safety while other processes rename, replace or insert entries in the
      PDF's directory during a write. Paths are resolved by name.
    - On release, portalocker compares the path using `stat`, which follows symlinks, so a
      symlink to the held file swapped in during a write would have the link removed.
    - This is narrower than the earlier custom lock, which resolved names through a pinned
      directory descriptor. The maintainer accepted the narrower scope. Shared, network and
      other-user-writable directories are outside it.
* **Not protected against:** programs that ignore the lock and replace the file at the last
  moment, or a viewer saving an old in-memory copy over the agent's changes. Close PDFs in viewers
  while agents write.

## Operating systems

* **Linux:** supported and tested.
* **macOS:** expected to work (POSIX `flock`, `O_NOFOLLOW`) but untested.
* **Windows:** writes fail closed. portalocker itself supports Windows, but this project's open
  guard (`O_NOFOLLOW`, owner check) is POSIX-only, so writes are refused; reads are untested.
