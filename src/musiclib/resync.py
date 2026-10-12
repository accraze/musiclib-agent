"""D25: re-fetch MusicBrainz metadata for imported albums whose artist names are in a
non-Latin script, so English aliases (import.languages: [en]) apply. Library only.

beets moves the album's items; files musiclib placed itself (D20 extras) aren't beets items,
so they're moved along to the album's new folder here. Every moved file is logged (D39).
"""

import os
import sqlite3
import unicodedata
from pathlib import Path

from .audit import log_relocate, move_leftovers
from .importer import current_paths, open_library


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
    changed = moved_extras = moved_items = 0
    for album in albums:
        items = list(album.items())
        old = {i.id: os.fsdecode(i.path) for i in items}
        old_dir = Path(old[items[0].id]).parent
        before = album.albumartist
        plugin.albums(lib, [f"id:{album.id}"], move=True, pretend=False, write=True)
        album.load()
        items = list(album.items())
        new_dir = Path(os.fsdecode(items[0].path)).parent
        if album.albumartist != before:
            changed += 1
        why = f"D25: album folder renamed ('{before}' -> '{album.albumartist}')"
        where = {d: s for s, d in current_paths(conn).items()}
        for item in items:
            new = os.fsdecode(item.path)
            if new != old[item.id] and old[item.id] in where:
                log_relocate(conn, where[old[item.id]], new, why, "user")
                moved_items += 1
        moved_extras += move_leftovers(conn, old_dir, new_dir, why, "user")
        conn.commit()
    return {"albums": len(albums), "artist_changed": changed, "items_moved": moved_items,
            "extras_moved": moved_extras}
