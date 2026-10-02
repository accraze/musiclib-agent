"""M5: ingest new folders from the inbox (SPEC Workflow 2, D27-D32).

  list    folders waiting in inbox_dir, and batches already claimed
  claim   rename a folder to inbox/.processed/<date>/<folder> (D28); dry run without apply.
          From then on the folder is read-only, like the dump (D27), and its files are
          recorded with absolute paths (D29).
  scan    inventory + AcoustID + verify, on the batch's files only

Later steps (dedupe against the library, match, import) reuse the dump pipeline.
"""

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import acoustid, inventory, verify
from .dupes import _folder
from .config import Config
from .scan import AUDIO_EXTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_batches (
    id          INTEGER PRIMARY KEY,
    folder      TEXT NOT NULL,          -- name as dropped into the inbox
    path        TEXT NOT NULL UNIQUE,   -- absolute, under inbox/.processed/<date>/ (D28)
    status      TEXT NOT NULL,          -- claimed | scanned | deduped | matched | imported
    claimed_at  TEXT NOT NULL,
    claimed_by  TEXT NOT NULL,
    note        TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _inbox(cfg: Config) -> Path:
    if cfg.inbox_dir is None:
        raise SystemExit("inbox_dir is not set in musiclib.toml")
    return cfg.inbox_dir


def _tally(folder: Path) -> dict:
    audio = size = other = 0
    for dirpath, _, names in os.walk(folder):
        for n in names:
            p = Path(dirpath) / n
            if p.is_symlink() or not p.is_file():
                continue
            size += p.stat().st_size
            if p.suffix[1:].lower() in AUDIO_EXTS:
                audio += 1
            else:
                other += 1
    return {"audio_files": audio, "other_files": other, "bytes": size}


def listing(conn: sqlite3.Connection, cfg: Config) -> dict:
    migrate(conn)
    inbox = _inbox(cfg)
    waiting, ignored = [], []
    if inbox.is_dir():
        for p in sorted(inbox.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_symlink() or not p.is_dir():
                ignored.append({"name": p.name, "why": "not a folder: put loose files in a folder"})
                continue
            waiting.append({"folder": p.name, **_tally(p)})
    batches = [dict(r) for r in conn.execute(
        "SELECT id, folder, path, status, claimed_at, note FROM ingest_batches ORDER BY id DESC")]
    return {"inbox": str(inbox), "exists": inbox.is_dir(), "waiting": waiting,
            "ignored": ignored, "batches": batches}


def _target(cfg: Config, folder: str, day: str) -> Path:
    """inbox/.processed/<day>/<folder>, with ' (2)' etc. if that name is taken."""
    base = cfg.processed_dir / day / folder
    dest, n = base, 2
    while dest.exists():
        dest = base.with_name(f"{folder} ({n})")
        n += 1
    return dest


def claim(conn: sqlite3.Connection, cfg: Config, folder: str, *, apply: bool = False,
          decided_by: str = "user", day: str | None = None) -> dict:
    """D28: move a dropped folder into .processed/<date>/ before anything else touches it.
    A rename inside the inbox: same filesystem, instant, reversible. Logged."""
    migrate(conn)
    inbox = _inbox(cfg)
    src = inbox / folder
    if "/" in folder or folder.startswith(".") or not folder:
        raise SystemExit(f"not an inbox folder name: {folder!r}")
    if src.is_symlink() or not src.is_dir():
        raise SystemExit(f"no such folder in the inbox: {src}")
    dest = _target(cfg, folder, day or date.today().isoformat())
    plan = {"folder": folder, "from": str(src), "to": str(dest), **_tally(src)}
    if not plan["audio_files"]:
        raise SystemExit(f"{src} has no audio files")
    if not apply:
        return {"dry_run": True, **plan}
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)  # never a copy+delete: fails across filesystems instead
    with conn:
        bid = conn.execute(
            "INSERT INTO ingest_batches (folder, path, status, claimed_at, claimed_by) "
            "VALUES (?, ?, 'claimed', ?, ?)", (folder, str(dest), now(), decided_by)).lastrowid
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'ingest_claim', ?, ?, ?, ?)",
                     (now(), str(src), str(dest), f"claimed as ingest batch {bid} (D28)", decided_by))
    return {"batch": bid, **plan}


def batch(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row:
    """A batch by id, or by folder name when that is unambiguous."""
    migrate(conn)
    rows = conn.execute("SELECT * FROM ingest_batches WHERE id = ? OR folder = ?",
                        (ref if str(ref).isdigit() else -1, str(ref))).fetchall()
    if not rows:
        raise SystemExit(f"no ingest batch {ref!r}")
    if len(rows) > 1:
        raise SystemExit(f"{ref!r} names several batches: use the id ({[r['id'] for r in rows]})")
    return rows[0]


def _set_status(conn: sqlite3.Connection, bid: int, status: str, note: str | None = None) -> None:
    conn.execute("UPDATE ingest_batches SET status = ?, note = ? WHERE id = ?", (status, note, bid))
    conn.commit()


def scan(conn: sqlite3.Connection, cfg: Config, ref: str | int, *, workers: int,
         lookup=acoustid.run, progress=sys.stderr) -> dict:
    """Inventory, AcoustID lookups and verify verdicts for one batch. Read-only on files."""
    b = batch(conn, ref)
    root = Path(b["path"])
    if not root.is_dir():
        raise SystemExit(f"batch folder is missing: {root}")
    inv = inventory.scan_root(conn, root, workers=workers, progress=progress)
    looked = lookup(conn, cfg.acoustid_key, progress=progress)
    verdicts = verify.run_under(conn, str(root))
    errors = [dict(r) for r in conn.execute(
        "SELECT path, error FROM files WHERE top_dir = ? AND error IS NOT NULL", (str(root),))]
    _set_status(conn, b["id"], "scanned")
    return {"batch": b["id"], "folder": b["folder"], "path": str(root),
            "audio_files": inv["audio_files"], "scanned": inv["scanned"], "scan_errors": errors,
            "acoustid": {k: v for k, v in looked.items() if k != "elapsed_s"}, "verify": verdicts}


# --- Dedupe (D30) ------------------------------------------------------------------------

DUPES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_dupes (
    batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    path      TEXT NOT NULL,     -- batch folder (ends in /) or file
    scope     TEXT NOT NULL,     -- folder | file
    outcome   TEXT NOT NULL,     -- skip (not imported) | review | flag (report only)
    kind      TEXT NOT NULL,     -- identical | duplicate | upgrade | extra_tracks | edition
                                 -- | in_batch | in_batch_review | dump_overlap
    other     TEXT,              -- the copy it was compared with (library dir, batch keeper, dump folder)
    coverage  REAL,              -- share of this folder's tracks found in `other`
    reason    TEXT NOT NULL,
    stats     TEXT               -- JSON: this folder vs other
);
CREATE INDEX IF NOT EXISTS ingest_dupes_batch ON ingest_dupes(batch_id);
"""


def _library_rows(conn: sqlite3.Connection) -> list[dict]:
    """One row per library file, from its original (dump or inbox) file: fingerprint identity,
    quality and hash of the original, `path` = where it is in the library now, `album` = the
    release it was imported as. Library copies carry beets' tags, so their own bytes differ."""
    from .dupes import FILE_ROWS
    from .importer import current_paths, migrate as importer_migrate

    importer_migrate(conn)
    placed = current_paths(conn)
    if not placed:
        return []
    released = {}
    for r in conn.execute("""SELECT m.files, i.album_id FROM imports i JOIN matches m ON m.id = i.match_id
                             WHERE i.status = 'imported' AND i.album_id IS NOT NULL"""):
        for f in json.loads(r["files"]):
            released[f] = r["album_id"]
    rows = conn.execute(FILE_ROWS.format(where="f.path IN (SELECT value FROM json_each(?))"),
                        (json.dumps(list(placed)),)).fetchall()
    return [{**dict(r), "origin": r["path"], "path": placed[r["path"]],
             "album": released.get(r["path"], r["album"])} for r in rows]


REFINE_SHARE = 0.5  # folders sharing this much by AcoustID get their other tracks fingerprint-compared
# Same recording across masters, for album-level dedupe (D34). Measured on Ramones "Leave Home"
# (original MP3 vs 2017 remaster FLAC): same song 0.85-0.96, different songs 0.48-0.55.
# Stricter than D20's COPY_SIMILARITY (0.9), which must keep a different master inside an album.
SAME_RECORDING = 0.75


def _by_folder(rows) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(_folder(r["path"]), []).append(r)
    return out


def _fingerprint_matches(mine: list, theirs: list, known: set[str]) -> set[str]:
    """My track keys not already shared by AcoustID whose audio matches one of their unshared
    tracks by direct fingerprint comparison: AcoustID splits some remasters and encodes of
    one recording into different ids."""
    from .dupes import track_key
    from .fpsim import similarity

    theirs = [r for r in theirs if r["fingerprint"] and track_key(r) not in known]
    found = set()
    for a in mine:
        k = track_key(a)
        if k in known or k in found or not a["fingerprint"]:
            continue
        for b in theirs:
            if (a["duration"] is not None and b["duration"] is not None
                    and abs(a["duration"] - b["duration"]) <= 10
                    and similarity(a["fingerprint"], b["fingerprint"]) >= SAME_RECORDING):
                found.add(k)
                break
    return found


def _overlaps(mine: dict, theirs: dict, min_share: float, mine_rows, their_rows) -> list[tuple]:
    """(my folder, their folder, my keys found in theirs, share of mine, share of theirs) where
    either side holds at least `min_share` of the other's tracks (D14). Found = same AcoustID,
    or for folders already sharing REFINE_SHARE, the same audio by fingerprint."""
    index: dict[str, set] = {}
    for f in theirs.values():
        for k in f.keys:
            index.setdefault(k, set()).add(f.path)
    mine_files, their_files = _by_folder(mine_rows), None
    out = []
    for f in mine.values():
        counts: dict[str, int] = {}
        for k in f.keys:
            for p in index.get(k, ()):
                counts[p] = counts.get(p, 0) + 1
        for p, n in counts.items():
            t = theirs[p]
            same = f.keys & t.keys
            if REFINE_SHARE <= max(n / len(f.keys), n / len(t.keys)) < min_share:
                their_files = their_files or _by_folder(their_rows)
                same = same | _fingerprint_matches(mine_files.get(f.path, []), their_files.get(p, []), same)
            n = len(same)
            if n / len(f.keys) >= min_share or n / len(t.keys) >= min_share:
                out.append((f, t, same, n / len(f.keys), n / len(t.keys)))
    return out


def _quality(f) -> int:
    from .dupes import quality_tier
    n = f.files or 1
    return quality_tier(f.lossless / n > 0.5, f.bitrate_sum / n)


def dedupe(conn: sqlite3.Connection, ref: str | int) -> dict:
    """D30: duplicates inside the batch and against the library. Proposals in ingest_dupes;
    nothing moves. skip = not imported (the manifest names the kept copy), review = asked,
    flag = only reported (overlap with dump albums that aren't in the library, Q4)."""
    from . import dupes

    b = batch(conn, ref)
    if b["status"] == "claimed":
        raise SystemExit(f"batch {b['id']} is not scanned yet: run ingest scan first")
    conn.executescript(DUPES_SCHEMA)
    root = b["path"]
    mine_rows, mine = dupes.folders_of(conn.execute(
        dupes.FILE_ROWS.format(where="f.top_dir = ?"), (root,)).fetchall())
    out: list[tuple] = []  # (path, scope, outcome, kind, other, coverage, reason, stats)

    # 1. Inside the batch: the dump's own rules (tiers 1-3, D14, D19).
    gone: set[str] = set()  # batch folders/files already decided
    for tier, scope, action, reason, keeper, _, _, members in dupes.group(mine_rows, mine):
        for path, role, cov, st in members:
            if role == "drop":
                kind = "in_batch" if action == "auto" else "in_batch_review"
                out.append((path, scope, "skip" if action == "auto" else "review", kind, keeper,
                            cov, f"tier {tier}: {reason}", json.dumps(st) if st else None))
                if action == "auto":
                    gone.add(path)

    # 2. Against the library, by the originals' identity.
    lib_rows = _library_rows(conn)
    _, library = dupes.folders_of(lib_rows)
    lib_sha = {r["sha256"]: r["path"] for r in lib_rows if r["sha256"]}
    decided = set(gone)
    for f, lib, same, mine_share, lib_share in sorted(
            _overlaps({p: f for p, f in mine.items() if p not in gone}, library, dupes.CONTAINMENT,
                      mine_rows, lib_rows),
            key=lambda o: (-o[3], o[1].path)):
        if f.path in decided:
            continue  # compared with its best-covering library album already
        decided.add(f.path)
        stats = json.dumps({"ingest": f.stats(), "library": lib.stats()})
        extra = len(f.keys - same)
        if f.release and lib.release and f.release != lib.release:
            o = ("review", "edition", f"tagged as another release than the library copy "
                 f"({f.release} vs {lib.release}): edition?")
        elif _quality(f) > _quality(lib):
            o = ("review", "upgrade", "better audio than the library copy: replace it? (D32)")
        elif extra:
            o = ("review", "extra_tracks", f"{extra} track(s) the library copy lacks")
        else:
            o = ("skip", "duplicate", "already in the library at equal or better quality")
        out.append((f.path, "folder", *o[:2], lib.path, round(mine_share, 3), o[2], stats))

    # Identical bytes to a library original, in folders not otherwise matched.
    for r in mine_rows:
        folder = dupes._folder(r["path"])
        if folder not in decided and r["path"] not in gone and r["sha256"] in lib_sha:
            out.append((r["path"], "file", "skip", "identical", lib_sha[r["sha256"]], 1.0,
                        "identical bytes to a library file's original", None))

    # 3. Q4: overlap with dump albums that never reached the library (review, dropped): flag only.
    # Whole folders only: leftovers of an imported folder (a skipped extra) aren't "not imported".
    placed = {dupes._folder(r["origin"]) for r in lib_rows}
    dump_rows, dump = dupes.folders_of([r for r in conn.execute(
        dupes.FILE_ROWS.format(where="substr(f.path, 1, 1) != '/'")).fetchall()
        if dupes._folder(r["path"]) not in placed])
    flagged = set()
    for f, d, _, mine_share, _ in _overlaps(
            {p: f for p, f in mine.items() if p not in gone}, dump, dupes.CONTAINMENT, mine_rows, dump_rows):
        if (f.path, d.path) not in flagged:
            flagged.add((f.path, d.path))
            out.append((f.path, "folder", "flag", "dump_overlap", d.path, round(mine_share, 3),
                        "also in the dump, not in the library (review queue or dropped copy)", None))

    with conn:
        conn.execute("DELETE FROM ingest_dupes WHERE batch_id = ?", (b["id"],))
        conn.executemany("INSERT INTO ingest_dupes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(b["id"], *o) for o in out])
        conn.execute("UPDATE ingest_batches SET status = 'deduped' WHERE id = ?", (b["id"],))
    return dupes_summary(conn, b["id"])


def dupes_summary(conn: sqlite3.Connection, bid: int) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT path, scope, outcome, kind, other, coverage, reason FROM ingest_dupes "
        "WHERE batch_id = ? ORDER BY outcome, kind, path", (bid,))]
    counts: dict[str, int] = {}
    for r in rows:
        counts[f"{r['outcome']}:{r['kind']}"] = counts.get(f"{r['outcome']}:{r['kind']}", 0) + 1
    return {"batch": bid, "counts": counts, "items": rows}


# --- Match and import (D16 two steps, D31 shared review queue) ----------------------------

def _skipped(conn: sqlite3.Connection, bid: int) -> tuple[set[str], list[str]]:
    """Batch files and folders that dedupe says not to import."""
    files, folders = set(), []
    for r in conn.execute("SELECT path, scope FROM ingest_dupes WHERE batch_id = ? AND outcome = 'skip'", (bid,)):
        (folders.append if r["scope"] == "folder" else files.add)(r["path"])
    return files, folders


def albums(conn: sqlite3.Connection, root: str, bid: int):
    """(album_key, dirs, files) for the batch, as beets groups them (multi-disc folders
    become one album), minus dedupe skips. Keys and paths are absolute (D29)."""
    from beets.importer.tasks import albums_in_dir

    audio = {r[0] for r in conn.execute("SELECT path FROM files WHERE top_dir = ?", (root,))}
    skip_files, skip_folders = _skipped(conn, bid)
    for dirs, paths in albums_in_dir(os.fsencode(root)):
        files = sorted(f for f in map(os.fsdecode, paths) if f in audio and f not in skip_files
                       and _folder(f) not in skip_folders)  # not subfolders: they're other copies
        if files:
            ds = [os.fsdecode(d).rstrip("/") + "/" for d in dirs]
            yield ds[0], ds, files


def _dupe_notes(conn: sqlite3.Connection, bid: int, dirs: list[str]) -> tuple[str | None, str | None]:
    """(review note, flag note) from ingest_dupes rows on these folders."""
    review, flags = [], []
    for r in conn.execute("SELECT * FROM ingest_dupes WHERE batch_id = ? AND outcome != 'skip'", (bid,)):
        if any(r["path"].startswith(d) for d in dirs):
            (review if r["outcome"] == "review" else flags).append(f"{r['kind']} vs {r['other']}")
    # "duplicate review" puts the album in the review queue's `dupe` kind (review.IN_DUPE).
    return (("ingest duplicate review: " + "; ".join(review)) if review else None,
            ("ingest flag: " + "; ".join(flags)) if flags else None)


def match(conn: sqlite3.Connection, ref: str | int, *, rematch: bool = False,
          matcher=None, progress=sys.stderr) -> dict:
    """Dry-run match each batch album on MusicBrainz (~5 s/album). Read-only on files.
    Rows go to `matches` with batch_id, so /review sees them (D31)."""
    from .importer import migrate as importer_migrate
    from .match import COLUMNS, match_album
    from .review import _imported_releases

    matcher = matcher or match_album
    importer_migrate(conn)
    b = batch(conn, ref)
    if b["status"] in ("claimed", "scanned"):
        raise SystemExit(f"batch {b['id']} is not deduped yet: run ingest dedupe first")
    root = b["path"]
    done = set() if rematch else {r[0] for r in conn.execute(
        "SELECT album_key FROM matches WHERE batch_id = ?", (b["id"],))}
    todo = [a for a in albums(conn, root, b["id"]) if a[0] not in done]
    print(f"ingest match: {len(todo)} albums in batch {b['id']}", file=progress, flush=True)
    counts: dict[str, int] = {}
    cols = COLUMNS + ["batch_id"]
    in_library = _imported_releases(conn)
    for key, dirs, files in todo:
        row = {"album_key": key, "dirs": json.dumps(dirs), "files": json.dumps(files),
               "search_id": None, "matched_at": now(), "batch_id": b["id"]}
        try:
            row.update(matcher(Path(root), files, None))
        except Exception as e:  # unreadable file, network trouble: record and move on
            row.update(action="error", note=f"{type(e).__name__}: {e}"[:300])
        review, flag = _dupe_notes(conn, b["id"], dirs)
        if row.get("album_id") in in_library:  # D30 backstop when audio ids missed the copy
            review = "; ".join(filter(None, (review, f"ingest duplicate review: release already in the "
                                                     f"library (from {in_library[row['album_id']]})")))
        if review and row["action"] in ("auto", "unsorted"):
            row["action"] = "review"
        row["note"] = "; ".join(n for n in (row.get("note"), review, flag) if n) or None
        conn.execute(f"INSERT OR REPLACE INTO matches ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row.get(c) for c in cols])
        conn.commit()
        counts[row["action"]] = counts.get(row["action"], 0) + 1
        print(f"  {row['action']}: {key}", file=progress, flush=True)
    _set_status(conn, b["id"], "matched")
    return {"batch": b["id"], "matched": len(todo), **counts}


def _keys(conn: sqlite3.Connection, bid: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT album_key FROM matches WHERE batch_id = ?", (bid,))]


def _waiting(conn: sqlite3.Connection, bid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT m.album_key, m.action, m.distance, m.note FROM matches m
        LEFT JOIN imports i ON i.match_id = m.id AND i.status = 'imported'
        WHERE m.batch_id = ? AND i.match_id IS NULL AND m.decision IS NULL
          AND m.action IN ('review', 'error') ORDER BY m.album_key""", (bid,))]


def import_batch(conn: sqlite3.Connection, cfg: Config, ref: str | int, *, apply: bool = False,
                 progress=sys.stderr) -> dict:
    """Import the batch albums that need no review: strong matches (D15) and albums with no
    candidate at all (D13, Unsorted/). Reviewed albums come in through /review as usual.
    Callers must have run beetsenv.setup()."""
    from . import importer

    importer.migrate(conn)
    b = batch(conn, ref)
    if b["status"] in ("claimed", "scanned", "deduped"):
        raise SystemExit(f"batch {b['id']} is not matched yet: run ingest match first")
    keys, root = _keys(conn, b["id"]), Path(b["path"])
    if not apply:
        return {"batch": b["id"], "dry_run": True,
                **{w: importer.plan(conn, root, w, only=keys) for w in ("auto", "unsorted")},
                "waiting_for_review": _waiting(conn, b["id"])}
    results = {}
    with importer.library_lock(cfg.state_dir):
        for which in ("auto", "unsorted"):
            if importer.select(conn, which, albums=keys):
                results[which] = importer.run(conn, root, cfg.state_dir / "staging", which,
                                              only=keys, progress=progress)
    _set_status(conn, b["id"], "imported")
    refresh_status(conn, b["id"])
    return {"batch": b["id"], **results, "waiting_for_review": _waiting(conn, b["id"])}


def refresh_status(conn: sqlite3.Connection, bid: int) -> None:
    """Called after any import or review decision on a batch album, whichever command made it
    (ingest import, /review's import --which approved, review decide): a matched batch with
    nothing left to import becomes 'imported'; otherwise the note says what is left."""
    b = batch(conn, bid)
    if b["status"] not in ("matched", "imported"):
        return
    left = conn.execute("""
        SELECT COUNT(*) FROM matches m LEFT JOIN imports i ON i.match_id = m.id AND i.status = 'imported'
        WHERE m.batch_id = ? AND i.match_id IS NULL AND COALESCE(m.decision, '') != 'skip'""", (bid,)).fetchone()[0]
    if not left:
        _set_status(conn, bid, "imported")
    elif b["status"] == "imported":
        _set_status(conn, bid, "imported", f"{left} album(s) not imported yet (review or pending)")


def upgrades(conn: sqlite3.Connection, cfg: Config, ref: str | int, *, apply: bool = False,
             decided_by: str = "user") -> dict:
    """D32: once an approved upgrade is imported, take the old library copy out (logged).
    Only files placed from other originals are removed, never this batch's own."""
    from . import importer

    importer.migrate(conn)
    b = batch(conn, ref)
    root = b["path"].rstrip("/") + "/"
    placed = importer.current_paths(conn)
    todo = []
    for u in conn.execute("SELECT path, other FROM ingest_dupes WHERE batch_id = ? AND kind = 'upgrade'",
                          (b["id"],)):
        m = conn.execute("""
            SELECT m.album_key, m.decision, i.status, i.library_dir FROM matches m
            LEFT JOIN imports i ON i.match_id = m.id
            WHERE m.batch_id = ? AND EXISTS (SELECT 1 FROM json_each(m.dirs) d
                                             WHERE substr(?, 1, length(d.value)) = d.value)""",
                         (b["id"], u["path"])).fetchone()
        if m is None or m["status"] != "imported" or m["decision"] != "approve":
            continue  # not approved and imported (yet): the old copy stays
        old = sorted(d for s, d in placed.items() if d.startswith(u["other"]) and not s.startswith(root))
        if old:
            todo.append({"album": m["album_key"], "new_dir": m["library_dir"], "old_dir": u["other"],
                         "remove": old})
    if not apply:
        return {"batch": b["id"], "dry_run": True, "upgrades": todo}
    removed = 0
    with importer.library_lock(cfg.state_dir):
        for t in todo:
            for dest in t["remove"]:
                importer.remove_from_library(
                    conn, dest, f"D32: replaced by the better copy from {t['album']} (now in {t['new_dir']})",
                    decided_by)
                removed += 1
    return {"batch": b["id"], "upgrades": len(todo), "removed": removed}


# --- Manifest (D18 for a batch) -----------------------------------------------------------

def manifest(conn: sqlite3.Connection, cfg: Config, ref: str | int) -> dict:
    """Every batch file with where it went or why not; written to state/reports/."""
    from .importer import migrate as importer_migrate

    importer_migrate(conn)
    conn.executescript(DUPES_SCHEMA)
    b = batch(conn, ref)
    root = b["path"]
    files = [r[0] for r in conn.execute("SELECT path FROM files WHERE top_dir = ? ORDER BY path", (root,))]
    status: dict[str, tuple[str, str | None]] = {}
    for r in conn.execute("SELECT id, files, action, note, decision FROM matches WHERE batch_id = ?", (b["id"],)):
        imported = conn.execute("SELECT 1 FROM imports WHERE match_id = ? AND status = 'imported'",
                                (r["id"],)).fetchone()
        if imported:
            kind = "imported"
        elif r["decision"]:
            kind = f"decided:{r['decision']}"  # approved/asis, not imported yet; or skip
        else:
            kind = {"auto": "pending", "unsorted": "pending"}.get(r["action"], r["action"])
        for f in json.loads(r["files"]):
            status[f] = (kind, r["note"])
    for r in conn.execute("SELECT path, scope, other, reason FROM ingest_dupes "
                          "WHERE batch_id = ? AND outcome = 'skip'", (b["id"],)):
        hit = [r["path"]] if r["scope"] == "file" else [f for f in files if _folder(f) == r["path"]]
        for f in hit:
            status[f] = ("duplicate", f"{r['reason']}; kept {r['other']}")
    for r in conn.execute("SELECT action, source_path, dest_path, reason FROM audit_log WHERE source_path IN "
                          "(SELECT value FROM json_each(?)) ORDER BY id", (json.dumps(files),)):
        if r["action"].startswith("import") or r["action"] == "retag_by_fingerprint":
            status[r["source_path"]] = ("imported", r["dest_path"])
        elif r["action"] == "skip_extra":
            status[r["source_path"]] = ("skipped", r["reason"])
        elif r["action"] == "remove_from_library":
            status[r["source_path"]] = ("removed", r["reason"])
    rows = [{"path": f, "status": status.get(f, ("unmatched", None))[0],
             "detail": status.get(f, ("unmatched", None))[1]} for f in files]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.reports_dir / f"ingest-{b['id']}-{date.today().isoformat()}.json"
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    return {"batch": b["id"], "folder": b["folder"], "path": root, "status": b["status"],
            "files": len(rows), "by_status": dict(sorted(counts.items())),
            "flags": [dict(r) for r in conn.execute(
                "SELECT path, other, reason FROM ingest_dupes WHERE batch_id = ? AND outcome = 'flag'", (b["id"],))],
            "written": str(out)}
