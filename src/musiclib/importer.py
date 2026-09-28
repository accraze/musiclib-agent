"""M3: import matched albums into the library. Dry run unless apply=True (SPEC safety rule 4).

Each album is copied from the dump into a staging folder (WAVs converted to FLAC, D7), and
beets moves it from staging into the library. beets never sees a dump path, so nothing it
does (tag writes, art embedding) can reach the dump (safety rules 1 and 2).

Modes:
  apply  the release chosen by `musiclib match` (or a reviewer) is pinned as the only
         candidate, so beets can't pick a different one (D16)
  asis   D13: Unsorted/<dump folder>/<dump filename>, tags untouched; beets isn't involved
"""

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ART_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    match_id     INTEGER PRIMARY KEY REFERENCES matches(id),
    run_id       INTEGER REFERENCES runs(id),
    mode         TEXT NOT NULL,          -- apply | asis
    status       TEXT NOT NULL,          -- imported | skipped | error
    album_id     TEXT,
    library_dir  TEXT,
    files        INTEGER,
    note         TEXT,
    imported_at  TEXT NOT NULL
);
"""

UNSORTED = "Unsorted"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    from .match import SCHEMA as MATCH_SCHEMA
    conn.executescript(MATCH_SCHEMA + SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    # Reviewer decisions (M4) live on the match row.
    for col, typ in (("decision", "TEXT"), ("decided_album_id", "TEXT"), ("decided_by", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")


def select(conn: sqlite3.Connection, which: str, limit: int | None = None,
           albums: list[str] | None = None) -> list[dict]:
    """Albums ready to import. which: auto | unsorted | approved; `albums` narrows to album keys."""
    where = {
        "auto": "m.action = 'auto' AND m.decision IS NULL",
        "unsorted": "(m.action = 'unsorted' AND m.decision IS NULL) OR m.decision = 'asis'",
        "approved": "m.decision = 'approve'",
    }[which]
    rows = conn.execute(f"""
        SELECT m.* FROM matches m LEFT JOIN imports i ON i.match_id = m.id AND i.status = 'imported'
        WHERE ({where}) AND i.match_id IS NULL ORDER BY m.album_key""").fetchall()
    if albums:
        rows = [r for r in rows if r["album_key"] in set(albums)]
    out = []
    for r in rows[:limit]:
        r = dict(r)
        asis = which == "unsorted"
        r["mode"] = "asis" if asis else "apply"
        r["pin"] = None if asis else (r["decided_album_id"] or r["album_id"])
        r["decided_by"] = r["decided_by"] or "auto"
        out.append(r)
    return out


def plan(conn: sqlite3.Connection, source: Path, which: str, limit: int | None = None,
         only: list[str] | None = None) -> dict:
    albums = select(conn, which, limit, only)
    total = 0
    items = []
    for a in albums:
        files = json.loads(a["files"])
        size = sum((source / f).stat().st_size for f in files)
        total += size
        items.append({"album_key": a["album_key"], "mode": a["mode"], "release": a["pin"],
                      "match": f"{a['albumartist']} - {a['album']} ({a['year']})" if a["pin"] else None,
                      "files": len(files), "wav_to_flac": sum(f.lower().endswith(".wav") for f in files),
                      "bytes": size})
    return {"which": which, "albums": len(albums), "bytes": total, "dry_run": True, "items": items}


def stage(source: Path, staging: Path, files: list[str], dirs: list[str]) -> dict[str, str]:
    """Copy an album's files (and local cover art) into staging. Returns {staged path: dump rel}."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    mapping = {}
    for i, rel in enumerate(files):
        src = source / rel
        base = f"{i:03d} {src.name}"
        if src.suffix.lower() == ".wav":  # D7
            dest = staging / (base[:-4] + ".flac")
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map_metadata", "0",
                            "-c:a", "flac", str(dest)], check=True)
        else:
            dest = staging / base
            shutil.copy2(src, dest)
        mapping[str(dest)] = rel
    for d in dirs:  # local art for fetchart's filesystem source
        for p in sorted((source / d).iterdir()):
            if p.is_file() and p.suffix[1:].lower() in ART_EXTS and not (staging / p.name).exists():
                shutil.copy2(p, staging / p.name)
    return mapping


@contextlib.contextmanager
def overrides(values: dict):
    """Temporarily layer config values on top of beets' config."""
    import beets
    beets.config.set(values)
    source = beets.config.sources[0]
    try:
        yield
    finally:
        beets.config.sources.remove(source)


def _session_class():
    from beets.importer import ImportSession
    from beets.importer.tasks import Action

    class PinnedSession(ImportSession):
        """Non-interactive: apply the pinned release, or import as-is; never guess."""

        def __init__(self, lib, paths, mode: str, pin: str | None):
            super().__init__(lib, None, paths, None)
            self.mode, self.pin, self.tasks, self.note = mode, pin, [], None

        def should_resume(self, path):
            return False

        def choose_match(self, task):
            self.tasks.append(task)
            if self.mode == "asis":
                return Action.ASIS
            for cand in task.candidates:
                if cand.info.album_id == self.pin:
                    return cand
            self.note = f"pinned release {self.pin} not returned by MusicBrainz"
            return Action.SKIP

        def choose_item(self, task):
            self.tasks.append(task)
            self.note = "unexpected singleton task"
            return Action.SKIP

        def resolve_duplicate(self, task, found_duplicates):
            raise RuntimeError("duplicate_action should be 'keep'")

    return PinnedSession


def import_album(lib, staging: Path, pin: str) -> tuple[list[tuple[str, str]], str | None]:
    """Run one beets session with `pin` as the only candidate. Returns ([(staged, library)], note)."""
    import beets.util

    values = {"import": {"move": True, "copy": False, "search_ids": [pin],
                         "group_albums": False, "singletons": False}}
    with overrides(values):
        session = _session_class()(lib, [os.fsencode(staging)], "apply", pin)
        session.run()
    moved = []
    for task in session.tasks:
        old = getattr(task, "old_paths", None) or []
        for before, item in zip(old, task.imported_items()):
            moved.append((beets.util.displayable_path(before), beets.util.displayable_path(item.path)))
    return moved, session.note


def import_asis(library_dir: Path, mapping: dict[str, str]) -> list[tuple[str, str]]:
    """D13: move staged files to Unsorted/, keeping the dump's folder and file names."""
    moved = []
    for staged, rel in mapping.items():
        dest = library_dir / UNSORTED / Path(rel).with_suffix(Path(staged).suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        while dest.exists():
            dest = dest.with_stem(f"{dest.stem.removesuffix(f' ({n - 1})')} ({n})")
            n += 1
        shutil.move(staged, dest)
        moved.append((staged, str(dest)))
    return moved


def open_library():
    """The beets library, with the configured (D12) path formats."""
    import beets
    from beets.library import Library
    from beets.ui import get_path_formats, get_replacements

    return Library(beets.config["library"].as_filename(), beets.config["directory"].as_filename(),
                   path_formats=get_path_formats(), replacements=get_replacements())


def run(conn: sqlite3.Connection, source: Path, staging_root: Path, which: str, *,
        limit: int | None = None, only: list[str] | None = None, progress=sys.stderr) -> dict:
    """Import for real. Callers must have run beetsenv.setup()."""
    import beets

    migrate(conn)
    lib = open_library()
    library_dir = Path(beets.config["directory"].as_filename())
    albums = select(conn, which, limit, only)
    run_id = conn.execute(
        "INSERT INTO runs (command, args, started_at, status) VALUES ('import', ?, ?, 'running')",
        (json.dumps({"which": which, "limit": limit, "albums": only}), now())).lastrowid
    conn.commit()
    counts = {"imported": 0, "skipped": 0, "error": 0}
    print(f"import: {len(albums)} albums ({which})", file=progress, flush=True)

    for n, a in enumerate(albums, 1):
        staging = staging_root / str(a["id"])
        files, dirs = json.loads(a["files"]), json.loads(a["dirs"])
        status, note, moved = "error", None, []
        try:
            mapping = stage(source, staging, files, dirs)
            if a["mode"] == "asis":
                moved = import_asis(library_dir, mapping)
            else:
                moved, note = import_album(lib, staging, a["pin"])
            status = "imported" if len(moved) == len(files) else "skipped" if not moved else "error"
            if status == "error":
                note = f"only {len(moved)} of {len(files)} files imported"
            reason = (f"match {a['action']} {a['recommendation']} d={a['distance']} release {a['pin']}"
                      if a["mode"] == "apply" else "no MusicBrainz match: as-is into Unsorted (D13)")
            conn.executemany(
                "INSERT INTO audit_log (ts, run_id, action, source_path, dest_path, reason, decided_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(now(), run_id, "import" if a["mode"] == "apply" else "import_asis",
                  mapping.get(before, before), after,
                  reason + (" (wav->flac)" if mapping.get(before, "").lower().endswith(".wav") else ""),
                  a["decided_by"]) for before, after in moved])
        except Exception as e:
            note = f"{type(e).__name__}: {e}"[:300]
        finally:
            shutil.rmtree(staging, ignore_errors=True)  # staging copies only, never dump files
        lib_dir = os.path.dirname(moved[0][1]) if moved else None
        conn.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (a["id"], run_id, a["mode"], status, a["pin"], lib_dir, len(moved), note, now()))
        conn.commit()
        counts[status] += 1
        print(f"  [{n}/{len(albums)}] {status}: {a['album_key']} -> {lib_dir or note}",
              file=progress, flush=True)

    conn.execute("UPDATE runs SET finished_at = ?, status = 'ok' WHERE id = ?", (now(), run_id))
    conn.commit()
    return {"run_id": run_id, "which": which, **counts}


def prune_duplicates(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Remove library copies whose dump source was later marked a duplicate (auto file groups),
    when the keeper's copy is in the library too. Library-only; the dump is never touched."""
    rows = conn.execute("""
        SELECT a.source_path, a.dest_path, g.keeper, g.reason FROM audit_log a
        JOIN dupe_members m ON m.path = a.source_path AND m.role = 'drop'
        JOIN dupe_groups g ON g.id = m.group_id AND g.scope = 'file' AND g.action = 'auto'
        WHERE a.action LIKE 'import%'
          AND EXISTS (SELECT 1 FROM audit_log k WHERE k.source_path = g.keeper AND k.action LIKE 'import%')
    """).fetchall()
    targets = [dict(r) for r in rows if Path(r["dest_path"]).exists()]
    if not apply:
        return {"dry_run": True, "would_remove": len(targets), "items": targets}
    lib = open_library()
    by_path = {os.fsdecode(i.path): i for i in lib.items()}
    removed = 0
    for t in targets:
        item = by_path.get(t["dest_path"])
        if item is not None:
            item.remove(delete=True)
        else:
            Path(t["dest_path"]).unlink()
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'prune_duplicate', ?, ?, ?, 'auto')",
                     (now(), t["source_path"], t["dest_path"], f"{t['reason']}; kept {t['keeper']}"))
        removed += 1
    conn.commit()
    return {"removed": removed}
