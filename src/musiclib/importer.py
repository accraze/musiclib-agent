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

from . import retag

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
    from .acoustid import migrate as acoustid_migrate
    from .match import SCHEMA as MATCH_SCHEMA
    acoustid_migrate(conn)
    conn.executescript(MATCH_SCHEMA + SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    # Reviewer decisions (M4) live on the match row; batch_id marks ingested albums (D31).
    for col, typ in (("decision", "TEXT"), ("decided_album_id", "TEXT"), ("decided_by", "TEXT"),
                     ("batch_id", "INTEGER")):
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


def _free_path(dest: Path) -> Path:
    n = 2
    base = dest
    while dest.exists():
        dest = base.with_stem(f"{base.stem} ({n})")
        n += 1
    return dest


VIDEO_EXTS = {"mp4", "m4v", "mkv", "webm", "mov", "avi"}
MIN_DECODABLE = 0.5  # a file is damaged if less than half its stated length decodes


def decodable_seconds(path: Path) -> float:
    """How much audio ffmpeg can actually decode (it reads through bad frames)."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True)
    times = [t for t in out.stderr.replace("\r", "\n").split() if t.startswith("time=")]
    if not times:
        return 0.0
    h, m, sec = times[-1][5:].split(":")
    try:
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except ValueError:
        return 0.0


def is_damaged(conn: sqlite3.Connection, source: Path, rel: str) -> bool:
    """Only files that failed to scan are checked, by decoding them. fpcalc often fails on a
    single bad header while the audio plays fine, so a scan error alone isn't damage."""
    row = conn.execute("SELECT error, duration FROM files WHERE path = ?", (rel,)).fetchone()
    if not row or not row[0]:
        return False
    expected = row[1] or 0
    return decodable_seconds(source / rel) < MIN_DECODABLE * expected if expected else True


def extra_skip_reason(conn: sqlite3.Connection, source: Path, rel: str, album_files: list[str],
                      imported: list[str] = ()) -> str | None:
    """Why an unmapped file shouldn't go into the album folder, or None to keep it.
    `imported`: dump files of this album that beets did place (for the duplicate check)."""
    if rel.rsplit(".", 1)[-1].lower() in VIDEO_EXTS:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                                "stream=codec_name", "-of", "csv=p=0", str(source / rel)],
                               capture_output=True, text=True)
        if any(c not in ("mjpeg", "png") for c in probe.stdout.split()):
            return "video file, not audio"
    if is_damaged(conn, source, rel):
        return "damaged file: less than half of it decodes"
    if _is_whole_album_file(conn, rel, album_files):
        return "whole album as one file; the split tracks were imported"
    dup = _duplicate_of(conn, rel, imported)
    if dup:
        return f"duplicate of '{dup}', already imported on this album"
    return None


COPY_SIMILARITY = 0.9  # fingerprint similarity of two encodes of the same recording (~0.98)


def _duplicate_of(conn: sqlite3.Connection, rel: str, imported: list[str]) -> str | None:
    """Same title tag, a length within 3 s, and (when both are fingerprinted) the same audio by
    direct fingerprint comparison: a second copy of a track already on the album. AcoustID ids
    aren't enough (it splits encodes); a bonus track with a copied title tag differs in length;
    a different master of the same recording scores below the threshold and is kept."""
    from .fpsim import similarity

    if not imported:
        return None
    q = "SELECT lower(trim(title)), duration, title, fingerprint FROM files WHERE path IN ({})"
    me = conn.execute(q.format("?"), (rel,)).fetchone()
    if not me or not me[0] or me[1] is None:
        return None
    marks = ",".join("?" * len(imported))
    for title, dur, orig, fp in conn.execute(q.format(marks), list(imported)):
        if title != me[0] or dur is None:
            continue
        if fp and me[3]:
            # Fingerprints decide; allow a looser length window for different fades/masterings.
            if abs(dur - me[1]) <= 10 and similarity(fp, me[3]) >= COPY_SIMILARITY:
                return orig
        elif abs(dur - me[1]) <= 3:
            return orig
    return None


def _is_whole_album_file(conn: sqlite3.Connection, rel: str, album_files: list[str]) -> bool:
    """Long AND named like the album (or 'full album') AND there are real split tracks.
    Length alone misfires: a 28-minute live bonus or a 9-minute track on a 3-track 10".
    The name misfires on a long title track (Sun Ra, The Magic City): a file AcoustID
    confirms as one recording is never a whole album."""
    from .verify import norm_title

    try:
        if conn.execute("SELECT 1 FROM files f JOIN verify v ON v.file_id = f.id "
                        "WHERE f.path = ? AND v.verdict = 'confirmed'", (rel,)).fetchone():
            return False
    except sqlite3.OperationalError:  # verify has not run: no verdicts to go on
        pass
    marks = ",".join("?" * len(album_files))
    durs = dict(conn.execute(f"SELECT path, COALESCE(duration, 0) FROM files WHERE path IN ({marks})",
                             album_files).fetchall())
    others = [d for f, d in durs.items() if f != rel]
    if len(others) < 4 or durs.get(rel, 0) < 0.8 * sum(others):
        return False
    row = conn.execute("SELECT title, album FROM files WHERE path = ?", (rel,)).fetchone()
    title, album = (row[0], row[1]) if row else (None, None)
    names = [norm_title(Path(rel).stem), norm_title(title)]
    album_n = norm_title(album)
    return any("full album" in n or (album_n and album_n in n) for n in names if n) or \
        "full album" in Path(rel).stem.lower()


def _skip_rows(run_id, skipped, decided_by):
    return [(now(), run_id, "skip_extra", rel, None, why, decided_by) for rel, why in skipped]


def place_extras(album_dir: Path, files: list[tuple[Path, str]], *, move: bool) -> list[tuple[str, str]]:
    """Files beets didn't map to the release (bonus tracks, strays) go into the album folder
    under their dump file name, tags untouched. beets itself only imports mapped files."""
    placed = []
    for src, rel in files:
        dest = _free_path(album_dir / (Path(rel).stem + src.suffix))
        if src.suffix.lower() == ".wav":  # only when copying straight from the dump (D7)
            dest = _free_path(dest.with_suffix(".flac"))
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map_metadata", "0",
                            "-c:a", "flac", str(dest)], check=True)
        elif move:
            shutil.move(src, dest)
        else:
            shutil.copy2(src, dest)
        placed.append((str(src), str(dest)))
    return placed


def unsorted_name(rel: str) -> Path:
    """Where a file goes under Unsorted/ (D13): its dump path, or for an inbox file (absolute,
    D29) its path below inbox/.processed/<date>/, i.e. starting at the dropped folder."""
    p = Path(rel)
    if not p.is_absolute():
        return p
    parts = p.parts
    if ".processed" in parts:
        i = len(parts) - 1 - parts[::-1].index(".processed")
        if len(parts) > i + 3:  # .processed/<date>/<folder>/.../<file>
            return Path(*parts[i + 2:])
    return Path(*parts[-2:])


def import_asis(library_dir: Path, mapping: dict[str, str]) -> list[tuple[str, str]]:
    """D13: move staged files to Unsorted/, keeping the dump's folder and file names."""
    moved = []
    unsorted = library_dir / UNSORTED
    for staged, rel in mapping.items():
        dest = unsorted / unsorted_name(rel).with_suffix(Path(staged).suffix)
        if not dest.resolve().is_relative_to(unsorted.resolve()):
            raise RuntimeError(f"refusing to place {rel} outside {unsorted}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        while dest.exists():
            dest = dest.with_stem(f"{dest.stem.removesuffix(f' ({n - 1})')} ({n})")
            n += 1
        shutil.move(staged, dest)
        moved.append((staged, str(dest)))
    return moved


@contextlib.contextmanager
def library_lock(state_dir: Path):
    """One library writer at a time: concurrent beets sessions (review imports, ingest)
    would contend for library.db and could race on destination paths."""
    import fcntl

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "library.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another command is changing the library (state/library.lock); "
                             "try again when it finishes") from None
        yield


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
        # Damaged files (less than half decodes) never reach beets, mapped or not.
        damaged = [f for f in files if is_damaged(conn, source, f)]
        try:
            mapping = stage(source, staging, [f for f in files if f not in damaged], dirs)
            extras, skipped = [], [(f, "damaged file: less than half of it decodes") for f in damaged]
            if a["mode"] == "asis":
                moved = import_asis(library_dir, mapping)
            else:
                moved, note = import_album(lib, staging, a["pin"])
                if moved:
                    done = {before for before, _ in moved}
                    leftover = []
                    for st, rel in mapping.items():
                        if st in done:
                            continue
                        why = extra_skip_reason(conn, source, rel, files, [mapping[b] for b in done])
                        (skipped.append((rel, why)) if why else leftover.append((Path(st), rel)))
                    extras = place_extras(Path(moved[0][1]).parent, leftover, move=True)
            placed = len(moved) + len(extras) + len(skipped)
            status = "imported" if placed == len(files) else "skipped" if not placed else "error"
            if status == "error":
                note = f"only {placed} of {len(files)} files imported"
            elif extras or skipped:
                note = (f"{len(extras)} file(s) not on the release kept in the album folder; "
                        f"{len(skipped)} skipped")
            reason = (f"match {a['action']} {a['recommendation']} d={a['distance']} release {a['pin']}"
                      if a["mode"] == "apply" else "no MusicBrainz match: as-is into Unsorted (D13)")
            conn.executemany(
                "INSERT INTO audit_log (ts, run_id, action, source_path, dest_path, reason, decided_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(now(), run_id, "import" if a["mode"] == "apply" else "import_asis",
                  mapping.get(before, before), after,
                  reason + (" (wav->flac)" if mapping.get(before, "").lower().endswith(".wav") else ""),
                  a["decided_by"]) for before, after in moved]
                + [(now(), run_id, "import_extra", mapping[before], after,
                    f"not on release {a['pin']}: kept in the album folder, tags untouched", a["decided_by"])
                   for before, after in extras]
                + _skip_rows(run_id, skipped, a["decided_by"]))
        except Exception as e:
            note = f"{type(e).__name__}: {e}"[:300]
        finally:
            shutil.rmtree(staging, ignore_errors=True)  # staging copies only, never dump files
        lib_dir = os.path.dirname(moved[0][1]) if moved else None
        conn.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (a["id"], run_id, a["mode"], status, a["pin"], lib_dir,
                      len(moved) + len(extras), note, now()))
        conn.commit()
        if status == "imported" and a["mode"] == "apply":
            try:
                d23 = retag.after_import(conn, lib, a["album_key"], files)
            except Exception as e:  # never let the check break an import that succeeded
                d23 = f"D23 check failed: {type(e).__name__}: {e}"[:200]
            if d23:
                note = f"{note}; {d23}" if note else d23
                conn.execute("UPDATE imports SET note = ? WHERE match_id = ?", (note, a["id"]))
                conn.commit()
        if status == "imported" and a.get("batch_id"):  # an ingested album (D31), via any command
            from .ingest import refresh_status
            refresh_status(conn, a["batch_id"])
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


def repair_extras(conn: sqlite3.Connection, source: Path, *, apply: bool = False) -> dict:
    """Albums imported before place_extras existed lack their unmapped files: copy those from
    the dump (read-only) into the album folder, tags untouched, and mark the album imported."""
    todo = []
    for r in conn.execute("""
        SELECT i.match_id, i.library_dir, i.album_id, m.album_key, m.files, m.decided_by FROM imports i
        JOIN matches m ON m.id = i.match_id
        WHERE i.status = 'error' AND i.mode = 'apply' AND i.library_dir IS NOT NULL"""):
        done = {x[0] for x in conn.execute(
            "SELECT source_path FROM audit_log WHERE action LIKE 'import%' AND source_path IN "
            "(SELECT value FROM json_each(?))", (r["files"],))}
        missing = [f for f in json.loads(r["files"]) if f not in done]
        if missing:
            todo.append((dict(r), missing))
    stale = stale_skips(conn, source)
    if not apply:
        return {"dry_run": True, "albums": len(todo),
                "files": [{"album": r["album_key"], "missing": m,
                           "skip": {f: why for f in m
                                    if (why := extra_skip_reason(conn, source, f, json.loads(r["files"])))}}
                          for r, m in todo],
                "skips_no_longer_apply": [{"album": s["album_key"], "file": s["rel"], "was": s["was"],
                                           "into": s["album_dir"]} for s in stale]}
    for s in stale:
        (_, dest), = place_extras(Path(s["album_dir"]), [(source / s["rel"], s["rel"])], move=False)
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'import_extra', ?, ?, ?, 'auto')",
                     (now(), s["rel"], dest, f"skipped earlier ({s['was']}), which the current rules "
                                             f"no longer support: kept in the album folder, tags untouched"))
        conn.commit()
    fixed = 0
    for r, missing in todo:
        album_files = json.loads(r["files"])
        placed_before = [f for f in album_files if f not in missing]
        skipped = [(f, why) for f in missing
                   if (why := extra_skip_reason(conn, source, f, album_files, placed_before))]
        missing = [f for f in missing if f not in dict(skipped)]
        conn.executemany("INSERT INTO audit_log (ts, run_id, action, source_path, dest_path, reason, decided_by) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", _skip_rows(None, skipped, r["decided_by"] or "auto"))
        placed = place_extras(Path(r["library_dir"]), [(source / f, f) for f in missing], move=False)
        conn.executemany(
            "INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) VALUES (?, ?, ?, ?, ?, ?)",
            [(now(), "import_extra", rel, dest,
              f"not on release {r['album_id']}: kept in the album folder, tags untouched (repair)",
              r["decided_by"] or "auto") for (_, dest), rel in zip(placed, missing)])
        conn.execute("UPDATE imports SET status = 'imported', files = files + ?, note = ? WHERE match_id = ?",
                     (len(placed), f"{len(placed)} file(s) not on the release kept in the album folder; "
                                   f"{len(skipped)} skipped", r["match_id"]))
        conn.commit()
        fixed += 1
    return {"repaired_albums": fixed, "re_placed_skips": len(stale)}


def stale_skips(conn: sqlite3.Connection, source: Path) -> list[dict]:
    """Files of imported albums that an earlier rule skipped (skip_extra) and that the
    current rules would keep, e.g. a title track once mistaken for a whole-album file."""
    out = []
    for r in conn.execute("""
        SELECT m.album_key, m.files FROM imports i JOIN matches m ON m.id = i.match_id
        WHERE i.status = 'imported' AND i.mode = 'apply'"""):
        files = json.loads(r["files"])
        last = {}
        for action, src, reason in conn.execute(
                "SELECT action, source_path, reason FROM audit_log WHERE source_path IN "
                "(SELECT value FROM json_each(?)) ORDER BY id", (r["files"],)):
            last[src] = (action, reason)
        skipped = [(f, why) for f, (action, why) in last.items() if action == "skip_extra"]
        if not skipped:
            continue
        placed = current_paths(conn, files)
        if not placed:
            continue
        for rel, was in skipped:
            if extra_skip_reason(conn, source, rel, files, list(placed)) is None:
                out.append({"album_key": r["album_key"], "rel": rel, "was": was,
                            "album_dir": os.path.dirname(next(iter(placed.values())))})
    return out


PLACED_ACTIONS = ("import", "import_asis", "import_extra", "retag_by_fingerprint", "relocate")


def current_paths(conn: sqlite3.Connection, sources: list[str] | None = None) -> dict[str, str]:
    """dump file -> its library path now (latest import/retag row), minus files since removed.
    Replayed in log order, so a file placed again after a removal (retag promotion) counts."""
    return {s: d for s, (d, _) in placements(conn, sources).items()}


def placements(conn: sqlite3.Connection, sources: list[str] | None = None) -> dict[str, tuple[str, str]]:
    """dump file -> (library path now, action that put it there)."""
    actions = PLACED_ACTIONS + ("remove_from_library",)
    marks = ",".join("?" * len(actions))
    rows = conn.execute(f"""SELECT action, source_path, dest_path FROM audit_log
        WHERE action IN ({marks}) AND dest_path IS NOT NULL ORDER BY id""", actions).fetchall()
    out = {}
    for action, src, dest in rows:
        if action == "remove_from_library":
            out = {s: v for s, v in out.items() if v[0] != dest}
        elif sources is None or src in sources:
            out[src] = (dest, action)
    return out


def restore(conn: sqlite3.Connection, source: Path, rel: str, reason: str, decided_by: str, *,
            track: int | None = None, apply: bool = False) -> dict:
    """Put a dump file that was removed from the library back into its album folder, copied
    from the dump (read-only): as an extra, or with `track` on that release track (the user's
    decision; the track must be free). Dry run unless `apply`."""
    if decided_by != "user":
        raise SystemExit("--restore is the user's call: use --by user")
    if rel in current_paths(conn):
        raise SystemExit(f"{rel} is already in the library at {current_paths(conn)[rel]}")
    if not (source / rel).is_file():
        raise SystemExit(f"{rel} is not in the dump")
    m = conn.execute("""SELECT m.album_key, m.files, i.album_id, i.library_dir FROM matches m
        JOIN imports i ON i.match_id = m.id AND i.status = 'imported' AND i.mode = 'apply'
        WHERE EXISTS (SELECT 1 FROM json_each(m.files) WHERE value = ?)""", (rel,)).fetchone()
    if m is None:
        raise SystemExit(f"{rel} belongs to no imported album")
    placed = current_paths(conn, json.loads(m["files"]))
    album_dir = Path(os.path.dirname(next(iter(placed.values())))) if placed else Path(m["library_dir"])
    lib, info, tr = None, None, None
    if track is not None:
        from beets import metadata_plugins
        info = metadata_plugins.album_for_id(m["album_id"])
        tr = next((t for t in info.tracks if t.index == track), None) if info else None
        if tr is None:
            raise SystemExit(f"release {m['album_id']} has no track {track}")
        lib = open_library()
        by_path = {os.fsdecode(i.path): i for i in lib.items()}
        held = [p for p in placed.values() if p in by_path and by_path[p].mb_trackid == tr.track_id]
        if held:
            raise SystemExit(f"track {track} '{tr.title}' is held by {Path(held[0]).name}")
    plan_ = {"album_key": m["album_key"], "file": rel, "into": str(album_dir),
             "track": f"{tr.index} {tr.title}" if tr else None}
    if not apply:
        return {"dry_run": True, **plan_}
    (_, dest), = place_extras(album_dir, [(source / rel, rel)], move=False)
    action, why = "import_extra", f"{reason}: restored from the dump as an extra, tags untouched"
    if tr is not None:
        from beets import autotag
        from beets.library import Item

        album_id = next((by_path[p].album_id for p in placed.values() if p in by_path), None)
        item = Item.from_path(dest)
        item.album_id = album_id
        lib.add(item)
        autotag.apply_metadata(info, [(item, tr)])
        item.try_write()
        item.move()
        item.store()
        dest, action = os.fsdecode(item.path), "import"
        why = f"{reason}: restored from the dump onto track {tr.index} '{tr.title}' (user's decision)"
    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (now(), action, rel, dest, why, decided_by))
    conn.commit()
    return {"restored": dest, **plan_}


def remove_from_library(conn: sqlite3.Connection, dest: str, reason: str, decided_by: str) -> dict:
    """Remove one library file (beets DB and disk), logged. Never touches the dump."""
    import beets

    library_dir = Path(beets.config["directory"].as_filename()).resolve()
    path = Path(dest).resolve()
    if not path.is_relative_to(library_dir):
        raise SystemExit(f"{dest} is not inside the library ({library_dir})")
    if not path.exists():
        raise SystemExit(f"{dest} does not exist")
    src = next(((s,) for s, d in current_paths(conn).items() if d == str(path)), None)
    lib = open_library()
    item = next((i for i in lib.items() if os.fsdecode(i.path) == str(path)), None)
    if item is not None:
        item.remove(delete=True)
    else:
        path.unlink()
    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                 "VALUES (?, 'remove_from_library', ?, ?, ?, ?)",
                 (now(), src[0] if src else None, str(path), reason, decided_by))
    conn.commit()
    return {"removed": str(path), "source": src[0] if src else None, "in_beets_db": item is not None}
