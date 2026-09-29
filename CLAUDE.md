# music-agent

An agent that manages a personal music library: organize, MusicBrainz-tag and de-duplicate years of unsorted mp3/flac/wav dumps, and ingest new folders.

## Spec and decisions

- Read `docs/SPEC.md` before starting work. It covers architecture, safety rules, workflows, dedupe policy, open questions, the decision log and milestones.
- The canonical, living version is the Claude Doc: https://claude.ai/code/artifact/100be31d-9e99-440a-8f47-25fef6b76e95
  `docs/SPEC.md` is a synced copy. When a decision changes, update the doc first, then re-sync this file and bump its "Last synced" line.
- New design decisions get a new row in the decision log (`D<n>`); never rewrite an old row — mark it Superseded.
- Don't act on anything listed under "Open questions" without asking.

## Non-negotiable safety rules (see SPEC "Safety rules")

- The source dump is read-only: never write tags to, move or delete files in it.
- Import by copying into the separate library root; never move.
- Losing duplicates go to `quarantine/<date>/` with a manifest; never delete files.
- Every mutating command defaults to a dry run and needs `--apply`.
- Every change is recorded in the audit log.

## Tooling

- Python 3.12 via `uv`; the `musiclib` CLI emits JSON.
- Engine: beets (`beet`), Chromaprint (`fpcalc`), `ffprobe`. Picard is available for manual fixes.

## Commands

- `uv run musiclib inventory [--subdir P] [--limit N] [--no-fingerprint]` — scan the dump into `state/musiclib.db` (read-only, incremental; progress on stderr, JSON result on stdout).
- `uv run musiclib report [--top N]` — JSON summary: formats, bitrates, tag/MBID coverage, duplicate tiers, errors.
- `uv run pytest` — includes a test that the source tree is byte-for-byte unchanged after an inventory.
- `uv run musiclib acoustid [--limit N] [--retry-errors]` — batch-look up fingerprints on AcoustID into `acoustid_lookups` (resumable, rate-limited). `--titles` fetches recording titles for mismatch/suggest files; run it after `verify`, then re-run `verify`.
- `uv run musiclib verify [--top N]` — classify every file: tag confirmed / alt_recording / mismatch / unverifiable / suggest / unknown vs AcoustID; flags suspect folders. Read-only.
- `uv run musiclib dupes` — tiered duplicate groups with proposed keepers (tier 1 identical files, tier 2 album copies, tier 3 editions) into `dupe_groups`/`dupe_members`. Proposals only; nothing moves. Run after `verify`.
- `uv run musiclib match [--subdir P] [--limit N] [--rematch] [--summary]` — dry run: beets/MusicBrainz match per album into `matches` (auto / review / unsorted / error). Read-only on files; ~5 s/album (MB rate limit); resumable.
- beets runs only through `musiclib.beetsenv.setup()`, which generates `state/beets/config.yaml` from `musiclib.toml` and sets `BEETSDIR` (D9).
- `uv run musiclib import [--which auto|unsorted|approved] [--limit N] [--apply]` — without `--apply` prints the plan only. With it: stage a copy of each album (WAV→FLAC), beets moves staging→library with the matched release pinned; unsorted albums go to `Unsorted/<dump folder>/` as-is. beets never sees dump paths. Every file gets an `audit_log` row.
- `uv run musiclib report --not-imported` — D18 manifest: every dump file with its status (imported / duplicate + keeper / review / error / pending / unmatched), written to `state/reports/`.
- `uv run musiclib review stats|list --kind K|auto [--apply]|decide --by agent|user` — M4 review queue. Use the `/review` skill (`.claude/skills/review/`); never record decisions the user has not approved; `review auto` applies the D21 standing approval only.
- `uv run musiclib import --remove LIBRARY_FILE --reason "..." [--by agent|user]` — remove one library file (beets DB + disk), logged; refuses paths outside the library.
- `uv run musiclib import --repair-extras [--apply]` — place unmapped files missing from albums imported before D20.
- `uv run musiclib import --prune-duplicates [--apply]` — remove library copies of files later found to be duplicates (uses audit_log; library only).
- `uv run musiclib retag --scan | --album K [--apply]` — relabel an imported album whose tags sit on the wrong audio; acts only on a clean one-to-one pairing where fingerprints and track lengths agree.
- `uv run musiclib merge --suggest | --albums A B ... [--apply]` — combine folders of one release (disc 1/disc 2) into one album and re-match it; suggestions exclude copies (shared audio) and oversize groups.
- Config: `musiclib.toml` (`source_dir`, `state_dir`, `library_dir`); secrets such as `acoustid_key` go in gitignored `musiclib.local.toml`, never in the repo or the spec doc. Never run `beet` with the global config (D9).
