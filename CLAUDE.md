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
- Config: `musiclib.toml` (`source_dir`, `state_dir`). Never run `beet` with the global config (D9).
