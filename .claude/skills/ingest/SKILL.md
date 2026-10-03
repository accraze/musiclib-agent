---
name: ingest
description: Ingest new music folders from the inbox into the library (M5): claim, scan, dedupe against the batch, the library and the dump, match, import, report. Use when the user runs /ingest, drops folders into the inbox, or asks to add new music to the library.
---

# Ingest

New folders land in `inbox_dir` (`musiclib.toml`, D27). Each goes through the same pipeline
as the dump, plus dedupe against the library (D30). You drive `musiclib ingest` subcommands;
you never touch files yourself.

Safety rules apply (docs/SPEC.md). In particular:
- A claimed folder (`inbox/.processed/<date>/<folder>`, D28) is read-only like the dump.
- Every mutating step is a dry run first. `claim`, `import` and `upgrades` need `--apply`.
- Nothing is imported that dedupe skipped; nothing in the library is replaced without an
  approved upgrade (D32).

## Loop (one folder at a time)

1. `uv run musiclib ingest list`: folders waiting, loose files ignored, earlier batches.
   Show the user the waiting folders (audio files, size) and ask which to ingest. Loose
   files at the inbox root must go in a folder first.
2. **Claim**: `uv run musiclib ingest claim "<folder>"` (dry run) shows where it will move.
   When the user says go, run it again with `--apply --by agent` (the user approved) and note
   the batch id. This is a rename inside the inbox; it is logged.
3. **Scan**: `uv run musiclib ingest scan --batch <id>`. Report audio files, scan errors and
   verify verdicts (`confirmed` = tags match the audio). Scan errors are not damage by
   themselves (D20 judges damage by decoding at import).
4. **Dedupe**: `uv run musiclib ingest dedupe --batch <id>`. Explain each item:
   - `skip:duplicate`: already in the library at equal or better quality. Not imported; the
     report names the library copy.
   - `skip:identical`, `skip:in_batch`: byte copies or a worse copy inside the batch.
   - `review:upgrade`: better audio than the library copy (e.g. FLAC over MP3).
   - `review:extra_tracks`: tracks the library copy lacks (bonus tracks, a fuller edition).
   - `review:edition`: tagged as another release of an album already in the library, or (D38)
     same artist and album name as a library folder but too few tracks match (another master,
     often a remaster AcoustID split). Compare the two copies before proposing.
   - `flag:dump_overlap`: the same album sits in the dump without being in the library
     (it's in the review queue, or it was a dropped copy). Only mention it; don't act on it.
5. **Match**: `uv run musiclib ingest match --batch <id>` (about 5 s per album). Albums
   land in the shared `matches` queue with the batch id (D31).
6. **Import**: `uv run musiclib ingest import --batch <id>` (dry run) lists strong matches
   (D15) and no-candidate albums (D13, Unsorted/). Show the plan; on the user's go, run it
   with `--apply`. This is the user's approval for those albums (safety rule 6 lets strong
   matches in without review).
7. **Review**: anything left (`waiting_for_review`) is handled with the `/review` skill,
   exactly like dump albums: standing approvals D21/D22/D24/D26 apply (D31), everything else
   is proposed in a batch and recorded only after the user approves. Albums held by a dedupe
   review appear under `--kind dupe` with an `ingest duplicate review: ...` note.
8. **Upgrades**: after an approved upgrade is imported,
   `uv run musiclib ingest upgrades --batch <id>` (dry run) lists the old library files that
   the new copy replaces. On the user's go, `--apply --by agent` removes them (logged, D32).
9. **Report**: `uv run musiclib ingest report --batch <id>` writes the manifest to
   `state/reports/` and summarizes where every file went. Finish with a short summary:
   imported (where), skipped as duplicates (of what), waiting for review, flags.

## Judgment

- Upgrades: approve when the new copy is clearly better audio of the same release (same
  track list, `confirmed` verdicts) and complete. If the new copy is missing tracks the
  library copy has, say so: replacing it would lose tracks.
- Extra tracks and editions: approving imports the new copy *alongside* the library copy
  (both kept, like two editions); `skip` leaves it out. Only `upgrade` ever replaces a library
  copy (D32). Propose skip when the only difference is a stray or a duplicate track, approve
  when it's a genuinely different or fuller release, and ask when unsure.
- If a batch mixes several albums, that's fine: beets groups them by folder.
- If another command holds the library lock ("another command is changing the library"),
  wait and retry; never work around it.
