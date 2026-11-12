# Agent guide

This guide supplements [AGENTS.md](../AGENTS.md), which is binding. It explains the server's
behaviour so you can predict results and report them accurately.

## Data model in one page

* **Root comment:** any markup annotation without `/IRT` (sticky note, highlight, square, …).
  Popups, replies (`/IRT` + `/RT /R`), group members and native status markers are not root
  comments.
* **Ids:** `comment_id` is a unique `/NM` if present, otherwise `xref:<n>`. A reply id given to
  `get_comment` resolves to its root.
* **Effective state:**
  * a root's private `/PDFReviewState` record decides, **unless** a native Review status marker
    exists that was added or edited after the server's last write on that comment, for example
    by a human using a viewer's status menu; then the latest such native marker wins;
  * with no record, the last native marker in document order decides;
  * dates are never used;
  * `Completed`, `Cancelled` and `Rejected` count as done; everything else is open.
* **Reading the result:**
  * `effective_source` is `private`, `native` or `none`;
  * `state_history` entries carry `source` (`native`, `server` or `adopted_native`);
  * `managed_prefix.owned` says whether the server owns the `[DONE] ` prefix.

## What each write does to the text

| Before | Call | After |
|---|---|---|
| `Fix typo` | `append_agent_response(text="Done in §2", actor="review-agent")` | `Fix typo\n\n[review-agent] Done in §2` |
| `Fix typo\n\n[review-agent] Done in §2` | `mark_comment_done(...)` | `[DONE] Fix typo\n\n[review-agent] Done in §2`, grey |
| (done) | `append_agent_response(text="URL added")` | `[DONE] …\n\n[review-agent] URL added` |
| (done) | `reopen_comment(...)` | prefix removed, both answers kept, original colour |
| `` (empty highlight) | `mark_comment_done` | `[DONE]` |
| `[DONE] see note` written by a human | `mark_comment_done` | unchanged text (prefix not owned); reopen also leaves it |

Notes:
* `edit_comment` on a done comment must keep the `[DONE] ` prefix; otherwise it is rejected.
* Comments with rich text (`/RC`) get state and grey but no prefix; check `text_note`.
* `appearance.reason` values such as `unsupported_type`, `style_key_unavailable` or
  `style_key_mismatch` mean the colour was not changed, while the state change still happened.
  Report them.

## Error handling

| Error | Meaning | What to do |
|---|---|---|
| `Stale revision` | the file changed since your read | re-read, re-evaluate, at most 2 retries (AGENTS.md section 4.5) |
| `expected_content does not exactly match` | the comment text changed | re-read; never guess the text |
| `Comment is already done/open` | the state differs from what you assumed | re-read and report; don't toggle blindly |
| `Relative path … rejected` / `escapes the allowed directories` | path policy | ask the human for the correct absolute path |
| `WriteBlockedError` (encrypted, signed, lock unsafe, unsupported platform) | writing is not safe | stop and report |
| `PDF is locked by another writer` | another writer holds the lock | wait briefly and re-read once; if it persists, stop |
| `invalid … review-state record` | the private metadata is corrupt or from an unknown version | stop; state changes on that comment are refused by design |

## Verifying a synthetic lifecycle (optional, human-approved)

1. In a scratch directory, ask the human for, or create with `add_comment` on a throwaway copy,
   a PDF with one comment.
2. `review_summary`, then `append_agent_response`, then `mark_comment_done`. Check that the text
   starts with `[DONE] ` and that `annotations_created` is 0.
3. `reopen_comment`: the prefix is gone and the answer is still there.
4. `sync_comment_appearance` must report `changed: false` (already in sync).

The repository's `scripts/smoke_test.py` performs the same lifecycle automatically against a
temporary PDF and a temporary key directory.
