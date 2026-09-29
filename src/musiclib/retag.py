"""Relabel imported albums whose tags sit on the wrong audio, using fingerprints.

beets pairs files with release tracks by title tag, so an album whose files carry each
other's titles (e.g. Potshot, Till I Die) imports with every title on the wrong song. The
AcoustID recording (or title) of each file says which release track it really is. We only
act on a clean one-to-one pairing; anything ambiguous is reported, not changed. Library only.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .verify import MIN_SCORE, titles_agree


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint_ids(conn: sqlite3.Connection, rel: str) -> tuple[set[str], list[str]]:
    row = conn.execute("""
        SELECT a.recordings, a.titles FROM files f JOIN acoustid_lookups a
        ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration WHERE f.path = ?""",
                       (rel,)).fetchone()
    if not row:
        return set(), []
    recs = {r["id"] for r in json.loads(row[0] or "[]") if r["score"] >= MIN_SCORE}
    titles = [t["title"] for t in json.loads(row[1] or "{}").values()]
    return recs, titles


def swap_suspects(conn: sqlite3.Connection, min_swapped: int = 2) -> list[dict]:
    """Imported albums where mismatched files fingerprint as *another track of the same album*."""
    out = []
    for m in conn.execute("""
        SELECT m.id, m.album_key, m.files FROM matches m
        JOIN imports i ON i.match_id = m.id AND i.status = 'imported'"""):
        files = json.loads(m["files"])
        marks = ",".join("?" * len(files))
        rows = conn.execute(f"""SELECT f.path, f.title, v.verdict, a.titles FROM files f
            JOIN verify v ON v.file_id = f.id
            LEFT JOIN acoustid_lookups a ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
            WHERE f.path IN ({marks})""", files).fetchall()
        own = {r["path"]: r["title"] for r in rows}
        swapped = 0
        for r in rows:
            if r["verdict"] != "mismatch":
                continue
            ac = [t["title"] for t in json.loads(r["titles"] or "{}").values()]
            if any(titles_agree(t, o) for t in ac for p, o in own.items() if p != r["path"] and o):
                swapped += 1
        if swapped >= min_swapped:
            out.append({"album_key": m["album_key"], "files": len(files), "swapped": swapped})
    return out


def plan(conn: sqlite3.Connection, lib, album_key: str) -> dict:
    """Which library items should move to which release track, by fingerprint."""
    from beets import metadata_plugins

    m = conn.execute("""SELECT m.id, m.files, i.album_id FROM matches m
        JOIN imports i ON i.match_id = m.id AND i.status = 'imported' WHERE m.album_key = ?""",
                     (album_key,)).fetchone()
    if m is None:
        raise SystemExit(f"{album_key}: not imported")
    info = metadata_plugins.album_for_id(m["album_id"])
    if info is None:
        raise SystemExit(f"{album_key}: release {m['album_id']} not found")
    from .importer import current_paths

    by_path = {os.fsdecode(i.path): i for i in lib.items()}
    dest = current_paths(conn, set(json.loads(m["files"])))

    assign, problems = {}, []
    for rel, path in dest.items():
        item = by_path.get(path)
        if item is None:
            continue
        current = next((t for t in info.tracks if t.track_id == item.mb_trackid), None)
        recs, titles = _fingerprint_ids(conn, rel)
        cands = [t for t in info.tracks if t.track_id in recs] or \
                [t for t in info.tracks if any(titles_agree(t.title, a) for a in titles)]
        if len(cands) == 1:
            assign[path] = (item, current, cands[0])
        elif not recs and not titles:
            assign[path] = (item, current, current)  # no fingerprint data: leave as is
        else:
            problems.append(f"{Path(path).name}: {len(cands)} candidate tracks")
    # Independent evidence: a moved file's length must fit its new track better than its old one.
    lengths = dict(conn.execute("SELECT path, duration FROM files WHERE path IN (SELECT value FROM json_each(?))",
                                (m["files"],)).fetchall())
    rel_of = {v: k for k, v in dest.items()}
    for path, (item, cur, tr) in assign.items():
        if tr is None or cur is None or cur.track_id == tr.track_id or not (tr.length and cur.length):
            continue
        if abs(tr.length - cur.length) <= 3:
            continue  # equal-length tracks: length is no evidence either way
        dur = lengths.get(rel_of[path]) or 0
        if abs(dur - tr.length) >= abs(dur - cur.length):
            problems.append(f"{Path(path).name}: length {dur:.0f}s fits '{cur.title}' "
                            f"({cur.length:.0f}s) at least as well as '{tr.title}' ({tr.length:.0f}s)")
    targets = [t.track_id for _, _, t in assign.values() if t is not None]
    if len(targets) != len(set(targets)):
        problems.append("two files point at the same release track")
    changes = [{"file": Path(p).name, "from": c.title if c else None, "to": t.title,
                "to_track": t.index} for p, (_, c, t) in assign.items()
               if t is not None and (c is None or c.track_id != t.track_id)]
    return {"album_key": album_key, "release": m["album_id"], "changes": changes, "problems": problems,
            "_info": info, "_assign": assign}


def tmp_name(path: Path) -> Path:
    """Temporary name that keeps the real extension (beets builds the final name from it)."""
    return path.with_name(path.stem + ".retag-tmp" + path.suffix)


def apply(conn: sqlite3.Connection, lib, album_key: str, decided_by: str, reason: str) -> dict:
    from beets import autotag

    p = plan(conn, lib, album_key)
    if p["problems"]:
        raise SystemExit(f"{album_key}: not a clean pairing, nothing changed: {p['problems']}")
    from .importer import current_paths

    rel_of = {d: s for s, d in current_paths(conn).items()}
    moving = [(item, cur, tr, rel_of.get(os.fsdecode(item.path))) for item, cur, tr in p["_assign"].values()
              if tr is not None and (cur is None or cur.track_id != tr.track_id)]
    # Step aside first so swapped files don't collide on each other's names. Keep the real
    # extension: beets builds the final name from it.
    for item, _, _, _ in moving:
        old = Path(os.fsdecode(item.path))
        tmp = tmp_name(old)
        os.rename(old, tmp)
        item.path = os.fsencode(str(tmp))
        item.store()
    for item, cur, tr, rel in moving:
        before = cur.title if cur else None
        autotag.apply_metadata(p["_info"], [(item, tr)])
        item.try_write()
        item.move()
        item.store()
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'retag_by_fingerprint', ?, ?, ?, ?)",
                     (now(), rel, os.fsdecode(item.path),
                      f"{reason}: was '{before}', audio is '{tr.title}' (track {tr.index})", decided_by))
    conn.commit()
    return {"album_key": album_key, "retagged": len(moving)}
