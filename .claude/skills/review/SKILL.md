---
name: review
description: Work through the music library review queue (albums beets couldn't match strongly). Use when the user runs /review, asks to review matches, clear the review queue, or decide what to do with unmatched albums. Proposes decisions in batches and records them only after the user approves each batch.
---

# Review queue

Albums land here when `musiclib match` found no strong MusicBrainz match (D15). Your job:
propose a decision per album, get the user's approval for the batch, then record and import.

Safety rules apply (docs/SPEC.md): never touch the dump, never record a decision the user
hasn't approved (rule 7), every decision needs a reason.

## Loop

1. `uv run musiclib review stats` to see what's open. Work kinds in this order:
   `close` → `weak` → `none` → `dupe` → `error`.
2. `uv run musiclib review list --kind <kind> --limit 20` for a batch. Each album carries:
   - `local`: what the files say (majority artist/album/date, sample titles, minutes) and
     `verify`, the M2 fingerprint verdicts (`confirmed` = tags match the audio).
   - `candidates`: top 3 MusicBrainz releases with `distance` and `penalties`.
   - `suggest` / `why`: a rule-based starting point. It is only a hint; override it.
3. Decide each album (see Judgment). Present ONE table to the user:
   `# | album folder | decision | release (artist – album, year, country) | why`
   Group obvious approvals together; call out anything you're unsure about.
4. Wait for the user. They may approve all, edit rows, or reject. Never assume approval.
5. Record exactly what was approved, as a JSON list piped to:
   `uv run musiclib review decide --by agent` (the user approved an agent proposal) or
   `--by user` (the user chose the release themselves).
   Each entry: `{"album_key": ..., "decision": "approve"|"asis"|"skip", "album_id": <only if
   not the top candidate>, "reason": "<short, specific>"}`. The batch is all-or-nothing.
6. Import: `uv run musiclib import --which approved --apply` and, if any `asis`,
   `uv run musiclib import --which unsorted --apply`. Report counts and any errors.
7. Offer the next batch.

## Judgment

- **Approve** when the release is clearly right:
  - the best candidate beats the runner-up by a wide gap (≥ 0.15), and
  - penalties are cosmetic (artist spelling like "Sun Ra" vs "The Sun Ra Arkestra",
    track-title differences, year/country/label/media of the pressing), and
  - track counts fit, or the mismatch is explained (a missing bonus track; a box set
    where fingerprints confirm every file we have).
  Strong support: `verify` mostly `confirmed`, and local artist/album agree with the candidate.
- **Pick a non-top candidate** (`album_id`) when local tags, year or track count clearly fit
  it better, e.g. the original LP vs a deluxe reissue with bonus tracks we don't have.
- **As-is (Unsorted/)** when no candidate is plausible: distance ≥ 0.5, wrong artist,
  bootlegs, DJ mixes, self-released or radio rips. D13 keeps the dump's names and tags.
- **Skip** unreadable folders (`error`) and anything the user wants to handle by hand.
- **Ask** rather than guess when two releases fit equally, or the files look like a mix
  of albums (several release IDs in one folder, or `verify` full of `mismatch`).
  Dub/instrumental versions that fingerprint as the originals (e.g. Burning Spear's
  *Living Dub*) are judgment calls: surface them.

## Files that don't fit the release

beets imports only files it maps to the release's tracks. `musiclib import` then puts the
leftovers (bonus tracks, strays) in the album folder under their dump name, tags untouched,
and skips music videos, whole-album single files and damaged files (D20). Say so in the table when an
album has `extra_items`, and name anything that looks like a video, a full-album file or
a track from another album.

## Tone of the table

Short and specific. "gap 0.42, artist naming only" beats "looks good". Put the risky rows
at the top so the user reads those first.
