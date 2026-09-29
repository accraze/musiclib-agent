"""M3 dry run: match every album that survives dedupe against MusicBrainz via beets.

Read-only on files (beets only reads tags here). Results go to the `matches` table;
`musiclib import` later applies them. Resumable: matched albums are skipped unless --rematch.

Actions (D11, D13, D15):
  auto      beets strong recommendation (distance <= 0.04)
  review    candidates exist but none is strong, or the album sits in a dupe review group
  unsorted  no candidates at all: import as-is into Unsorted/
  error     unreadable files
"""

import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id             INTEGER PRIMARY KEY,
    album_key      TEXT NOT NULL UNIQUE,    -- first directory, relative to source_dir
    dirs           TEXT NOT NULL,           -- JSON list (several for multi-disc albums)
    files          TEXT NOT NULL,           -- JSON list of relative file paths to import
    action         TEXT NOT NULL,           -- auto | review | unsorted | error
    note           TEXT,
    recommendation TEXT,                    -- none | low | medium | strong
    distance       REAL,
    album_id       TEXT,                    -- best candidate's MusicBrainz release id
    albumartist    TEXT,
    album          TEXT,
    year           INTEGER,
    extra_items    INTEGER,                 -- our files beets couldn't place on the release
    extra_tracks   INTEGER,                 -- release tracks we don't have
    candidates     TEXT,                    -- JSON top candidates with penalties
    search_id      TEXT,                    -- release id pinned by D10 tag carry-over
    matched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS matches_action ON matches(action);
"""
TOP_CANDIDATES = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dropped(conn: sqlite3.Connection) -> tuple[set[str], list[str], set[str]]:
    """Files and folders dropped by auto dupe groups, and folders in dupe review groups."""
    files, folders, review = set(), [], set()
    for r in conn.execute("""
        SELECT g.scope, g.action, m.path, m.role FROM dupe_groups g
        JOIN dupe_members m ON m.group_id = g.id"""):
        if r["action"] == "auto" and r["role"] == "drop":
            (files.add if r["scope"] == "file" else folders.append)(r["path"])
        elif r["action"] == "review":
            review.add(r["path"])
    return files, folders, review


def _carry_ids(conn: sqlite3.Connection) -> dict[str, str]:
    """D10: keeper folder -> the loser's majority verified release id."""
    out = {}
    for g in conn.execute("SELECT keeper, carry_tags_from FROM dupe_groups WHERE carry_tags_from IS NOT NULL"):
        row = conn.execute("""
            SELECT f.mb_albumid, COUNT(*) AS n FROM files f JOIN verify v ON v.file_id = f.id
            WHERE f.path LIKE ? || '%' AND v.verdict = 'confirmed' AND f.mb_albumid != ''
            GROUP BY f.mb_albumid ORDER BY n DESC LIMIT 1""", (g["carry_tags_from"],)).fetchone()
        if row:
            out[g["keeper"]] = row["mb_albumid"]
    return out


def albums(conn: sqlite3.Connection, source: Path, subdir: str | None = None):
    """Yield (album_key, dirs, files) for every album to import, relative to source."""
    from beets.importer.tasks import albums_in_dir

    audio = {r[0] for r in conn.execute("SELECT path FROM files")}
    drop_files, drop_folders, _ = _dropped(conn)

    def keep(rel: str) -> bool:
        return rel in audio and rel not in drop_files and not any(rel.startswith(d) for d in drop_folders)

    tops = sorted(p for p in os.listdir(source) if (source / p).is_dir())
    if subdir:
        tops = [t for t in tops if t == subdir or t.startswith(subdir)]
    for top in tops:
        for dirs, paths in albums_in_dir(os.fsencode(source / top)):
            rels = sorted(r for r in (os.path.relpath(os.fsdecode(p), source) for p in paths) if keep(r))
            if rels:
                rel_dirs = [os.path.relpath(os.fsdecode(d), source) + "/" for d in dirs]
                yield rel_dirs[0], rel_dirs, rels


def _candidate(m) -> dict:
    info = m.info
    return {"album_id": info.album_id, "artist": info.artist, "album": info.album,
            "year": info.year, "country": info.country, "media": info.media,
            "tracks": len(info.tracks), "distance": round(float(m.distance), 4),
            "penalties": [k for k, v in m.distance.items() if v > 0][:6],
            "extra_items": len(m.extra_items), "extra_tracks": len(m.extra_tracks)}


def match_album(source: Path, files: list[str], search_id: str | None) -> dict:
    from beets.autotag.match import tag_album
    from beets.library import Item

    items = [Item.from_path(os.fsencode(source / f)) for f in files]
    _, _, proposal = tag_album(items, search_ids=[search_id] if search_id else [])
    cands = [_candidate(m) for m in proposal.candidates[:TOP_CANDIDATES]]
    rec = proposal.recommendation.name
    out = {"recommendation": rec, "candidates": json.dumps(cands, ensure_ascii=False)}
    if not cands:
        return {**out, "action": "unsorted"}
    best = cands[0]
    return {**out, "action": "auto" if rec == "strong" else "review",
            "distance": best["distance"], "album_id": best["album_id"],
            "albumartist": best["artist"], "album": best["album"], "year": best["year"],
            "extra_items": best["extra_items"], "extra_tracks": best["extra_tracks"]}


COLUMNS = ["album_key", "dirs", "files", "action", "note", "recommendation", "distance",
           "album_id", "albumartist", "album", "year", "extra_items", "extra_tracks",
           "candidates", "search_id", "matched_at"]


def run(conn: sqlite3.Connection, source: Path, *, subdir: str | None = None,
        limit: int | None = None, rematch: bool = False, progress=sys.stderr) -> dict:
    conn.executescript(SCHEMA)
    done = set() if rematch else {r[0] for r in conn.execute("SELECT album_key FROM matches")}
    carry = _carry_ids(conn)
    _, _, review_folders = _dropped(conn)
    todo = [a for a in albums(conn, source, subdir) if a[0] not in done][:limit]
    print(f"match: {len(todo)} albums to match (MusicBrainz is rate-limited to ~1 request/s)",
          file=progress, flush=True)

    counts = Counter()
    started = time.monotonic()
    for n, (key, dirs, files) in enumerate(todo, 1):
        search_id = next((carry[d] for d in dirs if d in carry), None)
        row = {"album_key": key, "dirs": json.dumps(dirs), "files": json.dumps(files),
               "search_id": search_id, "matched_at": now()}
        try:
            row.update(match_album(source, files, search_id))
        except Exception as e:  # unreadable file, network trouble: record and move on
            row.update(action="error", note=f"{type(e).__name__}: {e}"[:300])
        if row["action"] == "auto" and any(d in review_folders for d in dirs):
            row.update(action="review", note="album is in a duplicate review group")
        conn.execute(
            f"INSERT OR REPLACE INTO matches ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
            [row.get(c) for c in COLUMNS])
        conn.commit()
        counts[row["action"]] += 1
        if n % 25 == 0 or n == len(todo):
            elapsed = time.monotonic() - started
            print(f"  {n}/{len(todo)} {dict(counts)} ETA {(len(todo) - n) * elapsed / n / 60:.0f} min",
                  file=progress, flush=True)
    return {"matched": len(todo), **counts, "elapsed_s": round(time.monotonic() - started, 1)}


def summary(conn: sqlite3.Connection, top: int = 10) -> dict:
    conn.executescript(SCHEMA)
    return {
        "by_action": {r[0]: {"albums": r[1], "files": r[2]} for r in conn.execute(
            "SELECT action, COUNT(*), SUM(json_array_length(files)) FROM matches GROUP BY action")},
        "by_recommendation": {r[0]: r[1] for r in conn.execute(
            "SELECT recommendation, COUNT(*) FROM matches GROUP BY recommendation")},
        "review_samples": [dict(r) for r in conn.execute("""
            SELECT album_key, recommendation, distance, albumartist, album, year, extra_items,
                   extra_tracks, note FROM matches WHERE action = 'review' ORDER BY random() LIMIT ?""",
            (top,))],
    }


def _ensure_imports(conn: sqlite3.Connection) -> None:
    from .importer import migrate
    migrate(conn)


def _audio_ids(conn: sqlite3.Connection, files: list[str]) -> set[str]:
    marks = ",".join("?" * len(files))
    return {r[0] for r in conn.execute(f"""
        SELECT a.acoustid_id FROM files f JOIN acoustid_lookups a
        ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
        WHERE f.path IN ({marks}) AND a.acoustid_id IS NOT NULL""", files)}


def merge_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Undecided, unimported albums that look like parts of one release: same top-candidate
    release, no shared audio between the parts (shared audio means copies, not parts), and
    together no more files than the release has tracks (+2 for bonus files)."""
    _ensure_imports(conn)
    groups: dict[str, list] = {}
    for m in conn.execute("""
        SELECT m.album_key, m.files, m.candidates FROM matches m
        LEFT JOIN imports i ON i.match_id = m.id AND i.status = 'imported'
        WHERE m.decision IS NULL AND i.match_id IS NULL AND m.action IN ('review', 'auto')"""):
        cands = json.loads(m["candidates"] or "[]")
        if cands:
            groups.setdefault(cands[0]["album_id"], []).append((m["album_key"], json.loads(m["files"]), cands[0]))
    out = []
    for rid, parts in groups.items():
        if len(parts) < 2:
            continue
        release = parts[0][2]
        total = sum(len(f) for _, f, _ in parts)
        if total > release["tracks"] + 2:
            continue
        ids = [_audio_ids(conn, f) for _, f, _ in parts]
        overlap = any(ids[i] & ids[j] for i in range(len(ids)) for j in range(i + 1, len(ids)))
        if overlap:
            continue
        out.append({"release": rid, "title": f"{release['artist']} - {release['album']} ({release['year']})",
                    "tracks": release["tracks"], "files": total, "albums": sorted(k for k, _, _ in parts)})
    return out


def merge(conn: sqlite3.Connection, source: Path, keys: list[str], *, apply: bool = False) -> dict:
    """Combine several album folders into one album and re-match it. The merged album takes
    the first key; the others are removed from the queue. Refuses decided or imported albums."""
    _ensure_imports(conn)
    rows = []
    for k in keys:
        r = conn.execute("SELECT m.*, i.status AS imported FROM matches m LEFT JOIN imports i "
                         "ON i.match_id = m.id AND i.status = 'imported' WHERE m.album_key = ?", (k,)).fetchone()
        if r is None:
            raise SystemExit(f"unknown album: {k}")
        if r["decision"] or r["imported"]:
            raise SystemExit(f"{k}: already decided or imported")
        rows.append(r)
    dirs = [d for r in rows for d in json.loads(r["dirs"])]
    files = sorted({f for r in rows for f in json.loads(r["files"])})
    if not apply:
        return {"dry_run": True, "into": keys[0], "dirs": dirs, "files": len(files)}
    result = match_album(source, files, None)
    row = {"album_key": keys[0], "dirs": json.dumps(dirs), "files": json.dumps(files),
           "note": f"merged from {len(keys)} folders", "search_id": None, "matched_at": now(), **result}
    with conn:
        conn.executemany("DELETE FROM matches WHERE id = ?", [(r["id"],) for r in rows])
        conn.execute(f"INSERT INTO matches ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                     [row.get(c) for c in COLUMNS])
        conn.execute("INSERT INTO audit_log (ts, action, source_path, reason, decided_by) "
                     "VALUES (?, 'merge_albums', ?, ?, 'user')",
                     (now(), keys[0], "merged: " + " + ".join(keys)))
    return {"into": keys[0], "files": len(files), "action": row["action"],
            "recommendation": row.get("recommendation"), "distance": row.get("distance"),
            "album": f"{row.get('albumartist')} - {row.get('album')} ({row.get('year')})"}
