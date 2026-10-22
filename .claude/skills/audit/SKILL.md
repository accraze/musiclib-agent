---
name: audit
description: Periodic library audit (M6, D39): check that beets, the files on disk and the audit log agree, that the dump and inbox archive are unchanged, find damaged files, incomplete albums and missing art, and propose MusicBrainz corrections (mbsync) in batches. Use when the user runs /audit or asks to check, audit or health-check the library.
---

# Audit

`musiclib audit` is read-only on the library, the dump and the inbox archive. The one
library-changing step is `audit apply` (mbsync), and it runs only on albums the user approved.
You drive the subcommands; you never touch files yourself. Safety rules apply (docs/SPEC.md).

## Loop

1. **Check**: `uv run musiclib audit check` (about 10 s; takes `state/library.lock`, so if
   it's held, wait and retry). It writes `state/reports/audit-<date>.json` and prints the
   headline numbers. Read the report for examples.
2. **Coverage**: if `decode_pending` or `mbsync_unchecked` is high, offer to run the long checks.
   They're resumable, so `--limit` runs them in pieces:
   - `uv run musiclib audit decode [--limit N]`: about 35 min for the whole library. Run it in the background.
   - `uv run musiclib audit mbsync [--limit N]`: about 1 s per album (MusicBrainz rate
     limit), about 70 min in all. Unchecked albums go first, then the least recently checked.
   Run `audit check` again afterwards so the report includes them.
3. **Report** to the user in one short table, problems first (see below). Say what's
   clean in one line ("dump and inbox unchanged; no missing or untracked files").
4. **MusicBrainz corrections**: `uv run musiclib audit list --limit 20`, then
   `uv run musiclib audit show <album_id>` for any album you want to look at closely. Present ONE table:
   `# | album | fields changing | moves to | proposal | why`
   Propose `apply` for real corrections (title spelling, track or artist credits, a fixed
   date), and `leave` where the change looks wrong for this copy. Put renames (moves) and
   album-title changes at the top. Wait for approval, then:
   `uv run musiclib audit apply --albums <ids...> --reason "approved by user <date>"`
   (dry run), then the same with `--apply --by agent` (or `--by user` when the user picked
   them). Each album is re-checked just before it's applied; one that no longer differs is
   skipped. Report what moved.

## Reading the check

| Finding | Meaning | What to propose |
| --- | --- | --- |
| `beets_missing` | beets lists a file that isn't on disk | Ask. Never guess; the user may have moved it |
| `logged_missing` + `unlogged`, `relinkable` > 0 | beets moved files without an audit row (D25 resync before D39) | `audit relink` (dry run), then `--apply --by agent` once the user agrees. It only adds `relocate` rows |
| `orphan_rows` | old audit rows whose source is an album key, not a file (2026-09-29 retag sweep). They place nothing | Nothing; mention the count once |
| `untracked` | a library file neither beets nor the log knows | Ask; it may have been copied in by hand |
| `modified_since_import` | changed on disk after beets wrote it (another tagger, or damage) | Show the files. Once the user has looked, `audit check --accept-modified` |
| `originals_missing` / `originals_changed` | the dump or the inbox archive changed (safety rule 1) | Stop and tell the user first: this should never happen |
| `damaged` | decodes to less than 90% of its MusicBrainz track length (D44; the stated length when there is none, e.g. Unsorted) | Compare with the dump original; the fix (`import --remove`, `--restore`, `--redo`) is the user's call. Losing originals are usually damaged the same way: a better copy has to come from outside, through the inbox (upgrade) |
| `header_length_wrong` | short of the length its header states, but as long as its MusicBrainz track (D44) | Nothing; the file plays in full. Count only |
| `incomplete_albums` | fewer tracks than the release (box sets, partial discs) | Mostly intentional. List only the striking ones (1 of 20) |
| `no_art` | album without cover art | List; fetching art is a follow-up |
| `unsorted` | as-is folders, no MusicBrainz IDs by design (D13) | Counts only |

## Not in scope

Unattended, `audit decode` (new files only) and `audit check` run nightly (D42, D43); the report
is waiting in `state/reports/` when the user runs /audit. `mbsync` and `apply` stay hand-run.
Originals of batches pruned under D43 are not expected, so they never show as missing.
Never run `beet` directly (D9).
