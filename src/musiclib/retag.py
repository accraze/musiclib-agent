"""Relabel imported albums whose tags sit on the wrong audio, using fingerprints.

beets pairs files with release tracks by title tag, so an album whose files carry each
other's titles (e.g. Potshot, Till I Die) imports with every title on the wrong song. The
AcoustID recording (or title) of each file says which release track it really is. We only
act on a clean one-to-one pairing; anything ambiguous is reported, not changed. Library only.

A track can also be held by the wrong audio while the right audio sits beside it as an extra
(D20): a bonus track tagged with that track's title won the pairing. Then the extra is promoted
onto the track and the displaced file goes back into the folder as an extra, from the dump.
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


def _extra_evidence(conn: sqlite3.Connection, rel: str) -> tuple[set[str], list[str]]:
    """Like _fingerprint_ids, but a file whose tag AcoustID confirmed also answers to its own
    title (verify only fetches AcoustID titles for files that disagree with their tags)."""
    recs, titles = _fingerprint_ids(conn, rel)
    if not titles:
        row = conn.execute("SELECT f.title FROM files f JOIN verify v ON v.file_id = f.id "
                           "WHERE f.path = ? AND v.verdict = 'confirmed'", (rel,)).fetchone()
        titles = [row[0]] if row and row[0] else []
    return recs, titles


def _fits(tracks, recs: set[str], titles: list[str]) -> list:
    return [t for t in tracks if t.track_id in recs] or \
           [t for t in tracks if any(titles_agree(t.title, a) for a in titles)]


def extra_rels(conn: sqlite3.Connection, files: list[str]) -> list[str]:
    """The album's files that sit in its folder as extras (not beets items) right now."""
    from .importer import placements

    return [rel for rel, (_, action) in placements(conn, files).items() if action == "import_extra"]


def promotion_count(conn: sqlite3.Connection, files: list[str]) -> int:
    """Extras whose audio answers to the title of one of the album's imported files: the
    extra may be the real track and the imported file a stray that carries its title."""
    extras = set(extra_rels(conn, files))
    if not extras:
        return 0
    marks = ",".join("?" * len(files))
    own = [r["title"] for r in conn.execute(f"SELECT path, title FROM files WHERE path IN ({marks})", files)
           if r["path"] not in extras and r["title"]]
    return sum(1 for rel in extras
               if any(titles_agree(t, o) for t in _extra_evidence(conn, rel)[1] for o in own))


def swap_count(conn: sqlite3.Connection, files: list[str]) -> int:
    """Mismatched files whose audio fingerprints as *another track of the same album*."""
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
    return swapped


def swap_suspects(conn: sqlite3.Connection, min_swapped: int = 2) -> list[dict]:
    """Imported albums with swapped-track evidence, or extras that may be the real track."""
    out = []
    for m in conn.execute("""
        SELECT m.id, m.album_key, m.files FROM matches m
        JOIN imports i ON i.match_id = m.id AND i.status = 'imported' AND i.mode = 'apply'"""):
        files = json.loads(m["files"])
        swapped, promotable = swap_count(conn, files), promotion_count(conn, files)
        if swapped >= min_swapped or promotable:
            out.append({"album_key": m["album_key"], "files": len(files), "swapped": swapped,
                        "extras_fitting": promotable})
    return out


def after_import(conn: sqlite3.Connection, lib, album_key: str, files: list[str]) -> str | None:
    """D23: relabel by fingerprint right after import when the pairing is clean; otherwise
    return a note so the album is flagged. None when there is no swap evidence."""
    if swap_count(conn, files) < 2 and not promotion_count(conn, files):
        return None
    p = plan(conn, lib, album_key)
    if not p["changes"]:
        return None
    if p["problems"]:
        return f"swapped-track evidence but no clean pairing: run `musiclib retag --album` ({len(p['problems'])} issues)"
    notes = []
    if any(not c.get("promote") for c in p["changes"]):
        n = apply(conn, lib, album_key, "auto", "D23: tags were on the wrong audio (fingerprint and length agree)",
                  promote=False)
        notes.append(f"D23 relabeled {n['retagged']} file(s) by fingerprint")
    if p["_promote"]:  # not covered by D23: flagged, never applied automatically
        notes.append(f"{len(p['_promote'])} extra(s) are the real audio of a track: run `musiclib retag --album`")
    return "; ".join(notes)


def plan(conn: sqlite3.Connection, lib, album_key: str, force: list[str] = ()) -> dict:
    """Which library items should move to which release track, and which extras should
    replace the item holding a track, by fingerprint and length. `force` names extras (library
    file name or dump path) the user has chosen to promote: D27 waives the length check for them."""
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

    lengths = dict(conn.execute("SELECT path, duration FROM files WHERE path IN (SELECT value FROM json_each(?))",
                                (m["files"],)).fetchall())
    assign, problems, holders = {}, [], {}
    for rel, path in dest.items():
        item = by_path.get(path)
        if item is None:
            continue
        current = next((t for t in info.tracks if t.track_id == item.mb_trackid), None)
        if current is not None:
            holders[current.track_id] = rel
        recs, titles = _fingerprint_ids(conn, rel)
        cands = [t for t in info.tracks if t.track_id in recs] or \
                [t for t in info.tracks if any(titles_agree(t.title, a) for a in titles)]
        if current is not None and any(t.track_id == current.track_id for t in cands):
            assign[path] = (item, current, current)  # its audio fits its tag: leave it
        elif len(cands) == 1:
            assign[path] = (item, current, cands[0])
        elif not recs and not titles:
            assign[path] = (item, current, current)  # no fingerprint data: leave as is
        else:
            problems.append(f"{Path(path).name}: {len(cands)} candidate tracks")
    # Independent evidence: a moved file's length must fit its new track better than its old one.
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
    moving = {rel_of[p] for p, (_, c, t) in assign.items()
              if t is not None and (c is None or c.track_id != t.track_id)}
    promote = _promotions(conn, info.tracks, dest, holders, moving, lengths, problems, force)
    changes += [{"file": Path(dest[e]).name, "from": None, "to": t.title, "to_track": t.index, "promote": True,
                 "replaces": Path(dest[x]).name, "forced": _named(e, dest, force),
                 "lengths": {"track": round(t.length or 0), "file": round(lengths.get(e) or 0),
                             "replaced": round(lengths.get(x) or 0)}} for e, t, x in promote]
    return {"album_key": album_key, "release": m["album_id"], "changes": changes, "problems": problems,
            "_info": info, "_assign": assign, "_promote": promote}


def _named(rel: str, dest: dict[str, str], names) -> bool:
    return rel in names or Path(dest[rel]).name in names


def _promotions(conn, tracks, dest: dict[str, str], holders: dict[str, str], moving: set[str],
                lengths: dict[str, float], problems: list[str], force=()) -> list[tuple[str, object, str]]:
    """(extra, release track, file holding it now) for each extra that is that track's audio
    while the holder is not. The extra must fingerprint as the track; when the holder does too
    (a single edit, an alternate take) only a clearly closer length decides. A forced extra
    (D27, the user's choice) still needs a fingerprint naming exactly one held track, not length."""
    out = []
    extras = extra_rels(conn, list(dest))
    for name in force:
        if not any(_named(r, dest, [name]) for r in extras):
            problems.append(f"{name}: not an extra of this album")
    for rel in extras:
        forced = _named(rel, dest, force)
        cands = _fits(tracks, *_extra_evidence(conn, rel))
        if len(cands) != 1 or cands[0].track_id not in holders:
            if forced:
                problems.append(f"{Path(dest[rel]).name}: fingerprint fits {len(cands)} release tracks"
                                + (" but no file holds it" if len(cands) == 1 else ""))
            continue  # fits no track, several, or one nothing holds: a bonus track, leave it
        t, holder = cands[0], holders[cands[0].track_id]
        if holder in moving:
            problems.append(f"{Path(dest[rel]).name}: fits '{t.title}', whose file is also being relabeled")
            continue
        if forced:
            out.append((rel, t, holder))
            continue
        holder_fits = t in _fits(tracks, *_extra_evidence(conn, holder))
        de, dh = lengths.get(rel) or 0, lengths.get(holder) or 0
        if not t.length:
            if not holder_fits:
                out.append((rel, t, holder))
            continue
        off_e, off_h = abs(de - t.length), abs(dh - t.length)
        if holder_fits:
            if off_e + 3 < off_h:
                out.append((rel, t, holder))
        elif off_e > off_h + 3:
            problems.append(f"{Path(dest[rel]).name}: fingerprints as '{t.title}' ({t.length:.0f}s) but its "
                            f"length {de:.0f}s fits worse than the current file's {dh:.0f}s")
        else:
            out.append((rel, t, holder))
    targets = [t.track_id for _, t, _ in out]
    if len(targets) != len(set(targets)):
        problems.append("two extras fit the same release track")
    return out


def tmp_name(path: Path) -> Path:
    """Temporary name that keeps the real extension (beets builds the final name from it)."""
    return path.with_name(path.stem + ".retag-tmp" + path.suffix)


def apply(conn: sqlite3.Connection, lib, album_key: str, decided_by: str, reason: str, *,
          source: Path | None = None, promote: bool = True, force: list[str] = ()) -> dict:
    from beets import autotag
    from beets.library import Item

    if force and decided_by != "user":
        raise SystemExit("--promote is the user's call (D27): use --by user")
    p = plan(conn, lib, album_key, force)
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
    promoted = 0
    if promote and p["_promote"]:
        if source is None:
            raise SystemExit("promoting extras needs the dump (source_dir) to re-place displaced files")
        from .importer import place_extras

        dest = current_paths(conn)
        by_path = {os.fsdecode(i.path): i for i in lib.items()}
        for extra, tr, holder in p["_promote"]:
            old = by_path[dest[holder]]
            album_id, old_path = old.album_id, Path(dest[holder])
            old.remove(delete=True)  # beets DB and the library copy; the dump file stays
            (_, back), = place_extras(old_path.parent, [(source / holder, holder)], move=False)
            new = Item.from_path(dest[extra])
            new.album_id = album_id
            lib.add(new)
            autotag.apply_metadata(p["_info"], [(new, tr)])
            new.try_write()
            new.move()
            new.store()
            conn.executemany(
                "INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(now(), "remove_from_library", holder, str(old_path),
                  f"{reason}: held '{tr.title}' (track {tr.index}) but is other audio", decided_by),
                 (now(), "import_extra", holder, back,
                  f"{reason}: displaced from '{tr.title}': kept in the album folder, tags untouched", decided_by),
                 (now(), "retag_by_fingerprint", extra, os.fsdecode(new.path),
                  f"{reason}: extra promoted{' by user choice (D27)' if _named(extra, dest, force) else ''}, "
                  f"audio is '{tr.title}' (track {tr.index})", decided_by)])
            conn.commit()
            promoted += 1
    return {"album_key": album_key, "retagged": len(moving), "promoted": promoted}
