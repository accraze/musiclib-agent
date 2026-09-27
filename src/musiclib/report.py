"""Summaries over the inventory. Read-only on everything."""

import json
import sqlite3
from collections import Counter


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def _one(conn: sqlite3.Connection, sql: str, *args):
    return conn.execute(sql, args).fetchone()[0]


def _dupe_groups(conn: sqlite3.Connection, column: str, top: int) -> dict:
    """Groups of files sharing a value in `column`, with bytes reclaimable by keeping one each."""
    stats = conn.execute(f"""
        SELECT COUNT(*) AS groups, COALESCE(SUM(n), 0) AS files,
               COALESCE(SUM(extra), 0) AS extra_files, COALESCE(SUM(waste), 0) AS reclaimable_bytes
        FROM (SELECT COUNT(*) AS n, COUNT(*) - 1 AS extra, SUM(size) - MAX(size) AS waste
              FROM files WHERE {column} IS NOT NULL AND {column} != ''
              GROUP BY {column} HAVING COUNT(*) > 1)
    """).fetchone()
    largest = _rows(conn, f"""
        SELECT {column} AS key, COUNT(*) AS copies, SUM(size) - MAX(size) AS reclaimable_bytes,
               GROUP_CONCAT(path, ' || ') AS paths
        FROM files WHERE {column} IS NOT NULL AND {column} != ''
        GROUP BY {column} HAVING COUNT(*) > 1
        ORDER BY reclaimable_bytes DESC LIMIT ?
    """, top)
    for g in largest:
        g["paths"] = g["paths"].split(" || ")[:6]
    return {**dict(stats), "largest": largest}


def inventory_summary(conn: sqlite3.Connection, top: int = 10) -> dict:
    total = _one(conn, "SELECT COUNT(*) FROM files")
    if not total:
        return {"files": 0, "note": "inventory is empty; run `musiclib inventory` first"}

    def pct(n):
        return round(100 * n / total, 1)

    coverage = {}
    for field in ("artist", "album", "title", "mb_trackid", "mb_albumid", "acoustid_id"):
        n = _one(conn, f"SELECT COUNT(*) FROM files WHERE {field} IS NOT NULL AND {field} != ''")
        coverage[field] = {"files": n, "pct": pct(n)}
    n = _one(conn, "SELECT COUNT(*) FROM files WHERE has_art = 1")
    coverage["embedded_art"] = {"files": n, "pct": pct(n)}
    n = _one(conn, "SELECT COUNT(*) FROM files WHERE fingerprint IS NOT NULL")
    coverage["fingerprint"] = {"files": n, "pct": pct(n)}

    return {
        "files": total,
        "bytes": _one(conn, "SELECT SUM(size) FROM files"),
        "hours": round(_one(conn, "SELECT COALESCE(SUM(duration), 0) FROM files") / 3600, 1),
        "top_level_dirs": _one(conn, "SELECT COUNT(DISTINCT top_dir) FROM files WHERE top_dir != ''"),
        "loose_root_files": _one(conn, "SELECT COUNT(*) FROM files WHERE top_dir = ''"),
        "by_codec": _rows(conn, """
            SELECT COALESCE(codec, '?' || ext) AS codec, COUNT(*) AS files, SUM(size) AS bytes
            FROM files GROUP BY 1 ORDER BY files DESC"""),
        "lossless_bytes": _one(conn, "SELECT COALESCE(SUM(size), 0) FROM files WHERE lossless = 1"),
        "mp3_bitrate": _rows(conn, """
            SELECT CASE
                     WHEN bitrate_mode = 'VBR' THEN 'VBR'
                     WHEN bitrate >= 320000 THEN '320'
                     WHEN bitrate >= 256000 THEN '256-319'
                     WHEN bitrate >= 192000 THEN '192-255'
                     WHEN bitrate >= 128000 THEN '128-191'
                     ELSE '<128' END AS bucket,
                   COUNT(*) AS files
            FROM files WHERE codec = 'mp3' GROUP BY 1 ORDER BY files DESC"""),
        "tag_coverage": coverage,
        "duplicates": {
            "tier1_identical_bytes": _dupe_groups(conn, "sha256", top),
            "tier2_preview_same_fingerprint": _dupe_groups(conn, "fingerprint", top),
            "tier2_preview_same_mb_recording_tag": _dupe_groups(conn, "mb_trackid", top),
        },
        "errors": {
            "files": _one(conn, "SELECT COUNT(*) FROM files WHERE error IS NOT NULL"),
            "samples": _rows(conn, "SELECT path, error FROM files WHERE error IS NOT NULL LIMIT ?", top),
        },
        "warnings": {
            "files": _one(conn, "SELECT COUNT(*) FROM files WHERE warnings IS NOT NULL"),
            "samples": _rows(conn, "SELECT path, warnings FROM files WHERE warnings IS NOT NULL LIMIT ?", top),
        },
        "other_files": _rows(conn, """
            SELECT ext, COUNT(*) AS files, SUM(size) AS bytes FROM other_files
            GROUP BY ext ORDER BY files DESC LIMIT 25"""),
    }


def not_imported(conn: sqlite3.Connection) -> tuple[dict, list[dict]]:
    """D18: every dump audio file with where it went, or why it wasn't imported.

    Statuses: imported, duplicate (dropped; keeper named), review, error, pending (matched
    and ready, not imported yet), unmatched (not in any album, e.g. loose root files).
    """
    status: dict[str, tuple[str, str | None]] = {}
    for r in conn.execute("SELECT source_path, dest_path FROM audit_log WHERE action LIKE 'import%'"):
        status[r[0]] = ("imported", r[1])
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "matches" in have:
        imported = set()
        if "imports" in have:
            imported = {r[0] for r in conn.execute("SELECT match_id FROM imports WHERE status = 'imported'")}
        for m in conn.execute("SELECT id, action, note, files FROM matches"):
            for f in json.loads(m["files"]):
                if f not in status:
                    kind = {"auto": "pending", "unsorted": "pending"}.get(m["action"], m["action"])
                    if m["id"] in imported:
                        kind = "imported"
                    status[f] = (kind, m["note"])
    if "dupe_groups" in have:
        for r in conn.execute("""
            SELECT g.scope, g.keeper, g.reason, m.path FROM dupe_groups g
            JOIN dupe_members m ON m.group_id = g.id WHERE g.action = 'auto' AND m.role = 'drop'"""):
            files = [r["path"]] if r["scope"] == "file" else [
                p[0] for p in conn.execute("SELECT path FROM files WHERE path LIKE ? || '%'", (r["path"],))]
            for f in files:
                if f not in status or status[f][0] != "imported":
                    status[f] = ("duplicate", f"{r['reason']}; kept {r['keeper']}")
    rows = []
    for (path,) in conn.execute("SELECT path FROM files ORDER BY path"):
        kind, detail = status.get(path, ("unmatched", None))
        rows.append({"path": path, "status": kind, "detail": detail})
    counts = dict(sorted(Counter(r["status"] for r in rows).items()))
    return {"files": len(rows), "by_status": counts}, rows
