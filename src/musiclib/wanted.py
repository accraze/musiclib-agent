"""The wanted list (D47): what to find a better copy of.

  refresh   automatic entries from the library: albums with damaged files (audit decode, D44),
            damaged files in Unsorted (one entry each), and albums averaging under 120 kbps.
            An entry closes itself (fulfilled) when its problem is gone from the library; a
            manual entry when an album of that artist and title is imported after it was added.
  add       the user's own entries (artist + album). `dismiss` takes any entry off the list
            for good; a dismissed automatic entry is not added again.
  page      a self-contained HTML page of the open list, to publish as an artifact.

Ingest dedupe (D30) asks before skipping an inbox copy of a wanted damaged album: see
`damaged_keys` and ingest._classify. Nothing here touches audio files.
"""

import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS wanted (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,  -- kind:<library folder | file | artist|album>
    kind          TEXT NOT NULL,         -- damaged | damaged_file | low_bitrate | manual
    artist        TEXT,
    album         TEXT,
    folder        TEXT,                  -- library folder (trailing /), automatic entries
    files         TEXT,                  -- JSON: the damaged library files
    detail        TEXT,                  -- why it is wanted, for the list
    mb_albumid    TEXT,
    note          TEXT,                  -- the user's note (manual)
    had_albums    TEXT,                  -- JSON: matching beets album ids when added (manual)
    status        TEXT NOT NULL,         -- open | fulfilled | dismissed
    added_by      TEXT NOT NULL,         -- auto | user | agent
    added_at      TEXT NOT NULL,
    closed_by     TEXT,
    closed_at     TEXT,
    closed_reason TEXT
);
"""

LOW_TIER = 1        # dupes.quality_tier 1: under 120 kbps on average
KINDS = ("manual", "damaged", "damaged_file", "low_bitrate")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def norm(s: str | None) -> str:
    """Name key for matching: case, accents, punctuation and a leading "the" don't count."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s[4:] if s.startswith("the ") else s


# --- what the library wants ----------------------------------------------------------------

def _damaged_files(conn: sqlite3.Connection) -> list[str]:
    from .audit import DAMAGED, is_damaged

    try:
        rows = conn.execute("""SELECT path, expected, mb_length, decoded FROM audit_decode
                               WHERE expected > 0 AND decoded < ? * expected""", (DAMAGED,)).fetchall()
    except sqlite3.OperationalError:  # no decode yet
        return []
    return sorted(r[0] for r in rows if is_damaged(r[1], r[3], r[2]) and os.path.exists(r[0]))


def findings(conn: sqlite3.Connection, lib, library_dir: Path) -> dict[str, dict]:
    """Automatic entries by key, from the library as it is now."""
    from .dupes import quality_tier

    unsorted = str(library_dir).rstrip("/") + "/Unsorted/"
    out: dict[str, dict] = {}
    by_folder: dict[str, list[str]] = {}
    for p in _damaged_files(conn):
        if p.startswith(unsorted):
            stem = os.path.splitext(os.path.basename(p))[0]
            out[f"damaged_file:{p}"] = {"kind": "damaged_file", "artist": None, "album": stem,
                                        "folder": os.path.dirname(p) + "/", "files": [p],
                                        "detail": "damaged file in Unsorted (cut short or broken)"}
        else:
            by_folder.setdefault(os.path.dirname(p) + "/", []).append(p)
    for album in lib.albums():
        items = list(album.items())
        if not items:
            continue
        folder = os.path.dirname(os.fsdecode(items[0].path)) + "/"
        base = {"artist": album.albumartist, "album": album.album, "folder": folder,
                "mb_albumid": album.mb_albumid or None}
        if folder in by_folder:
            hurt = by_folder.pop(folder)
            out[f"damaged:{folder}"] = {**base, "kind": "damaged", "files": hurt,
                                        "detail": f"{len(hurt)} of {len(items)} tracks damaged"}
        lossless = sum(1 for i in items if i.format in ("FLAC", "ALAC", "WAV", "AIFF")) / len(items) > 0.5
        known = [i.bitrate for i in items if i.bitrate]  # 0: beets doesn't know, not bad audio
        kbps = sum(known) / len(known) if known else 0
        if known and quality_tier(lossless, kbps) <= LOW_TIER:
            out[f"low_bitrate:{folder}"] = {**base, "kind": "low_bitrate", "files": None,
                                            "detail": f"{round(kbps / 1000)} kbps on average"}
    for folder, hurt in by_folder.items():  # damaged files outside any beets album
        out[f"damaged:{folder}"] = {"kind": "damaged", "artist": None, "album": os.path.basename(folder.rstrip("/")),
                                    "folder": folder, "files": hurt, "mb_albumid": None,
                                    "detail": f"{len(hurt)} damaged file(s)"}
    return out


def _matching(lib, artist: str, album: str) -> dict[int, str]:
    """Library albums of this artist and title: beets id -> folder."""
    want = (norm(artist), norm(album))
    out = {}
    for a in lib.albums():
        items = list(a.items()) if (norm(a.albumartist), norm(a.album)) == want else []
        if items:
            out[a.id] = os.path.dirname(os.fsdecode(items[0].path)) + "/"
    return out


def _arrived(lib, entry) -> str | None:
    """A library album matching a manual entry that wasn't there when it was added."""
    had = set(json.loads(entry["had_albums"] or "[]"))
    new = [f for i, f in _matching(lib, entry["artist"], entry["album"]).items() if i not in had]
    return new[0] if new else None


def refresh(conn: sqlite3.Connection, lib, library_dir: Path, *, clock=None) -> dict:
    """Bring the list up to date with the library. Returns what was added and fulfilled."""
    migrate(conn)
    t = (clock() if clock else datetime.now(timezone.utc)).isoformat(timespec="seconds")
    found = findings(conn, lib, library_dir)
    rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM wanted")}
    added, fulfilled = [], []
    with conn:
        for key, f in found.items():
            r = rows.get(key)
            files = json.dumps(f["files"]) if f["files"] else None
            if r is None or r["status"] == "fulfilled":  # new, or back again after a fix
                conn.execute("DELETE FROM wanted WHERE key = ?", (key,))
                conn.execute("""INSERT INTO wanted (key, kind, artist, album, folder, files, detail, mb_albumid,
                                status, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 'auto', ?)""",
                             (key, f["kind"], f["artist"], f["album"], f["folder"], files, f["detail"],
                              f.get("mb_albumid"), t))
                added.append(_label(f))
            elif r["status"] == "open":
                conn.execute("UPDATE wanted SET files = ?, detail = ? WHERE key = ?", (files, f["detail"], key))
        for key, r in rows.items():
            if r["status"] != "open":
                continue
            if r["kind"] == "manual":
                where = _arrived(lib, r)
                reason = f"imported: {where}" if where else None
            elif key not in found:
                gone = (not os.path.exists(json.loads(r["files"])[0]) if r["kind"] == "damaged_file"
                        else not os.path.isdir(r["folder"] or ""))
                reason = ("gone from the library (replaced or removed)" if gone else
                          "now above 120 kbps on average" if r["kind"] == "low_bitrate" else
                          "no damaged files left there")
            else:
                reason = None
            if reason:
                conn.execute("""UPDATE wanted SET status = 'fulfilled', closed_by = 'auto', closed_at = ?,
                                closed_reason = ? WHERE key = ?""", (t, reason, key))
                fulfilled.append({"entry": _label(r), "kind": r["kind"], "why": reason})
    return {"added": len(added), "added_examples": added[:20], "fulfilled": fulfilled,
            "open": counts(conn)}


# --- by hand -------------------------------------------------------------------------------

def add(conn: sqlite3.Connection, lib, artist: str, album: str, *, note: str | None = None,
        added_by: str = "user") -> dict:
    migrate(conn)
    if not norm(artist) or not norm(album):
        raise SystemExit("wanted add: give both --artist and --album")
    key = f"manual:{norm(artist)}|{norm(album)}"
    r = conn.execute("SELECT id, status FROM wanted WHERE key = ?", (key,)).fetchone()
    if r and r["status"] == "open":
        return {"id": r["id"], "already_wanted": True}
    have = _matching(lib, artist, album)
    with conn:
        conn.execute("DELETE FROM wanted WHERE key = ?", (key,))
        cur = conn.execute("""INSERT INTO wanted (key, kind, artist, album, note, had_albums, detail, status,
                              added_by, added_at) VALUES (?, 'manual', ?, ?, ?, ?, ?, 'open', ?, ?)""",
                           (key, artist, album, note, json.dumps(sorted(have)), "added by hand", added_by, now()))
    # Already in the library: still wanted (a better copy, say); only another album fulfills it.
    return {"id": cur.lastrowid, "added": f"{artist} - {album}", "in_library_now": sorted(have.values())}


def dismiss(conn: sqlite3.Connection, ids: list[int], *, reason: str, by: str = "user") -> dict:
    migrate(conn)
    done = []
    with conn:
        for i in ids:
            r = conn.execute("SELECT * FROM wanted WHERE id = ?", (i,)).fetchone()
            if r is None:
                raise SystemExit(f"wanted dismiss: no entry {i}")
            if r["status"] == "open":
                conn.execute("""UPDATE wanted SET status = 'dismissed', closed_by = ?, closed_at = ?,
                                closed_reason = ? WHERE id = ?""", (by, now(), reason, i))
                done.append(_label(r))
    return {"dismissed": done}


# --- reading -------------------------------------------------------------------------------

def _label(r) -> str:
    return f"{r['artist']} - {r['album']}" if r["artist"] else r["album"]


def counts(conn: sqlite3.Connection) -> dict:
    migrate(conn)
    return {k: n for k, n in conn.execute(
        "SELECT kind, COUNT(*) FROM wanted WHERE status = 'open' GROUP BY kind ORDER BY kind")}


def entries(conn: sqlite3.Connection, status: str = "open", kind: str | None = None) -> list[dict]:
    migrate(conn)
    q = "SELECT * FROM wanted WHERE status = ?" + (" AND kind = ?" if kind else "")
    out = []
    for r in conn.execute(q + " ORDER BY kind, COALESCE(artist, album), album", (status, *([kind] if kind else []))):
        d = {k: r[k] for k in ("id", "kind", "artist", "album", "folder", "detail", "mb_albumid", "note",
                               "added_by", "added_at", "closed_at", "closed_reason") if r[k] is not None}
        if r["files"]:
            d["files"] = json.loads(r["files"])
        out.append(d)
    out.sort(key=lambda d: KINDS.index(d["kind"]))
    return out


def damaged_keys(conn: sqlite3.Connection, lib_rows: list[dict]) -> set[str]:
    """Track keys (dupes.track_key) of the library files on open damaged entries: an inbox
    copy holding one of them is asked about instead of skipped as a duplicate (ingest dedupe)."""
    from .dupes import track_key

    migrate(conn)
    hurt = set()
    for (files,) in conn.execute("SELECT files FROM wanted WHERE status = 'open' AND files IS NOT NULL"):
        hurt.update(json.loads(files))
    return {track_key(r) for r in lib_rows if r["path"] in hurt}


def write_page(conn: sqlite3.Connection, out: Path) -> dict:
    """The open list as a self-contained HTML page (template next to this module)."""
    data = {"generated": now(), "entries": entries(conn)}
    for e in data["entries"]:
        e.pop("files", None)  # library paths aren't needed on a page that may be shared
        e.pop("folder", None)
    tpl = (Path(__file__).parent / "pages" / "wanted.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tpl.replace("__DATA__", blob))
    return {"entries": len(data["entries"]), "by_kind": counts(conn), "written": str(out)}
