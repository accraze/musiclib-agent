"""D25: re-fetch MusicBrainz metadata for imported albums whose artist names are in a
non-Latin script, so English aliases (import.languages: [en]) apply. Library only.

beets moves the album's items; files musiclib placed itself (D20 extras) aren't beets items,
so they're moved along to the album's new folder here and logged.
"""

import os
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .importer import current_paths, open_library


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def non_latin(text: str | None) -> bool:
    for ch in text or "":
        if ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN"):
            return True
    return False


def candidates(lib) -> list:
    """Albums with a non-Latin album artist or track artist."""
    out = []
    for album in lib.albums():
        if not album.mb_albumid:
            continue
        if non_latin(album.albumartist) or any(non_latin(i.artist) for i in album.items()):
            out.append(album)
    return out


def run(conn: sqlite3.Connection, *, apply: bool = False, limit: int | None = None) -> dict:
    from beetsplug.mbsync import MBSyncPlugin

    lib = open_library()
    albums = candidates(lib)[:limit]
    if not apply:
        return {"dry_run": True, "albums": len(albums),
                "examples": [f"{a.albumartist} - {a.album}" for a in albums[:15]]}
    plugin = MBSyncPlugin()
    changed = moved_extras = 0
    for album in albums:
        items = list(album.items())
        old_dir = Path(os.fsdecode(items[0].path)).parent
        before = album.albumartist
        plugin.albums(lib, [f"id:{album.id}"], move=True, pretend=False, write=True)
        album.load()
        items = list(album.items())
        new_dir = Path(os.fsdecode(items[0].path)).parent
        if album.albumartist != before:
            changed += 1
        if new_dir != old_dir and old_dir.exists():
            where = {d: s for s, d in current_paths(conn).items()}
            for f in sorted(old_dir.iterdir()):
                if not f.is_file():
                    continue
                dest = new_dir / f.name
                if dest.exists():
                    continue
                shutil.move(str(f), dest)
                if str(f) in where:  # a file musiclib placed: keep the mapping current
                    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                                 "VALUES (?, 'relocate', ?, ?, ?, 'user')",
                                 (now(), where[str(f)], str(dest), f"D25: album folder renamed ('{before}' -> "
                                                                     f"'{album.albumartist}')"))
                    moved_extras += 1
            try:
                old_dir.rmdir()
                old_dir.parent.rmdir()  # artist folder, if now empty
            except OSError:
                pass
        conn.commit()
    return {"albums": len(albums), "artist_changed": changed, "extras_moved": moved_extras}
