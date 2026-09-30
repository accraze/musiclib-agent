"""M4: the review queue. The agent reads batches with context, proposes decisions, and
records them only after the user approves the batch (SPEC safety rule 7).

Kinds (albums with action 'review' or 'error' and no decision yet):
  close  best candidate distance < 0.1
  weak   0.1 <= distance < 0.5
  none   distance >= 0.5 or no candidate: effectively unmatched (D13 Unsorted/)
  dupe   waiting on a duplicate review group
  error  unreadable files

Decisions: approve (import with a release pinned), asis (Unsorted/, D13), skip (leave out).
"""

import json
import sqlite3
from collections import Counter

from .verify import titles_agree
from datetime import datetime, timezone

from .importer import migrate
from .verify import SCHEMA as VERIFY_SCHEMA

# An album is in a duplicate review group if any of its folders is a member of one
# (e.g. the losing copy of a tier-3 edition pair). Such albums wait in `dupe` whatever their
# match distance, so a second copy can't be approved by accident.
IN_DUPE = """(COALESCE(m.note, '') LIKE '%duplicate review%' OR EXISTS (
    SELECT 1 FROM json_each(m.dirs) d
    JOIN dupe_members dm ON dm.path = d.value
    JOIN dupe_groups dg ON dg.id = dm.group_id AND dg.action = 'review'))"""
KIND_SQL = {
    "close": f"m.action = 'review' AND NOT {IN_DUPE} AND m.distance < 0.1",
    "weak": f"m.action = 'review' AND NOT {IN_DUPE} AND m.distance >= 0.1 AND m.distance < 0.5",
    "none": f"m.action = 'review' AND NOT {IN_DUPE} AND (m.distance >= 0.5 OR m.distance IS NULL)",
    "dupe": f"m.action = 'review' AND {IN_DUPE}",
    "error": "m.action = 'error'",
}
# Penalties that don't question *which* release it is: naming, dates, pressing details.
COSMETIC = {"artist", "album", "year", "country", "media", "label", "catalognum",
            "albumdisambig", "albumstatus", "mediums", "tracks", "track_title", "track_artist"}
DECISIONS = {"approve", "asis", "skip"}

# D21: standing user approval for close calls that meet ALL of these. Anything else is asked.
D21 = {"max_distance": 0.1, "min_gap": 0.15, "min_confirmed": 0.9, "max_mismatch": 1,
       "max_missing_tracks": 1, "max_extra_files": 2,
       "plausible_runner_up": 0.35}  # a runner-up this close that fits exactly is worth asking about
D21_PENALTIES = COSMETIC | {"missing_tracks", "unmatched_tracks"}
# D22: exact tracklist fits where AcoustID simply has no data (obscure releases).
# D26 extends the same rule to distances up to 0.2 (weak matches just over the close line).
D22 = {"max_distance": 0.2, "min_gap": 0.3}
VIDEO_EXTS = {"mp4", "m4v", "mkv", "webm", "mov", "avi"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def suggest(kind: str, cands: list[dict]) -> tuple[str, str]:
    """A starting point for the agent, never applied on its own."""
    if kind == "none":
        return "asis", "no candidate closer than 0.5"
    if kind == "error":
        return "skip", "unreadable files"
    if not cands:
        return "asis", "no candidates"
    best = cands[0]
    gap = (cands[1]["distance"] - best["distance"]) if len(cands) > 1 else 1.0
    penalties = set(best["penalties"])
    if (kind == "close" and gap >= 0.15 and penalties <= COSMETIC
            and not best["extra_items"] and not best["extra_tracks"]):
        return "approve", f"clear winner (gap {gap:.2f}), cosmetic penalties only: {sorted(penalties)}"
    reasons = []
    if best["extra_items"]:
        reasons.append(f"{best['extra_items']} of our files don't fit the release")
    if best["extra_tracks"]:
        reasons.append(f"release has {best['extra_tracks']} tracks we lack")
    if gap < 0.15:
        reasons.append(f"runner-up is close (gap {gap:.2f})")
    if penalties - COSMETIC:
        reasons.append(f"structural penalties {sorted(penalties - COSMETIC)}")
    return "look", "; ".join(reasons) or "weak match"


def _local(conn: sqlite3.Connection, files: list[str]) -> dict:
    """What the files themselves say, plus the M2 fingerprint verdicts."""
    marks = ",".join("?" * len(files))
    rows = conn.execute(f"""
        SELECT f.path, f.artist, f.albumartist, f.album, f.title, f.date, f.codec, f.bitrate,
               f.duration, v.verdict
        FROM files f LEFT JOIN verify v ON v.file_id = f.id WHERE f.path IN ({marks})""", files).fetchall()

    def top(field):
        c = Counter(r[field] for r in rows if r[field])
        return c.most_common(1)[0][0] if c else None

    return {
        "artist": top("albumartist") or top("artist"), "album": top("album"), "date": top("date"),
        "codec": top("codec"), "minutes": round(sum(r["duration"] or 0 for r in rows) / 60, 1),
        "verify": dict(Counter(r["verdict"] or "none" for r in rows)),
        "sample": [{"file": r["path"].rsplit("/", 1)[-1], "title": r["title"]} for r in rows[:8]],
    }


def _ensure(conn: sqlite3.Connection) -> None:
    from .acoustid import migrate as acoustid_migrate
    from .dupes import SCHEMA as DUPES_SCHEMA
    acoustid_migrate(conn)
    migrate(conn)
    conn.executescript(VERIFY_SCHEMA)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "dupe_groups" not in have:
        conn.executescript(DUPES_SCHEMA)


def listing(conn: sqlite3.Connection, kind: str, limit: int = 20, offset: int = 0) -> dict:
    _ensure(conn)
    rows = conn.execute(f"""
        SELECT m.* FROM matches m WHERE ({KIND_SQL[kind]}) AND m.decision IS NULL
        ORDER BY m.distance, m.album_key LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM matches m WHERE ({KIND_SQL[kind]}) AND m.decision IS NULL").fetchone()[0]
    in_library = _imported_releases(conn)
    albums = []
    for m in rows:
        cands = json.loads(m["candidates"] or "[]")
        action, why = suggest(kind, cands)
        # A duplicate needs evidence: the best match (or one tied with it) is an imported release
        # AND this album shares audio with that import. A poor best candidate that happens to be
        # imported (Hy Brazil Vol 8 as the closest thing to Vol 5) is no signal on its own.
        best = cands[0]["distance"] if cands else None
        files = json.loads(m["files"])
        already = sorted({in_library[c["album_id"]] for c in cands
                          if c.get("album_id") in in_library and c["distance"] <= best + 0.05
                          and _shares_audio(conn, files, in_library[c["album_id"]])})
        if already:
            action, why = "skip", f"a candidate release is already in the library (from {already[0]})"
        albums.append({"album_key": m["album_key"], "files": len(json.loads(m["files"])),
                       "local": _local(conn, json.loads(m["files"])), "candidates": cands,
                       "note": m["note"], "suggest": action, "why": why, "already_in_library": already})
    return {"kind": kind, "remaining": total, "offset": offset, "albums": albums}


def _audio(conn: sqlite3.Connection, files: list[str]) -> set[str]:
    marks = ",".join("?" * len(files))
    return {r[0] for r in conn.execute(f"""SELECT a.acoustid_id FROM files f JOIN acoustid_lookups a
        ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
        WHERE f.path IN ({marks}) AND a.acoustid_id IS NOT NULL""", files)}


def _shares_audio(conn: sqlite3.Connection, files: list[str], other_key: str, min_share: float = 0.5) -> bool:
    """True when at least half of this album's identified audio is in the other album. Albums
    without any AcoustID data can't be ruled out, so they count as sharing."""
    other = conn.execute("SELECT files FROM matches WHERE album_key = ?", (other_key,)).fetchone()
    mine = _audio(conn, files)
    if not mine or other is None:
        return True
    return len(mine & _audio(conn, json.loads(other[0]))) >= min_share * len(mine)


def _imported_releases(conn: sqlite3.Connection) -> dict[str, str]:
    """release id -> album folder it was imported from."""
    return {r[0]: r[1] for r in conn.execute("""
        SELECT i.album_id, m.album_key FROM imports i JOIN matches m ON m.id = i.match_id
        WHERE i.status = 'imported' AND i.album_id IS NOT NULL""")}


def stats(conn: sqlite3.Connection) -> dict:
    _ensure(conn)
    out = {}
    for kind, where in KIND_SQL.items():
        r = conn.execute(f"""SELECT COUNT(*) AS total, SUM(m.decision IS NULL) AS open,
                             SUM(m.decision = 'approve') AS approve, SUM(m.decision = 'asis') AS asis,
                             SUM(m.decision = 'skip') AS skip FROM matches m WHERE {where}""").fetchone()
        out[kind] = {k: r[k] or 0 for k in r.keys()}
    return out


def decide(conn: sqlite3.Connection, decisions: list[dict], decided_by: str) -> dict:
    """Record a batch. Each: {album_key, decision, album_id?, reason}. All-or-nothing."""
    migrate(conn)
    if decided_by not in ("agent", "user"):
        raise SystemExit("decided_by must be 'agent' or 'user'")
    rows = []
    for d in decisions:
        key, decision = d.get("album_key"), d.get("decision")
        if decision not in DECISIONS:
            raise SystemExit(f"{key}: decision must be one of {sorted(DECISIONS)}")
        m = conn.execute("SELECT id, album_id, candidates FROM matches WHERE album_key = ?", (key,)).fetchone()
        if m is None:
            raise SystemExit(f"unknown album_key: {key}")
        if conn.execute("SELECT 1 FROM imports WHERE match_id = ? AND status = 'imported'", (m["id"],)).fetchone():
            raise SystemExit(f"{key}: already imported")
        album_id = d.get("album_id") or (m["album_id"] if decision == "approve" else None)
        if decision == "approve" and not album_id:
            raise SystemExit(f"{key}: approve needs an album_id (no candidate to default to)")
        if not d.get("reason"):
            raise SystemExit(f"{key}: every decision needs a reason")
        rows.append((m["id"], key, decision, album_id, d["reason"]))
    with conn:
        for mid, key, decision, album_id, reason in rows:
            conn.execute("UPDATE matches SET decision = ?, decided_album_id = ?, decided_by = ? WHERE id = ?",
                         (decision, album_id, decided_by, mid))
            conn.execute("INSERT INTO audit_log (ts, action, source_path, reason, decided_by) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (now(), f"decide_{decision}", key,
                          reason + (f" (release {album_id})" if album_id else ""), decided_by))
    batches = {r[0] for r in conn.execute(
        f"SELECT batch_id FROM matches WHERE batch_id IS NOT NULL AND id IN ({','.join('?' * len(rows))})",
        [r[0] for r in rows])} if rows else set()
    if batches:  # a skip can finish an ingest batch (M5)
        from .ingest import refresh_status
        for bid in batches:
            refresh_status(conn, bid)
    return {"recorded": len(rows), **Counter(r[2] for r in rows)}


VERSION_WORDS = ("instrumental", "acapella", "a cappella")  # "remix" is too noisy (catalog names)


def version_mismatch(local_album: str | None, album_key: str, cand_album: str) -> str | None:
    """'Instrumentals' folder matched to the vocal album (or the reverse): the recordings differ."""
    here = f"{local_album or ''} {album_key}".lower()
    there = (cand_album or "").lower()
    for w in VERSION_WORDS:
        if (w in here) != (w in there):
            return w
    return None


def d21_check(album: dict, files_meta: list[tuple]) -> tuple[bool, str]:
    """(qualifies, why) under D21, or else D22. `album` is a listing entry;
    files_meta = [(ext, error), ...]."""
    if album["candidates"]:
        w = version_mismatch(album["local"].get("album"), album.get("album_key", ""), album["candidates"][0]["album"])
        if w:
            return False, f"'{w}' in one title but not the other: likely a different version"
    if album.get("already_in_library"):
        return False, f"release already in the library (from {album['already_in_library'][0]})"
    ok, why = _d21(album, files_meta)
    if ok:
        return ok, why
    ok22, why22 = _d22(album, files_meta)
    return (True, why22) if ok22 else (False, why)


def _d22(album: dict, files_meta: list[tuple]) -> tuple[bool, str]:
    cands, verify, n = album["candidates"], album["local"]["verify"], album["files"]
    if not cands:
        return False, "no candidates"
    best = cands[0]
    gap = (cands[1]["distance"] - best["distance"]) if len(cands) > 1 else 1.0
    checks = [
        best["distance"] < D22["max_distance"],
        best["extra_items"] == 0 and best["extra_tracks"] == 0,
        gap >= D22["min_gap"],
        verify.get("mismatch", 0) == 0,
        set(best["penalties"]) <= COSMETIC,
        not any(ext in VIDEO_EXTS or err for ext, err in files_meta),
    ]
    if not all(checks):
        return False, "not D22"
    rule = "D22" if best["distance"] < 0.1 else "D26"
    return True, (f"{rule}: exact {n}-track fit, d={best['distance']:.3f}, gap {gap:.2f}, 0 mismatch, "
                  f"{verify.get('confirmed', 0)}/{n} confirmed (AcoustID lacks data)")


def _d21(album: dict, files_meta: list[tuple]) -> tuple[bool, str]:
    cands, verify, n = album["candidates"], album["local"]["verify"], max(1, album["files"])
    if not cands:
        return False, "no candidates"
    best = cands[0]
    runner = cands[1] if len(cands) > 1 else None
    gap = (runner["distance"] - best["distance"]) if runner else 1.0
    checks = [
        (best["distance"] < D21["max_distance"], f"distance {best['distance']:.3f}"),
        (gap >= D21["min_gap"], f"gap {gap:.2f}"),
        (verify.get("confirmed", 0) / n >= D21["min_confirmed"], f"{verify.get('confirmed', 0)}/{n} confirmed"),
        (verify.get("mismatch", 0) <= D21["max_mismatch"], f"{verify.get('mismatch', 0)} mismatch"),
        (best["extra_tracks"] <= D21["max_missing_tracks"], f"{best['extra_tracks']} missing tracks"),
        (best["extra_items"] <= D21["max_extra_files"], f"{best['extra_items']} extra files"),
        (set(best["penalties"]) <= D21_PENALTIES, f"penalties {sorted(set(best['penalties']) - D21_PENALTIES)}"),
        (not (runner and runner["distance"] < D21["plausible_runner_up"]
              and not runner["extra_items"] and not runner["extra_tracks"]
              and (best["extra_items"] or best["extra_tracks"])), "runner-up fits the files exactly"),
        (not any(ext in VIDEO_EXTS or err for ext, err in files_meta), "video or scan-error file"),
    ]
    failed = [why for ok, why in checks if not ok]
    if failed:
        return False, "; ".join(failed)
    return True, (f"D21: d={best['distance']:.3f}, gap {gap:.2f}, {verify.get('confirmed', 0)}/{n} confirmed, "
                  f"{best['extra_tracks']} missing, {best['extra_items']} extra")


def d24_check(album: dict, files_meta: list[tuple]) -> tuple[bool, str]:
    """D24: an unmatched album goes to Unsorted/ without asking when no candidate is at all
    plausible. A candidate whose album title matches the folder's album tag means a possible
    partial album, so those are asked about."""
    from .verify import norm_title

    cands = album["candidates"]
    local = norm_title(album["local"].get("album"))
    if cands and cands[0]["distance"] < 0.5:
        return False, "a candidate is closer than 0.5"
    if album.get("already_in_library"):
        return False, "possible duplicate of an imported album"
    if any(ext in VIDEO_EXTS for ext, _ in files_meta):
        return False, "contains video files"
    def contains(big: str, small: str) -> bool:  # whole words, and long enough to mean something
        return len(small) >= 8 and f" {small} " in f" {big} "

    for c in cands:
        cand = norm_title(c["album"])
        if local and cand and (titles_agree(local, cand) or contains(local, cand) or contains(cand, local)):
            return False, f"candidate '{c['album']}' matches the album title: possible partial album"
    best = f"{cands[0]['distance']:.2f} {cands[0]['artist']} - {cands[0]['album']}" if cands else "none"
    return True, f"D24: no plausible candidate (best {best}); Unsorted as-is (D13)"


def auto_approve(conn: sqlite3.Connection, *, kind: str = "close", limit: int = 20,
                 apply: bool = False) -> dict:
    """Split the next `limit` albums of `kind` into standing-approval decisions (D21/D22 for
    close calls, D24 for unmatched) and albums to ask about."""
    if kind not in ("close", "weak", "none"):
        raise SystemExit("auto works on --kind close, weak or none")
    batch = listing(conn, kind, limit)
    auto, ask = [], []
    for a in batch["albums"]:
        files = json.loads(conn.execute("SELECT files FROM matches WHERE album_key = ?",
                                        (a["album_key"],)).fetchone()[0])
        marks = ",".join("?" * len(files))
        meta = [tuple(m) for m in conn.execute(f"SELECT ext, error FROM files WHERE path IN ({marks})", files)]
        ok, why = (d24_check if kind == "none" else d21_check)(a, meta)
        (auto if ok else ask).append({**a, "d21": why})
    recorded = None
    if apply and auto:
        decision = "asis" if kind == "none" else "approve"
        recorded = decide(conn, [{"album_key": a["album_key"], "decision": decision, "reason": a["d21"]}
                                 for a in auto], "agent")
    return {"auto": auto, "ask": ask, "recorded": recorded, "remaining": batch["remaining"]}
