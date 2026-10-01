# music-agent

Turns years of unsorted mp3/flac/wav dumps into one clean library tagged with MusicBrainz data and free of duplicates, then keeps it that way as new music arrives.

Deterministic code and [beets](https://beets.io) do the mechanical work: scanning, fingerprinting, matching and copying. A Claude Code agent handles the judgment calls, such as ambiguous matches, which duplicate to keep, and tags that look wrong. It proposes decisions in batches, and nothing is recorded until you approve.

## How it works

```
dump ──► inventory ──► verify tags ──► dedupe ──► match ──► import ──► library
         (hash +       (vs AcoustID)   (3 tiers)  (MusicBrainz)
          fingerprint)                               │
                                                     └─► review queue ──► you approve
```

1. **Inventory:** scan the dump into a SQLite state DB. Each file gets a hash, a Chromaprint fingerprint and a copy of its tags.
2. **Verify:** check existing MusicBrainz tags against AcoustID fingerprint lookups.
3. **Dedupe:** group duplicates (identical files, copies of the same album, different editions) and propose a keeper for each group.
4. **Match:** look up each album on MusicBrainz through beets. Strong matches import automatically; weak ones go to the review queue.
5. **Import:** copy the keepers into the library, organized and tagged.
6. **Ingest:** new folders dropped into the inbox go through the same pipeline and are also deduped against the library.

## Safety

The tools are built to be hard to misuse on a collection you care about:

- **The dump is read-only.** Nothing writes tags to, moves or deletes files in it.
- **Copy, never move.** The library is a separate directory.
- **Nothing is deleted.** Duplicates are simply not imported, and a report lists every dump file that wasn't imported and why.
- **Dry run by default.** Every command that changes something only prints its plan unless you pass `--apply`.
- **Everything is logged.** Every change gets an audit-log row that records who decided it (auto, agent or you).

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Chromaprint (`fpcalc`) and `ffprobe` (from ffmpeg).

```sh
uv sync
```

Point `musiclib.toml` at your directories:

```toml
source_dir  = "/path/to/unsorted/dump"   # read-only
library_dir = "/path/to/clean/library"
inbox_dir   = "/path/to/inbox"
state_dir   = "state"
```

Put secrets in a gitignored `musiclib.local.toml`:

```toml
acoustid_key = "..."
```

beets runs with its own generated config under `state/beets/`, so it won't touch a beets setup you already have.

## Usage

Every command prints JSON. A typical first run:

```sh
uv run musiclib inventory      # scan the dump (read-only, resumable)
uv run musiclib report         # formats, tag coverage, duplicates
uv run musiclib acoustid       # fingerprint lookups
uv run musiclib verify         # check tags against fingerprints
uv run musiclib dupes          # propose duplicate keepers
uv run musiclib match          # MusicBrainz matching (dry run)
uv run musiclib import         # show the import plan
uv run musiclib import --apply # actually import
```

Run Claude Code in the repo for the parts that need judgment:

- `/review` works through albums that didn't match strongly, in batches you approve.
- `/ingest` processes new folders in the inbox.

`CLAUDE.md` has the full command reference.

## Development

```sh
uv run pytest
```

The tests include a check that the source dump is byte-for-byte unchanged after an inventory.

The design lives in [`docs/SPEC.md`](docs/SPEC.md), a synced copy of the canonical spec doc. It covers the architecture, safety rules, duplicate policy, decision log and milestones.
