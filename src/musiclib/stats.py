"""D49: library statistics, as JSON and as a self-contained page for the Library Almanac artifact.

Read-only: beets' database, file sizes on disk and musiclib's own tables. Nothing here changes
the library. The page carries no file paths, so it can be shared."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF"}
TIERS = ("lossless", "256k+", "192k", "128k", "under 128k")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tier(fmt: str, bitrate: int | None) -> str:
    if fmt in LOSSLESS:
        return "lossless"
    b = bitrate or 0
    return "256k+" if b >= 256000 else "192k" if b >= 192000 else "128k" if b >= 128000 else "under 128k"


def _rows(lib, sql: str) -> list:
    with lib.transaction() as tx:
        return tx.query(sql)


def library(lib) -> dict:
    """Everything that comes from beets (and the size of each file it lists)."""
    items = _rows(lib, "SELECT album_id, path, format, bitrate, length FROM items")
    albums = _rows(lib, """SELECT id, albumartist, album, year, genre, albumtype, country, label, comp, added,
                                  mb_albumid FROM albums""")
    fmt_tracks, fmt_bytes, tiers = Counter(), Counter(), Counter()
    album_len, album_tracks, longest_track = defaultdict(float), Counter(), None
    size = 0
    for album_id, path, fmt, bitrate, length in items:
        try:
            n = os.stat(os.fsdecode(path)).st_size
        except OSError:
            n = 0
        fmt = fmt or "?"
        fmt_tracks[fmt] += 1
        fmt_bytes[fmt] += n
        size += n
        tiers[tier(fmt, bitrate)] += 1
        album_len[album_id] += length or 0
        album_tracks[album_id] += 1
        if length and (longest_track is None or length > longest_track[1]):
            longest_track = (album_id, length)

    by_id = {a[0]: a for a in albums}
    name = lambda a: {"artist": a[1], "album": a[2], "year": a[3] or None}  # noqa: E731
    years, artists, genres, types, countries, labels, added = (Counter() for _ in range(7))
    for a in albums:
        _, artist, _, year, genre, albumtype, country, label, comp, when, _ = a
        if year:
            years[year] += 1
        if artist and not comp and artist != "Various Artists":
            artists[artist] += 1
        if genre:
            genres[genre] += 1
        types[albumtype or "unknown"] += 1
        if country:
            countries[country] += 1
        if label and label.lower() not in ("[no label]", "self-released", "not on label"):
            labels[label] += 1
        if when:
            added[datetime.fromtimestamp(when, timezone.utc).date().isoformat()] += 1

    dated = [a for a in albums if a[3]]
    oldest = min(dated, key=lambda a: a[3]) if dated else None
    longest_album = max(album_len, key=album_len.get) if album_len else None
    biggest = max(album_tracks, key=album_tracks.get) if album_tracks else None
    seconds = sum(album_len.values())
    cum, growth = 0, []
    for day in sorted(added):
        cum += added[day]
        growth.append([day, cum])
    return {
        "totals": {"albums": len(albums), "tracks": len(items), "artists": len({a[1] for a in albums if a[1]}),
                   "seconds": round(seconds), "bytes": size,
                   "compilations": sum(1 for a in albums if a[8]),
                   "with_mbid": sum(1 for a in albums if a[10]),
                   "with_genre": sum(genres.values())},
        "formats": [{"format": f, "tracks": n, "bytes": fmt_bytes[f]} for f, n in fmt_tracks.most_common()],
        "quality": [{"tier": t, "tracks": tiers[t]} for t in TIERS],
        "years": sorted([y, n] for y, n in years.items()),
        "artists": [[a, n] for a, n in artists.most_common(20)],
        "genres": [[g, n] for g, n in genres.most_common(15)],
        "types": [[t, n] for t, n in types.most_common()],
        "countries": [[c, n] for c, n in countries.most_common(12)],
        "labels": [[lb, n] for lb, n in labels.most_common(12)],
        "growth": growth,
        "records": {
            "oldest": name(oldest) if oldest else None,
            "longest_album": {**name(by_id[longest_album]), "seconds": round(album_len[longest_album])}
            if longest_album in by_id else None,
            "most_tracks": {**name(by_id[biggest]), "tracks": album_tracks[biggest]} if biggest in by_id else None,
            "longest_track": {**name(by_id[longest_track[0]]), "seconds": round(longest_track[1])}
            if longest_track and longest_track[0] in by_id else None,
        },
    }


def health(conn: sqlite3.Connection, lib, library_dir: Path) -> dict:
    """What musiclib knows about the library's state: the audit, the wanted list, the queues."""
    from . import audit, review, wanted

    audit.migrate(conn)
    decode = audit.decode_findings(conn)
    mb = dict(conn.execute("""SELECT COUNT(*) AS checked, SUM(status = 'changed') AS changed,
                              SUM(status = 'left') AS left FROM audit_mbsync""").fetchone())
    queue = {k: v["open"] for k, v in review.stats(conn).items()}
    return {
        "damaged": decode["damaged"]["count"],
        "decoded": decode["checked"],
        "no_art": len(audit.no_art(lib)),
        "mbsync": {k: v or 0 for k, v in mb.items()},
        "wanted": wanted.counts(conn),
        "review_open": queue,
        "unsorted": audit.unsorted_counts(library_dir),
    }


def collect(conn: sqlite3.Connection, lib, library_dir: Path) -> dict:
    return {"generated": now(), "library": library(lib), "health": health(conn, lib, library_dir)}


def write_page(data: dict, out: Path) -> dict:
    tpl = (Path(__file__).parent / "pages" / "stats.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tpl.replace("__DATA__", blob))
    return {"written": str(out), "albums": data["library"]["totals"]["albums"]}
