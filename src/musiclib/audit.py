"""M6 periodic audit (D39). Read-only on the library, the dump and the inbox archive.

  check    fast checks: beets, disk and audit log agree; library files unchanged since
           beets wrote them; dump and inbox archive unchanged since inventory; incomplete
           albums; missing art. Folds in the decode and mbsync findings so far.
  decode   incremental: decode library files not checked yet or changed since (beet bad).
  mbsync   incremental: what MusicBrainz would change per album, stored, never applied.
  apply    mbsync on albums the user approved through /audit; logged (the only writer).
  relink   log library files beets moved without an audit row (D25 resync before D39).

Results live in audit_decode / audit_mbsync so long checks run in pieces (--limit).
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .importer import current_paths, placements

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_decode (
    path        TEXT PRIMARY KEY,      -- library file
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    expected    REAL,                  -- stated length (beets, else the original's inventory row)
    decoded     REAL NOT NULL,         -- seconds ffmpeg could decode
    errors      INTEGER NOT NULL,      -- ffmpeg error lines (corrupt frames)
    checked_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_mbsync (
    album_id    INTEGER PRIMARY KEY,   -- beets album id
    mb_albumid  TEXT NOT NULL,
    album       TEXT NOT NULL,         -- "albumartist - album", for people
    album_dir   TEXT,
    status      TEXT NOT NULL,         -- same | changed | not_found | applied
    changes     TEXT,                  -- JSON: album fields, per-item fields, moves
    checked_at  TEXT NOT NULL,
    applied_at  TEXT
);
-- Each library file's size and mtime as the last check saw it, so a later change shows.
CREATE TABLE IF NOT EXISTS audit_files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    seen_at     TEXT NOT NULL
);
"""

AUDIO_EXTS = {"mp3", "flac", "m4a", "ogg", "opus", "wav", "aiff", "aif", "wma", "ape", "aac", "wv",
              "mpc", "mp4", "m4v", "mkv", "webm", "mov", "avi"}
# beets records mtime, then embedart rewrites the file: ~1 s per file, up to ~40 s for a
# long album. A file first seen counts as modified only well past that; after that, any
# change from the baseline does, unless beets wrote it (its mtime moved along).
MTIME_SLACK = 5.0
FIRST_SLACK = 300.0
DAMAGED = 0.9          # decoded less than this share of the stated length
EXAMPLES = 25
SKIP_FIELDS = {"mtime", "path", "added", "id", "album_id"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def library_audio(library_dir: Path) -> set[str]:
    out = set()
    for dirpath, _, names in os.walk(library_dir):
        for n in names:
            if n.rsplit(".", 1)[-1].lower() in AUDIO_EXTS:
                out.add(os.path.join(dirpath, n))
    return out


def _cap(rows: list, n: int = EXAMPLES) -> dict:
    return {"count": len(rows), "examples": rows[:n]}


# --- integrity ------------------------------------------------------------------------------

def relink_pairs(stale: list[str], unlogged: list[str]) -> dict[str, str]:
    """Logged destinations no longer on disk -> the unlogged library file that is the same
    file moved by beets: same file name and album folder name, only the artist folder renamed
    (D25). A second pass ignores a trailing %aunique suffix ("[1989]"), which can move from one
    edition to another when the artist changes. Ambiguous names are left out."""
    def exact(p):
        return Path(p).parent.name, Path(p).name

    def no_suffix(p):
        return re.sub(r" \[[^\]]*\]$", "", Path(p).parent.name), Path(p).name

    pairs = {}
    for key in (exact, no_suffix):
        by_new, by_old = defaultdict(list), defaultdict(list)
        for p in unlogged:
            if p not in pairs.values():
                by_new[key(p)].append(p)
        for p in stale:
            if p not in pairs:
                by_old[key(p)].append(p)
        pairs |= {olds[0]: by_new[k][0] for k, olds in by_old.items() if len(olds) == 1 and len(by_new[k]) == 1}
    return pairs


def modified_files(conn: sqlite3.Connection, items: dict, on_disk: set[str], *, accept: bool = False) -> list[dict]:
    """Library files changed outside musiclib/beets since the last check. Flagged files keep
    their old baseline, so they are reported until `accept` takes them as they are now."""
    baseline = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, size, mtime FROM audit_files")}
    out, keep = [], []
    for p in sorted(on_disk):
        st = os.stat(p)
        item = items.get(p)
        drift = st.st_mtime - item.mtime if item is not None and item.mtime else None
        if p not in baseline:
            flagged = drift is not None and abs(drift) > FIRST_SLACK
        elif baseline[p] == (st.st_size, st.st_mtime):
            flagged = False
        else:
            flagged = drift is None or abs(drift) > MTIME_SLACK
        if flagged:
            out.append({"path": p, "seconds_after_beets": round(drift) if drift is not None else None})
        if flagged and not accept:  # keep the old baseline (or beets' view) so it shows again
            keep.append((p, *baseline.get(p, (st.st_size, item.mtime if item is not None else 0.0))))
        else:
            keep.append((p, st.st_size, st.st_mtime))
    conn.execute("DELETE FROM audit_files")  # files gone from the library drop out
    conn.executemany("INSERT INTO audit_files VALUES (?, ?, ?, ?)", [(*k, now()) for k in keep])
    conn.commit()
    return out


def integrity(conn: sqlite3.Connection, lib, library_dir: Path, *, accept_modified: bool = False) -> dict:
    on_disk = library_audio(library_dir)
    items = {os.fsdecode(i.path): i for i in lib.items()}
    placed = {d: s for s, d in current_paths(conn).items()}

    known = {r[0] for r in conn.execute("SELECT path FROM files")}
    gone = sorted(p for p in placed if p not in on_disk)
    stale = [p for p in gone if placed[p] in known]
    # rows whose source is no inventoried file (an album key, from the 2026-09-29 retag sweep):
    # they place nothing real, so they're listed apart and never relinked
    orphans = [{"dest": p, "source": placed[p]} for p in gone if placed[p] not in known]
    unlogged = sorted(p for p in items if p not in placed and p in on_disk)
    modified = modified_files(conn, items, on_disk, accept=accept_modified)
    pairs = relink_pairs(stale, unlogged)
    return {
        "library_files": len(on_disk),
        "beets_items": len(items),
        "logged_files": len(placed),
        # beets lists a file that isn't there
        "beets_missing": _cap(sorted(p for p in items if p not in on_disk)),
        # the audit log places a file that isn't there (moved without a row, or deleted)
        "logged_missing": _cap(stale),
        "orphan_rows": _cap(orphans),
        # on disk, but neither beets nor the audit log knows it
        "untracked": _cap(sorted(p for p in on_disk if p not in items and p not in placed)),
        # beets knows it, the audit log doesn't (moved by beets without a row)
        "unlogged": _cap(unlogged),
        "relinkable": len(pairs),
        # changed on disk after beets last wrote it (another tagger, or damage)
        "modified": _cap(modified),
    }


def originals(conn: sqlite3.Connection, source_dir: Path) -> dict:
    """Dump and inbox-archive files still as inventoried (safety rule 1, D27). Batches pruned
    under D43 have no originals left, so they're not expected."""
    from .ingest import pruned_roots

    roots = tuple(pruned_roots(conn))
    missing, changed, checked = [], [], 0
    for row in conn.execute("SELECT path, size, mtime FROM files"):
        if roots and row["path"].startswith(roots):
            continue
        p = Path(row["path"]) if row["path"].startswith("/") else source_dir / row["path"]
        checked += 1
        try:
            st = p.stat()
        except FileNotFoundError:
            missing.append(row["path"])
            continue
        if st.st_size != row["size"] or abs(st.st_mtime - row["mtime"]) > 0.001:
            changed.append(row["path"])
    return {"checked": checked, "missing": _cap(missing), "changed": _cap(changed)}


def incomplete(lib) -> list[dict]:
    out = []
    for album in lib.albums():
        items = list(album.items())
        total = album.albumtotal or 0
        discs = {i.disc for i in items}
        missing_discs = max((i.disctotal or 0) for i in items) - len(discs) if items else 0
        if len(items) < total or missing_discs > 0:
            out.append({"album_id": album.id, "album": f"{album.albumartist} - {album.album}",
                        "tracks": len(items), "of": total, "missing_discs": max(missing_discs, 0)})
    return sorted(out, key=lambda r: (-(r["of"] - r["tracks"]), r["album"]))


def no_art(lib) -> list[dict]:
    out = []
    for album in lib.albums():
        if not album.artpath or not os.path.exists(os.fsdecode(album.artpath)):
            out.append({"album_id": album.id, "album": f"{album.albumartist} - {album.album}"})
    return sorted(out, key=lambda r: r["album"])


def unsorted_counts(library_dir: Path) -> dict:
    root = library_dir / "Unsorted"
    if not root.is_dir():
        return {"folders": 0, "files": 0}
    files = [p for p in library_audio(root)]
    return {"folders": len({str(Path(p).parent) for p in files}), "files": len(files)}


# --- decode (beet bad) ----------------------------------------------------------------------

def decode(path: str) -> tuple[float, int]:
    """Seconds ffmpeg decodes, and how many error lines it printed."""
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-stats", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True, errors="replace")
    lines = out.stderr.replace("\r", "\n").split("\n")
    times = [t for line in lines for t in line.split() if t.startswith("time=")]
    errors = sum(1 for line in lines if line.strip() and "time=" not in line)
    if not times:
        return 0.0, errors
    try:
        h, m, sec = times[-1][5:].split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec), errors
    except ValueError:
        return 0.0, errors


def _expected_lengths(conn: sqlite3.Connection, lib) -> dict[str, float]:
    durations = dict(conn.execute("SELECT path, duration FROM files"))
    out = {dest: durations.get(src) for src, dest in current_paths(conn).items()}
    for item in lib.items():
        out[os.fsdecode(item.path)] = item.length
    return out


def decode_pending(conn: sqlite3.Connection, library_dir: Path) -> list[tuple[str, int, float]]:
    done = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, size, mtime FROM audit_decode")}
    out = []
    for p in sorted(library_audio(library_dir)):
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if done.get(p) != (st.st_size, st.st_mtime):
            out.append((p, st.st_size, st.st_mtime))
    return out


def run_decode(conn: sqlite3.Connection, lib, library_dir: Path, *, limit: int | None = None,
               workers: int = 4, progress=sys.stderr) -> dict:
    migrate(conn)
    pending = decode_pending(conn, library_dir)
    todo = pending[:limit] if limit else pending
    expected = _expected_lengths(conn, lib)
    damaged = 0
    with ThreadPoolExecutor(workers) as pool:
        for n, ((path, size, mtime), (secs, errors)) in enumerate(
                zip(todo, pool.map(lambda t: decode(t[0]), todo)), 1):
            exp = expected.get(path)
            conn.execute("INSERT OR REPLACE INTO audit_decode VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (path, size, mtime, exp, secs, errors, now()))
            if exp and secs < DAMAGED * exp:
                damaged += 1
            if n % 50 == 0 or n == len(todo):
                conn.commit()
                print(f"decode {n}/{len(todo)} ({damaged} damaged)", file=progress, flush=True)
    conn.commit()
    return {"decoded": len(todo), "damaged_now": damaged, "still_pending": len(pending) - len(todo)}


def decode_findings(conn: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in conn.execute(
        """SELECT path, expected, decoded, errors FROM audit_decode
           WHERE expected > 0 AND decoded < ? * expected ORDER BY path""", (DAMAGED,))]
    noisy = conn.execute("SELECT COUNT(*) FROM audit_decode WHERE errors > 0").fetchone()[0]
    checked = conn.execute("SELECT COUNT(*) FROM audit_decode").fetchone()[0]
    gone = [r["path"] for r in rows if not os.path.exists(r["path"])]
    return {"checked": checked, "damaged": _cap([r for r in rows if r["path"] not in gone], 100),
            "decode_errors_but_full_length": noisy}


# --- mbsync (dry run) -----------------------------------------------------------------------

def _pairs(items, info):
    """beets mbsync's item -> release track mapping (release track id, else recording id)."""
    by_rt = {t.release_track_id: t for t in info.tracks}
    by_rec = defaultdict(list)
    for t in info.tracks:
        by_rec[t.track_id].append(t)
    pairs = []
    for item in items:
        if item.mb_releasetrackid in by_rt:
            pairs.append((item, by_rt[item.mb_releasetrackid]))
        else:
            cands = by_rec[item.mb_trackid]
            if len(cands) == 1:
                pairs.append((item, cands[0]))
            else:
                pairs += [(item, c) for c in cands if c.medium_index == item.track and c.medium == item.disc][:1]
    return pairs


def _jsonable(v):
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)


def album_diff(album, info) -> dict:
    """What mbsync would change on this album; nothing is stored."""
    from beets import autotag

    items = list(album.items())
    before = {i.id: dict(i) for i in items}
    paths = {i.id: os.fsdecode(i.path) for i in items}
    autotag.apply_metadata(info, _pairs(items, info))
    tracks, moves, fields = [], [], defaultdict(int)
    for item in items:
        ch = {k: [_jsonable(before[item.id].get(k)), _jsonable(item.get(k))]
              for k in item.keys() if k not in SKIP_FIELDS and before[item.id].get(k) != item.get(k)}
        if not ch:
            continue
        for k in ch:
            fields[k] += 1
        tracks.append({"path": paths[item.id], "changes": ch})
        dest = os.fsdecode(item.destination())
        if dest != paths[item.id]:
            moves.append({"from": paths[item.id], "to": dest})
    for item in items:  # leave nothing dirty behind
        item.load()
    return {"fields": dict(fields), "tracks": tracks, "moves": moves}


def run_mbsync(conn: sqlite3.Connection, lib, *, limit: int | None = None, progress=sys.stderr) -> dict:
    """Check albums never checked first, then the least recently checked. Network only."""
    from beets import metadata_plugins

    migrate(conn)
    seen = {r[0]: r[1] for r in conn.execute("SELECT album_id, checked_at FROM audit_mbsync")}
    albums = sorted((a for a in lib.albums() if a.mb_albumid), key=lambda a: (a.id in seen, seen.get(a.id, ""), a.id))
    albums = albums[:limit] if limit else albums
    counts = defaultdict(int)
    for n, album in enumerate(albums, 1):
        items = list(album.items())
        album_dir = os.path.dirname(os.fsdecode(items[0].path)) if items else None
        try:
            info = metadata_plugins.album_for_id(album.mb_albumid)
        except Exception as e:  # network or MusicBrainz trouble: try again next run
            print(f"mbsync {album.id}: {e}", file=progress)
            counts["error"] += 1
            continue
        if not info:
            status, diff = "not_found", None
        else:
            diff = album_diff(album, info)
            status = "changed" if diff["tracks"] else "same"
        counts[status] += 1
        conn.execute("""INSERT OR REPLACE INTO audit_mbsync
            (album_id, mb_albumid, album, album_dir, status, changes, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (album.id, album.mb_albumid, f"{album.albumartist} - {album.album}", album_dir, status,
                      json.dumps(diff, ensure_ascii=False) if diff else None, now()))
        conn.commit()
        if n % 25 == 0 or n == len(albums):
            print(f"mbsync {n}/{len(albums)} {dict(counts)}", file=progress, flush=True)
    total = sum(1 for a in lib.albums() if a.mb_albumid)
    checked = conn.execute("SELECT COUNT(*) FROM audit_mbsync").fetchone()[0]
    return {"checked_now": len(albums), **counts, "checked_total": checked, "albums_total": total}


def mbsync_findings(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    rows = conn.execute("""SELECT album_id, album, mb_albumid, album_dir, status, changes, checked_at
        FROM audit_mbsync WHERE status IN ('changed', 'not_found') ORDER BY album""").fetchall()
    changed = []
    for r in rows:
        d = json.loads(r["changes"]) if r["changes"] else {}
        changed.append({"album_id": r["album_id"], "album": r["album"], "status": r["status"],
                        "mb_albumid": r["mb_albumid"], "checked_at": r["checked_at"],
                        "fields": d.get("fields", {}), "tracks_changed": len(d.get("tracks", [])),
                        "moves": len(d.get("moves", [])), "new_dir": _new_dir(d)})
    by_field = defaultdict(int)
    for c in changed:
        for k in c["fields"]:
            by_field[k] += 1
    checked = conn.execute("SELECT COUNT(*) FROM audit_mbsync").fetchone()[0]
    return {"checked": checked, "changed": sum(c["status"] == "changed" for c in changed),
            "not_found": sum(c["status"] == "not_found" for c in changed),
            "albums_by_field": dict(sorted(by_field.items(), key=lambda kv: -kv[1])),
            "albums": changed[:limit] if limit else changed}


def _new_dir(diff: dict) -> str | None:
    dirs = {os.path.dirname(m["to"]) for m in diff.get("moves", [])}
    return dirs.pop() if len(dirs) == 1 else None


def mbsync_detail(conn: sqlite3.Connection, album_id: int) -> dict:
    r = conn.execute("SELECT * FROM audit_mbsync WHERE album_id = ?", (album_id,)).fetchone()
    if r is None:
        raise SystemExit(f"album {album_id} has not been checked; run `musiclib audit mbsync` first")
    return {**{k: r[k] for k in r.keys() if k != "changes"}, **(json.loads(r["changes"]) if r["changes"] else {})}


def move_leftovers(conn: sqlite3.Connection, old_dir: Path, new_dir: Path, reason: str,
                   decided_by: str) -> int:
    """beets moves its items with the album; files musiclib placed itself (D20 extras) are not
    beets items, so they follow here, logged."""
    if old_dir == new_dir or not old_dir.exists():
        return 0
    where = {d: s for s, d in current_paths(conn).items()}
    moved = 0
    for f in sorted(old_dir.iterdir()):
        if not f.is_file() or (new_dir / f.name).exists():
            continue
        shutil.move(str(f), new_dir / f.name)
        if str(f) in where:
            log_relocate(conn, where[str(f)], str(new_dir / f.name), reason, decided_by)
            moved += 1
    for d in (old_dir, old_dir.parent):  # the album folder, then the artist folder, if now empty
        try:
            d.rmdir()
        except OSError:
            break
    return moved


def log_relocate(conn, source: str, dest: str, reason: str, decided_by: str) -> None:
    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                 "VALUES (?, 'relocate', ?, ?, ?, ?)", (now(), source, dest, reason, decided_by))


def apply_mbsync(conn: sqlite3.Connection, lib, album_ids: list[int], reason: str, decided_by: str) -> dict:
    """Run beets mbsync on approved albums (tags written to library copies, files moved to the
    new layout), log every moved file and one mbsync row per album."""
    from beets import metadata_plugins
    from beetsplug.mbsync import MBSyncPlugin

    migrate(conn)
    plugin = MBSyncPlugin()
    out = []
    for album_id in album_ids:
        row = conn.execute("SELECT status FROM audit_mbsync WHERE album_id = ?", (album_id,)).fetchone()
        album = lib.get_album(album_id)
        if row is None or row["status"] != "changed" or album is None:
            out.append({"album_id": album_id, "skipped": "not a checked album with pending changes"})
            continue
        info = metadata_plugins.album_for_id(album.mb_albumid)
        diff = album_diff(album, info) if info else {"tracks": []}
        if not diff["tracks"]:  # MusicBrainz changed back, or someone already synced it
            conn.execute("UPDATE audit_mbsync SET status = 'same', changes = ?, checked_at = ? WHERE album_id = ?",
                         (json.dumps(diff), now(), album_id))
            out.append({"album_id": album_id, "skipped": "no changes any more"})
            continue
        items = list(album.items())
        old = {i.id: os.fsdecode(i.path) for i in items}
        old_dir = Path(old[items[0].id]).parent
        where = {d: s for s, d in current_paths(conn).items()}
        why = f"mbsync (D39): {', '.join(sorted(diff['fields']))}; {reason}"
        plugin.albums(lib, [f"id:{album_id}"], move=True, pretend=False, write=True)
        moved = 0
        for item in lib.get_album(album_id).items():
            new = os.fsdecode(item.path)
            if new != old.get(item.id) and old.get(item.id) in where:
                log_relocate(conn, where[old[item.id]], new, why, decided_by)
                moved += 1
        new_dir = Path(os.fsdecode(next(iter(lib.get_album(album_id).items())).path)).parent
        extras = move_leftovers(conn, old_dir, new_dir, why, decided_by)
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'mbsync', ?, ?, ?, ?)", (now(), str(old_dir), str(new_dir), why, decided_by))
        conn.execute("UPDATE audit_mbsync SET status = 'applied', applied_at = ?, album_dir = ? WHERE album_id = ?",
                     (now(), str(new_dir), album_id))
        conn.commit()
        out.append({"album_id": album_id, "album": f"{album.albumartist} - {album.album}",
                    "tracks_changed": len(diff["tracks"]), "files_moved": moved, "extras_moved": extras,
                    "dir": str(new_dir)})
    return {"albums": out, "applied": sum("skipped" not in a for a in out)}


# --- relink ---------------------------------------------------------------------------------

def relink(conn: sqlite3.Connection, lib, library_dir: Path, decided_by: str, *, apply: bool = False) -> dict:
    """Add the missing relocate rows for files beets moved without one (pairs from relink_pairs)."""
    on_disk = library_audio(library_dir)
    place = placements(conn)
    by_dest = {d: s for s, (d, _) in place.items()}
    items = {os.fsdecode(i.path) for i in lib.items()}
    known = {r[0] for r in conn.execute("SELECT path FROM files")}
    stale = [d for d in by_dest if d not in on_disk and by_dest[d] in known]
    unlogged = [p for p in items if p not in by_dest and p in on_disk]
    pairs = relink_pairs(stale, unlogged)
    if not apply:
        return {"dry_run": True, "pairs": len(pairs), "unpaired_logged_missing": len(stale) - len(pairs),
                "unpaired_unlogged": len(unlogged) - len(pairs),
                "examples": [{"from": o, "to": n} for o, n in sorted(pairs.items())[:EXAMPLES]]}
    why = "D39 relink: beets moved this file (album folder renamed) without an audit row"
    for old, new in sorted(pairs.items()):
        log_relocate(conn, by_dest[old], new, why, decided_by)
    conn.commit()
    return {"relinked": len(pairs)}


# --- the report -----------------------------------------------------------------------------

def check(conn: sqlite3.Connection, lib, library_dir: Path, source_dir: Path, *,
          accept_modified: bool = False) -> dict:
    migrate(conn)
    inc = incomplete(lib)
    art = no_art(lib)
    report = {
        "generated_at": now(),
        "integrity": integrity(conn, lib, library_dir, accept_modified=accept_modified),
        "originals": originals(conn, source_dir),
        "incomplete_albums": _cap(inc, 100),
        "no_art": _cap(art, 200),
        "unsorted": unsorted_counts(library_dir),  # no MusicBrainz IDs by design (D13)
        "decode": decode_findings(conn),
        "decode_pending": len(decode_pending(conn, library_dir)),
        "mbsync": mbsync_findings(conn),
        "albums": sum(1 for _ in lib.albums()),
    }
    report["mbsync"]["albums_unchecked"] = report["albums"] - report["mbsync"]["checked"]
    return report


def summary(report: dict) -> dict:
    """The headline numbers of a check, for the terminal."""
    i, o, m, d = report["integrity"], report["originals"], report["mbsync"], report["decode"]
    return {
        "library": {"albums": report["albums"], "files": i["library_files"], "beets_items": i["beets_items"]},
        "problems": {
            "beets_missing": i["beets_missing"]["count"], "logged_missing": i["logged_missing"]["count"],
            "orphan_rows": i["orphan_rows"]["count"],
            "untracked": i["untracked"]["count"], "unlogged": i["unlogged"]["count"],
            "relinkable": i["relinkable"], "modified_since_import": i["modified"]["count"],
            "originals_missing": o["missing"]["count"], "originals_changed": o["changed"]["count"],
            "damaged": d["damaged"]["count"],
        },
        "attention": {"incomplete_albums": report["incomplete_albums"]["count"],
                      "no_art": report["no_art"]["count"], "mbsync_changed": m["changed"],
                      "mbsync_not_found": m["not_found"]},
        "coverage": {"decoded": d["checked"], "decode_pending": report["decode_pending"],
                     "mbsync_checked": m["checked"], "mbsync_unchecked": m["albums_unchecked"]},
        "unsorted": report["unsorted"],
    }
