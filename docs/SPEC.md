<!--
Synced copy of the living spec doc:
https://claude.ai/code/artifact/100be31d-9e99-440a-8f47-25fef6b76e95
Discussion and edits happen in the doc; re-sync this file after changes.
Last synced: 2026-09-24 (doc rev 15)
-->

# Music Library Agent — Spec

## Overview

The goal is a single clean, MusicBrainz-tagged, de-duplicated library built from years of unsorted dumps, plus a repeatable way to add new folders to it.

**The problem:** one flat directory holds years of mp3, flac and wav folders. Nothing is sorted, duplicates are likely, and some MusicBrainz tags are probably wrong.

**Goals**

- Build one organized library with consistent folders and filenames.
- Verify MusicBrainz tags against acoustic fingerprints, not just against the existing tags.
- Find duplicates and keep the best copy of each by an explicit policy.
- Ingest a new folder in one step, with the same checks as the first cleanup.
- Log every action so any decision can be traced and undone.

**Non-goals (for now)**

- Playback, streaming or a UI. A media server such as Navidrome, Jellyfin or Plex can point at the finished library.
- Recommendations or playlist generation.
- Automatic deletion of files.

## Architecture

Beets and deterministic code do the mechanical work. The agent only handles judgment calls: ambiguous matches, choosing a keeper, and suspect tags.

```mermaid
flowchart TD
  A[Claude agent<br/>judgment + reports] --> B[musiclib CLI<br/>JSON in / JSON out]
  B --> C[beets<br/>chroma, mbsync, duplicates,<br/>fetchart, embedart, badfiles]
  B --> D[(State DB - SQLite<br/>inventory, review queue,<br/>audit log)]
  C --> E[(beets library.db)]
  C --> F[MusicBrainz + AcoustID]
```

The agent never touches files directly. It calls `musiclib` subcommands, and each subcommand supports `--dry-run`.

| Layer | Responsibility | Tech |
| --- | --- | --- |
| Agent | Resolve the review queue, pick duplicate keepers, explain decisions | Claude Code with CLAUDE.md and skills (`/ingest`, `/dedupe`, `/audit`) |
| musiclib CLI | Inventory, duplicate grouping, match candidates, apply, quarantine, report | Python 3.12, managed with `uv` |
| Engine | Tagging, fingerprinting, file moves, art | beets, Chromaprint (`fpcalc`), `ffprobe` |
| State | File inventory, hashes, fingerprints, review items, audit log | SQLite |

**Harness:** Claude Code comes first, for interactive use with no agent-loop code to maintain. When ingestion needs to run unattended (a watched inbox or cron), the same `musiclib` subcommands become tools in the Claude Agent SDK.

## Safety rules

Nothing irreversible happens without an explicit human step. These rules override any workflow below.

1. **The source dump is read-only.** The tools never write tags to, move or delete anything in it.
2. **Copy, don't move.** Beets imports with `copy: yes` into a separate library root.
3. **Quarantine, don't delete.** Losing duplicates go to `quarantine/<date>/` with a manifest. Only you empty it.
4. **Dry run first.** Every mutating `musiclib` subcommand prints its plan and needs `--apply` to make changes.
5. **Log everything.** Each change writes a row to the audit log: timestamp, action, source path, destination path, reason, and who decided (auto, agent or you).
6. **Confidence gates.** Only matches above the auto threshold import without review. Everything else goes to the review queue.
7. **Batch approval.** The agent proposes changes in batches, and you approve or reject each batch.

## Workflows

There are three workflows, and they share one pipeline. Ingestion is the initial cleanup run on a smaller input.

```mermaid
flowchart LR
  A[Inventory<br/>hash + fingerprint] --> B[Dedupe<br/>3 tiers]
  B --> C[Verify tags<br/>MBID vs AcoustID]
  C --> D{Confidence}
  D -->|high| E[Auto-import<br/>beet import, copy]
  D -->|low| F[Review queue<br/>agent proposes]
  F -->|you approve| E
  E --> G[Report + audit log]
```

### 1. Initial cleanup (one time)

1. **Inventory:** walk the dump. For each file, record path, format, codec, bitrate, sample rate, duration, size, SHA-256, Chromaprint fingerprint and existing tags (MusicBrainz IDs included) in the state DB. Read-only.
2. **Dedupe:** group files at the three duplicate tiers (next section) and choose a keeper for each group.
3. **Verify tags:** where a file already has a MusicBrainz recording ID, compare it with the AcoustID lookup. Flag mismatches.
4. **Auto-import:** run `beet import` on the keepers. Matches above the threshold land in the library.
5. **Review:** the agent works through the queue using folder names, tags, fingerprints and MusicBrainz searches. It proposes a match, `as-is` or skip.
6. **Quarantine:** move the non-keepers out, with a manifest.
7. **Report:** totals, duplicates found, space reclaimed, and what is still unresolved.

### 2. Ingestion (new folders)

1. Drop a folder into `inbox/`.
2. Run `/ingest`, or have a watcher or cron job trigger it once unattended mode exists.
3. The folder goes through the same pipeline, and dedupe also checks against the existing library, not just within the batch.
4. The folder is archived to `inbox/.processed/<date>/` and a short summary is produced.

### 3. Periodic audit

- `beet mbsync`: pull MusicBrainz corrections.
- `beet bad`: find corrupt or truncated files.
- `beet missing`: list incomplete albums.
- Report files missing cover art or MusicBrainz IDs.

## Duplicates and keeper policy

Duplicates are detected at three tiers, from certain to fuzzy. Tiers 1 and 2 can be resolved automatically. Tier 3 always goes to review.

| Tier | What matches | Detected by | Resolution |
| --- | --- | --- | --- |
| 1. Identical file | Same bytes | SHA-256 | Auto: keep one, quarantine the rest |
| 2. Same recording | Same audio, different encode or tags | AcoustID or MusicBrainz recording ID; Chromaprint similarity | Auto by keeper ranking |
| 3. Same release, different edition | Remaster, deluxe, regional pressing | MusicBrainz release group | Review: may be intentional |

**Keeper ranking (draft, to be confirmed):**

1. Lossless before lossy: FLAC, then WAV, then others.
2. Among lossy files: higher bitrate, and V0 or 320k before lower rates.
3. Complete album before a partial one.
4. Valid, fingerprint-consistent MusicBrainz tags before missing or conflicting ones.
5. Embedded art present.
6. Tiebreak: larger file, then the earliest path found.

## Inventory results (M1)

The first full inventory ran on 2026-09-24: 58,550 audio files (588.6 GB, about 4,350 hours) in 4,939 folders, scanned in 7.6 minutes. About 88% of files already carry MusicBrainz IDs. Duplicates look modest, but many tags are wrong in a telling way.

| Codec | Files | Size (GB) |
| --- | --- | --- |
| MP3 | 53,706 | 466.2 |
| FLAC | 3,653 | 107.5 |
| ALAC | 263 | 7.0 |
| Opus | 475 | 2.7 |
| AAC | 212 | 2.5 |
| Other (WMA, Vorbis, WAV, AIFF, APE) | 241 | 2.9 |

- **MP3 quality:** 23,688 at 320k, 13,739 VBR, 10,234 at 192–319k, 6,039 below 192k.
- **Tag coverage:** artist 98%, MusicBrainz recording and release IDs 88%, AcoustID 27%, embedded art 88%. 363 folders have no MusicBrainz IDs at all.
- **Identical files (tier 1):** 176 groups, 190 extra copies, 1.9 GB. Example: the same Sun Ra album at the root and under `Sun Ra/`.
- **Identical fingerprints (tier 2 preview):** 425 groups, 520 extra copies, 4.1 GB. This counts exact matches only; fuzzy matching in M2 will find more.
- **Same MusicBrainz recording ID:** 2,347 groups. 1,748 span different folders and are probably real duplicates. 599 groups (1,454 files) sit inside a single folder, which almost always means bad tags: different tracks of one album carrying the same recording ID.
- **Other tag problems:** 416 folders mix more than one release ID, and 286 release IDs are spread over more than one folder.
- **Damaged files:** 34 files can't be fingerprinted (empty or truncated audio). 576 more decode with errors but did fingerprint.
- **Non-audio:** 22,181 files, mostly `.mood` (14,012), cover art, `.cue`, `.log` and a few `.cbr` comics.

**What this means for M2:** existing MusicBrainz IDs can't be trusted on their own. Verification has to compare them against fingerprints, and a folder with repeated recording IDs should be flagged for re-matching.

## Open questions

These need an answer before Milestone 2 (import). Milestone 1 (inventory) needs only the dump path.

- [ ] Where should the clean library live? It must be outside the dump, which is confirmed at /srv/data/media/music.
- [x] Dump size: 633.6 GB, about 58,550 audio files. A 500-file trial ran at about 1.1 GB/s, so the full inventory takes about 10 minutes and no overnight batch is needed.
- [ ] When a lossless copy exists, drop the lossy duplicates, or keep a lossy copy for portable devices?
- [ ] Confirm WAV-to-FLAC conversion (D7).
- [ ] Folder and filename layout, e.g. `$albumartist/$year - $album/$disc$track $title`. Does a media server need to read it?
- [ ] Auto-import confidence threshold. The beets default is strong-recommendation distance ≤ 0.04; stricter means more review.
- [ ] Should an unmatched file be imported as-is into `Unsorted/`, or stay in the review queue?
- [ ] Is there an AcoustID API key? It's free, and needed for fingerprint lookups.

## Decision log

Every design decision is recorded here, newest first. To reverse one, mark it Superseded and add a new row; don't edit the old one.

| # | Date | Decision | Rationale | Status |
| --- | --- | --- | --- | --- |
| D9 | 2026-09-24 | Use a project-local beets config (BEETSDIR); never the global ~/.config/beets | The global config points directory at the dump with move, write and quiet all on, which would violate safety rules 1 and 2 | Accepted |
| D8 | 2026-09-24 | Mirror the spec into `docs/SPEC.md`; the Claude Doc stays canonical | Agent sessions get the spec as repo context; discussion stays in the doc | Accepted |
| D7 | 2026-09-24 | Convert WAV to FLAC on import | WAV tagging is unreliable; FLAC is lossless and fully taggable | Proposed |
| D6 | 2026-09-24 | Keeper ranking as drafted in *Duplicates and keeper policy* | Prefer quality, then completeness, then tag correctness | Proposed |
| D5 | 2026-09-24 | Track the spec and decisions in this doc | One living record we both edit and comment on | Accepted |
| D4 | 2026-09-24 | Agent acts only through `musiclib` JSON subcommands | Clear tool boundary; testable; portable to Agent SDK | Accepted |
| D3 | 2026-09-24 | Non-destructive: read-only source, copy on import, quarantine not delete | Years of files; mistakes must be recoverable | Accepted |
| D2 | 2026-09-24 | Claude Code first; Agent SDK later for unattended ingestion | No agent-loop code to start; same tools carry over | Accepted |
| D1 | 2026-09-24 | Use beets as the tagging and organizing engine | Mature MusicBrainz matching, Chromaprint and duplicate plugins; already installed | Accepted |

## Milestones

Milestone 1 is read-only and starts once the dump path is known. Each later milestone needs the open questions it depends on answered.

| # | Milestone | Deliverable | Status |
| --- | --- | --- | --- |
| M1 | Inventory | `musiclib inventory`: state DB with hashes, fingerprints and tags; a summary report of formats, MBID coverage and exact duplicates | Done |
| M2 | Dedupe + verify | `musiclib dupes` and `musiclib verify`: duplicate groups at three tiers, keeper picks, tag mismatch list | Not started |
| M3 | Import | beets config, `musiclib import` (dry run, then apply), quarantine and manifest | Not started |
| M4 | Review agent | CLAUDE.md, `/dedupe` and `/review` skills, batch approval flow | Not started |
| M5 | Ingestion | `inbox/` pipeline and `/ingest` skill, with dedupe against the existing library | Not started |
| M6 | Audit + unattended | `/audit`; optional Agent SDK runner triggered by a watcher or cron | Not started |
