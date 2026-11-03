"""Cover art for albums without it (D46).

  scan     for each library album without a folder image, find art: exact sources first
           (art embedded in its own tracks, then the Cover Art Archive by release, then by
           release group); with --search, albums with none get candidates from iTunes and
           Deezer, searched by artist and title. Downloads are cached in state/art/.
  apply    writes cover.<ext> into the album folder, sets the album's art in beets and embeds
           it into tracks that have none. Exact candidates apply as a standing approval;
           search candidates only once the user picked them (`decide`, or the gallery page).
  retry    weekly from the runner: exact sources again for albums still without art.

Every file written is logged (art_cover, art_embed). Dry run unless `apply`.
"""

import io
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS art_candidates (
    id          INTEGER PRIMARY KEY,
    album_id    INTEGER NOT NULL,      -- beets album id
    kind        TEXT NOT NULL,         -- exact | search
    source      TEXT NOT NULL,         -- embedded | caa_release | caa_group | itunes | deezer
    url         TEXT,                  -- where it came from (embedded: the track)
    title       TEXT,                  -- what the source calls it (search), for the gallery
    path        TEXT NOT NULL,         -- cached download under state/art/
    width       INTEGER,
    height      INTEGER,
    status      TEXT NOT NULL,         -- proposed | approved | rejected | applied | superseded
    decided_by  TEXT,
    decided_at  TEXT,
    found_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS art_candidates_album ON art_candidates(album_id);
CREATE TABLE IF NOT EXISTS art_checks (
    album_id    INTEGER PRIMARY KEY,
    exact_at    TEXT,                  -- last exact lookup
    exact_found INTEGER,
    search_at   TEXT,
    none_by     TEXT                   -- the user chose no cover for it (gallery "none")
);
"""

UA = "musiclib/0.1 (personal library manager; https://github.com/accraze/musiclib-agent)"
MIN_SIDE = 200          # smaller images aren't worth a cover
SEARCH_PER_SOURCE = 3
RETRY_DAYS = 7
MIME_EXT = {"JPEG": "jpg", "PNG": "png"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def cache_dir(state_dir: Path) -> Path:
    return state_dir / "art"


# --- network (replaced in tests) ------------------------------------------------------------

_last_request = [0.0]
REQUEST_GAP = 1.0       # seconds between requests (Cover Art Archive, iTunes rate limits)


def http_get(url: str, *, timeout: float = 30) -> bytes | None:
    """GET a URL, at most one request per REQUEST_GAP; None on 404 (no art there)."""
    import requests

    wait = REQUEST_GAP - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code == 200:
        return r.content
    if r.status_code in (404, 400, 410):
        return None
    r.raise_for_status()
    return None


def _json(url: str) -> dict:
    data = http_get(url)
    return json.loads(data) if data else {}


# --- images ---------------------------------------------------------------------------------

def image_info(data: bytes) -> tuple[str, int, int] | None:
    """(ext, width, height) of a JPEG or PNG, or None if it isn't one we can use."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            ext = MIME_EXT.get(im.format)
            w, h = im.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if not ext or min(w, h) < MIN_SIDE:
        return None
    return ext, w, h


def thumbnail(path: str, side: int = 300) -> bytes:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()


# --- finding art ----------------------------------------------------------------------------

def has_folder_art(album) -> bool:
    return bool(album.artpath) and os.path.exists(os.fsdecode(album.artpath))


def missing(lib) -> list:
    return [a for a in lib.albums() if not has_folder_art(a) and list(a.items())]


def embedded(album) -> tuple[bytes, str] | None:
    """The image most of the album's tracks carry, and one track that has it."""
    import mediafile

    seen, where = Counter(), {}
    for item in album.items():
        try:
            imgs = mediafile.MediaFile(os.fsdecode(item.path)).images or []
        except Exception:  # noqa: BLE001  unreadable tags: no art from this one
            continue
        front = next((i for i in imgs if i.type == mediafile.ImageType.front), imgs[0] if imgs else None)
        if front and front.data:
            seen[front.data] += 1
            where.setdefault(front.data, os.fsdecode(item.path))
    if not seen:
        return None
    data = seen.most_common(1)[0][0]
    return data, where[data]


def caa(kind: str, mbid: str, get=None) -> tuple[bytes, str] | None:
    get = get or http_get
    for size in ("front-1200", "front"):
        url = f"https://coverartarchive.org/{kind}/{mbid}/{size}"
        data = get(url)
        if data:
            return data, url
    return None


def search(album, get_json=None) -> list[tuple[str, str, str]]:
    """(source, image url, what the source calls it) from iTunes and Deezer."""
    get_json = get_json or _json
    artist, title = album.albumartist or "", album.album or ""
    out = []
    q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "album", "limit": SEARCH_PER_SOURCE})
    for r in get_json(f"https://itunes.apple.com/search?{q}").get("results", [])[:SEARCH_PER_SOURCE]:
        if r.get("artworkUrl100"):
            out.append(("itunes", r["artworkUrl100"].replace("100x100bb", "1200x1200bb"),
                        f"{r.get('artistName', '')} - {r.get('collectionName', '')}"))
    q = urllib.parse.urlencode({"q": f'artist:"{artist}" album:"{title}"', "limit": SEARCH_PER_SOURCE})
    for r in get_json(f"https://api.deezer.com/search/album?{q}").get("data", [])[:SEARCH_PER_SOURCE]:
        if r.get("cover_xl"):
            out.append(("deezer", r["cover_xl"], f"{(r.get('artist') or {}).get('name', '')} - {r.get('title', '')}"))
    return out


def _store(conn, state_dir: Path, album_id: int, kind: str, source: str, url: str, title: str | None,
           data: bytes) -> int | None:
    info = image_info(data)
    if info is None:
        return None
    ext, w, h = info
    cur = conn.execute("""INSERT INTO art_candidates (album_id, kind, source, url, title, path, width, height,
                          status, found_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, 'proposed', ?)""",
                       (album_id, kind, source, url, title, w, h, now()))
    cid = cur.lastrowid
    path = cache_dir(state_dir) / str(album_id) / f"{cid}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    conn.execute("UPDATE art_candidates SET path = ? WHERE id = ?", (str(path), cid))
    return cid


def find_exact(conn, state_dir: Path, album, get=None) -> int | None:
    """Store the first exact candidate for this album; returns its id."""
    found = None
    emb = embedded(album)
    if emb:
        found = _store(conn, state_dir, album.id, "exact", "embedded", emb[1], None, emb[0])
    for kind, mbid in (("release", album.mb_albumid), ("release-group", album.mb_releasegroupid)):
        if found or not mbid:
            continue
        hit = caa(kind, mbid, get)
        if hit:
            found = _store(conn, state_dir, album.id, "exact", "caa_" + kind.split("-")[-1], hit[1], None, hit[0])
    conn.execute("""INSERT INTO art_checks (album_id, exact_at, exact_found) VALUES (?, ?, ?)
                    ON CONFLICT(album_id) DO UPDATE SET exact_at = excluded.exact_at,
                    exact_found = excluded.exact_found""", (album.id, now(), int(bool(found))))
    return found


def find_search(conn, state_dir: Path, album, get=None, get_json=None) -> list[int]:
    get = get or http_get
    ids = []
    for source, url, title in search(album, get_json):
        try:
            data = get(url)
        except Exception:  # noqa: BLE001  one bad candidate doesn't stop the rest
            data = None
        if data and (cid := _store(conn, state_dir, album.id, "search", source, url, title, data)):
            ids.append(cid)
    conn.execute("""INSERT INTO art_checks (album_id, search_at) VALUES (?, ?)
                    ON CONFLICT(album_id) DO UPDATE SET search_at = excluded.search_at""", (album.id, now()))
    return ids


def _open(conn, album_id: int, kinds=("exact", "search")) -> list:
    marks = ",".join("?" * len(kinds))
    return conn.execute(f"""SELECT * FROM art_candidates WHERE album_id = ? AND kind IN ({marks})
                            AND status IN ('proposed', 'approved') ORDER BY id""", (album_id, *kinds)).fetchall()


def scan(conn: sqlite3.Connection, lib, state_dir: Path, *, search_too: bool = False, limit: int | None = None,
         get=None, get_json=None, progress=sys.stderr) -> dict:
    """Find art for albums without it. Network only; nothing in the library changes."""
    migrate(conn)
    todo = missing(lib)
    todo = todo[:limit] if limit else todo
    counts = Counter()
    for n, album in enumerate(todo, 1):
        done = conn.execute("SELECT none_by FROM art_checks WHERE album_id = ?", (album.id,)).fetchone()
        if done and done[0]:
            counts["chose_none"] += 1
            continue
        try:
            if _open(conn, album.id, ("exact",)) or find_exact(conn, state_dir, album, get):
                counts["exact"] += 1
            elif not search_too:
                counts["none"] += 1
            elif _open(conn, album.id, ("search",)) or find_search(conn, state_dir, album, get, get_json):
                counts["search"] += 1
            else:
                counts["none"] += 1
        except Exception as e:  # noqa: BLE001  network trouble: the next scan tries again
            print(f"art {album.id} {album.albumartist} - {album.album}: {e}", file=progress)
            counts["error"] += 1
        conn.commit()
        if n % 10 == 0 or n == len(todo):
            print(f"art {n}/{len(todo)} {dict(counts)}", file=progress, flush=True)
    return {"albums_without_art": len(missing(lib)), "scanned": len(todo), **counts}


# --- deciding (search candidates) -----------------------------------------------------------

def decide(conn: sqlite3.Connection, album_id: int, candidate: int | None, decided_by: str) -> dict:
    """The user's pick for an album: one candidate, or None for 'no cover fits'."""
    migrate(conn)
    if decided_by not in ("agent", "user"):
        raise SystemExit("decided_by must be agent or user")
    with conn:
        if candidate is None:
            conn.execute("UPDATE art_candidates SET status = 'rejected', decided_by = ?, decided_at = ? "
                         "WHERE album_id = ? AND status IN ('proposed', 'approved')", (decided_by, now(), album_id))
            conn.execute("""INSERT INTO art_checks (album_id, none_by) VALUES (?, ?)
                            ON CONFLICT(album_id) DO UPDATE SET none_by = excluded.none_by""", (album_id, decided_by))
            return {"album_id": album_id, "decision": "none"}
        row = conn.execute("SELECT * FROM art_candidates WHERE id = ? AND album_id = ?", (candidate, album_id)).fetchone()
        if row is None:
            raise SystemExit(f"candidate {candidate} is not one of album {album_id}'s")
        conn.execute("UPDATE art_candidates SET status = 'rejected', decided_by = ?, decided_at = ? "
                     "WHERE album_id = ? AND id != ? AND status IN ('proposed', 'approved')",
                     (decided_by, now(), album_id, candidate))
        conn.execute("UPDATE art_candidates SET status = 'approved', decided_by = ?, decided_at = ? WHERE id = ?",
                     (decided_by, now(), candidate))
    return {"album_id": album_id, "decision": candidate}


def pending_search(conn: sqlite3.Connection, lib) -> list[dict]:
    """Albums whose only candidates come from search and wait for the user (the gallery)."""
    migrate(conn)
    out = []
    for album in missing(lib):
        rows = _open(conn, album.id)
        if not rows or any(r["kind"] == "exact" or r["status"] == "approved" for r in rows):
            continue
        out.append({"album_id": album.id, "album": f"{album.albumartist} - {album.album}",
                    "year": album.original_year or album.year, "mb_albumid": album.mb_albumid,
                    "tracks": len(album.items()),
                    "candidates": [{"id": r["id"], "source": r["source"], "title": r["title"],
                                    "width": r["width"], "height": r["height"], "path": r["path"]} for r in rows]})
    return out


def write_gallery(conn: sqlite3.Connection, lib, out: Path) -> dict:
    """The data behind the gallery page: each album waiting for a pick, its candidates as
    300px thumbnails (data URIs, so the page needs nothing from outside)."""
    import base64

    albums = pending_search(conn, lib)
    for a in albums:
        for c in a["candidates"]:
            c["thumb"] = "data:image/jpeg;base64," + base64.b64encode(thumbnail(c.pop("path"))).decode()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated": now(), "albums": albums}, ensure_ascii=False))
    return {"albums": len(albums), "candidates": sum(len(a["candidates"]) for a in albums),
            "bytes": out.stat().st_size, "written": str(out)}


# --- applying -------------------------------------------------------------------------------

def plan(conn: sqlite3.Connection, lib) -> list[dict]:
    """What apply would do: each album without art that has an exact or an approved candidate."""
    migrate(conn)
    out = []
    for album in missing(lib):
        rows = _open(conn, album.id)
        pick = next((r for r in rows if r["status"] == "approved"), None) or \
            next((r for r in rows if r["kind"] == "exact"), None)
        if pick is None:
            continue
        items = list(album.items())
        out.append({"album_id": album.id, "album": f"{album.albumartist} - {album.album}",
                    "candidate": pick["id"], "source": pick["source"], "size": f"{pick['width']}x{pick['height']}",
                    "decided_by": pick["decided_by"] or "auto", "tracks": len(items),
                    "cover": str(_album_dir(items) / f"cover{Path(pick['path']).suffix}")})
    return out


def _album_dir(items) -> Path:
    return Path(os.path.commonpath([os.path.dirname(os.fsdecode(i.path)) for i in items]))


def _has_art(path: str) -> bool:
    import mediafile

    try:
        return bool(mediafile.MediaFile(path).images)
    except Exception:  # noqa: BLE001
        return True  # can't read it: don't write to it either


def _embed(item, data: bytes) -> None:
    import mediafile

    mf = mediafile.MediaFile(os.fsdecode(item.path))
    mf.images = [mediafile.Image(data=data, desc="", type=mediafile.ImageType.front)]
    mf.save()
    item.mtime = item.current_mtime()  # beets wrote it: the audit's modified check follows along
    item.store()


def apply(conn: sqlite3.Connection, lib, cfg, *, apply: bool = False, albums: list[int] | None = None,
          progress=sys.stderr) -> dict:
    """Write each planned cover (cover.<ext> + embed into tracks without art). Dry run unless `apply`."""
    from .importer import library_lock

    todo = [p for p in plan(conn, lib) if albums is None or p["album_id"] in albums]
    if not apply:
        return {"dry_run": True, "would_apply": todo}
    done, skipped = [], []
    with library_lock(cfg.state_dir):
        for p in todo:
            album = lib.get_album(p["album_id"])
            row = conn.execute("SELECT * FROM art_candidates WHERE id = ?", (p["candidate"],)).fetchone()
            cover = Path(p["cover"])
            if album is None or has_folder_art(album):
                skipped.append({**p, "why": "has art now"})
                continue
            if cover.exists():  # never overwrite a file we didn't write
                skipped.append({**p, "why": f"{cover.name} already exists; set it as the album's art by hand"})
                continue
            data = Path(row["path"]).read_bytes()
            by = row["decided_by"] or "auto"
            reason = f"D46: cover art from {row['source']}" + (f" ({row['title']})" if row["title"] else "")
            cover.write_bytes(data)
            album.artpath = os.fsencode(str(cover))
            album.store()
            embedded_into = []
            for item in album.items():
                path = os.fsdecode(item.path)
                if not _has_art(path):
                    _embed(item, data)
                    embedded_into.append(path)
            with conn:
                conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                             "VALUES (?, 'art_cover', ?, ?, ?, ?)", (now(), row["url"], str(cover), reason, by))
                conn.executemany("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                                 "VALUES (?, 'art_embed', ?, ?, ?, ?)",
                                 [(now(), str(cover), d, reason, by) for d in embedded_into])
                conn.execute("UPDATE art_candidates SET status = 'applied' WHERE id = ?", (row["id"],))
                conn.execute("UPDATE art_candidates SET status = 'superseded' WHERE album_id = ? AND id != ? "
                             "AND status IN ('proposed', 'approved')", (album.id, row["id"]))
            done.append({**p, "embedded_into": len(embedded_into)})
            print(f"art: {p['album']} <- {row['source']} ({len(embedded_into)} tracks embedded)",
                  file=progress, flush=True)
    return {"applied": done, "skipped": skipped}


def retry(conn: sqlite3.Connection, lib, cfg, *, days: int = RETRY_DAYS, get=None, clock=None,
          progress=sys.stderr) -> dict:
    """Weekly (runner): exact sources again for albums without art not checked for `days`,
    then apply what turned up. Search sources stay with the user."""
    migrate(conn)
    t = clock() if clock else datetime.now(timezone.utc)
    cutoff = (t - timedelta(days=days)).isoformat(timespec="seconds")
    last = dict(conn.execute("SELECT album_id, exact_at FROM art_checks"))
    due = [a for a in missing(lib) if not last.get(a.id) or last[a.id] < cutoff]
    get = get or http_get  # looked up at call time, so tests can replace it
    found = 0
    for album in due:
        try:
            found += bool(_open(conn, album.id, ("exact",)) or find_exact(conn, cfg.state_dir, album, get))
        except Exception as e:  # noqa: BLE001  network trouble: next week
            print(f"art retry {album.id}: {e}", file=progress)
        conn.commit()
    exact = [p for p in plan(conn, lib) if p["decided_by"] == "auto"]
    res = apply(conn, lib, cfg, apply=True, albums=[p["album_id"] for p in exact], progress=progress) \
        if exact else {"applied": [], "skipped": []}
    return {"checked": len(due), "found": found, "applied": len(res["applied"]), "skipped": len(res["skipped"])}
