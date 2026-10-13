---
name: dedupe
description: Resolve duplicate review groups (the `dupe` kind of the review queue): several dump folders holding the same album or editions of it. Use when the user runs /dedupe, asks to sort out duplicates, pick keepers, or clear the dupe queue. Proposes a keeper per group in batches and records decisions only after the user approves each batch.
---

# Duplicate groups

`musiclib dupes` (M2) grouped folders that hold the same recordings. Tiers 1 and 2 with a
clear keeper were settled automatically: the losers are simply not imported (D18). What is
left needs judgment, and every album of such a group waits in the `dupe` queue so a second
copy can't be approved by accident:

- **tier 2, partial**: copies of one release where the lower-quality copy has tracks the best
  copy lacks ("kept copies overlap").
- **tier 3, editions**: the same recordings tagged as different releases (remaster, deluxe,
  regional pressing). Keeping both may be intentional.

Safety rules apply (docs/SPEC.md): the dump is never touched, nothing is deleted, and no
decision is recorded without the user's approval of the batch (rule 7). Losing copies are
**skipped**, never removed: they stay in the dump and the not-imported report names them.

## Loop

1. `uv run musiclib review stats` for the open `dupe` count, then
   `uv run musiclib review dupes --limit 10` (`--offset` to page). Each group has `tier`,
   `reason`, the proposed `keeper` and every member folder with:
   - `role` (`keep`/`drop`, the M2 proposal) and `coverage` (share of its tracks in kept copies);
   - `stats`: files, tracks, lossless share, kbps, verified share, art, size, tagged release;
   - `status`: `open`, `decided: <decision>`, `imported` (with `library_dir`), or another match
     action (`auto`, `error`, `no match row`);
   - `local` (what the tags say, `verify` verdicts) and `best` (top MusicBrainz candidate);
   - `only_here`: files whose audio no other member has (by AcoustID).
2. Decide each group (see Judgment). Present ONE table:
   `group | tier | folder | quality | tracks | decision | why`, one row per member, the keeper
   first. Risky groups at the top.
3. Wait for the user. They may approve all, edit rows, or reject. Never assume approval.
4. Record exactly what was approved through the review queue, as one JSON list piped to
   `uv run musiclib review decide --by agent` (or `--by user` when the user chose):
   - keeper: `{"album_key": ..., "decision": "approve", "album_id": <only if not the top
     candidate>, "reason": "keeper of dupe group N: <why>"}`
   - loser: `{"album_key": ..., "decision": "skip", "reason": "duplicate of <keeper folder>
     (dupe group N): <why>"}`
   - a copy with no MusicBrainz match: `asis` (Unsorted/, D13) like any other album.
   Albums that are already imported or decided are not in the list; leave them as they are.
5. Import: `uv run musiclib import --which approved --apply` (and `--which unsorted --apply`
   for any `asis`). The usual post-import checks apply (D23 retag flags; report them).
6. Offer the next batch.

## Judgment

- **Keeper ranking (D6)**: lossless before lossy; then higher bitrate (V0/320k before less);
  then the complete copy; then verified MusicBrainz tags; then embedded art; then the larger
  file. The M2 `keeper` follows this, but check it against `local` and `best`.
- **Never import two copies of one release.** If a member is already `imported`, the open
  copies are skipped, unless one is clearly better (lossless vs lossy, or complete vs partial):
  then ask whether to upgrade. An approved upgrade imports the new copy first, then removes the
  old library files with `musiclib import --remove ... --reason "replaced by <folder>"` (as D32).
- **`only_here` is a hint, not proof.** AcoustID sometimes splits one song into two ids (other
  master, live vs studio). If both copies have a file with the same title and a similar length
  (as the Zappa *Dub Room Special* groups show for Inca Roads), it is the same track. A real
  unique track (a bonus track, a missing track in the better copy) is a reason to prefer the
  more complete copy or to ask.
- **Tier 2 partial**: prefer the copy that is both complete and better quality. When quality
  and completeness disagree (320k missing a track vs 192k complete), say so and ask; the
  missing track can't be joined in from the other copy.
- **Discs vs whole album**: a group of `CD 1` + `CD 2` folders and one full copy is one album
  either way. Keep the better copy; if that is the disc folders, approve one and join the other
  with `musiclib merge --albums A B` (dry run, then `--apply`) before import (or `import --redo --add` after, D41).
- **Tier 3 editions**: keep one unless the editions differ in ways the user would care about
  (a remaster beside the original, a deluxe with extra discs, a different tracklist). Name
  the difference and ask; default to the original release when they are otherwise equal.
- **Not duplicates after all**: compilations or splits that only share some tracks, or a
  12" single grouped with its album (Yabby You, *Babylon a Fall* vs *Deliver Me From My
  Enemies*). Keep both: decide each like any review album (`approve`/`asis`), and say why.
- **D10**: `carry_tags_from` names a loser whose verified tags were better; its release was
  already pinned on the keeper's match. Nothing extra to do, but mention it.

## Tone of the table

Short and specific: "FLAC 16/44, 12/12 confirmed" beats "better". Say what a skip loses
(nothing, or the named `only_here` track).
