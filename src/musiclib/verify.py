"""M2: compare each file's MusicBrainz recording tag with its AcoustID lookup. Read-only on files.

Verdicts (per file):
  confirmed    tag's recording is among AcoustID's matches
  mismatch     AcoustID confidently matched other recordings, not the tagged one
  unverifiable tagged, but AcoustID has no (confident) recording for this audio
  suggest      untagged, AcoustID has a confident recording
  unknown      untagged, no confident AcoustID recording
  no_lookup    no fingerprint or lookup yet
"""

import json
import sqlite3

MIN_SCORE = 0.8  # AcoustID results below this are treated as no match

SCHEMA = """
CREATE TABLE IF NOT EXISTS verify (
    file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    verdict        TEXT NOT NULL,
    tag_recording  TEXT,
    best_recording TEXT,      -- AcoustID's top recording (above MIN_SCORE)
    best_score     REAL,
    candidates     TEXT        -- JSON list of recording ids above MIN_SCORE
);
CREATE INDEX IF NOT EXISTS verify_verdict ON verify(verdict);
"""


def classify(tag: str | None, status: str | None, recordings_json: str | None) -> tuple[str, list[dict]]:
    recs = [r for r in json.loads(recordings_json or "[]") if r["score"] >= MIN_SCORE]
    ids = {r["id"] for r in recs}
    if status is None:
        return "no_lookup", recs
    if tag:
        if tag in ids:
            return "confirmed", recs
        return ("mismatch" if recs else "unverifiable"), recs
    return ("suggest" if recs else "unknown"), recs


def run(conn: sqlite3.Connection, top: int = 10) -> dict:
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM verify")
    rows = conn.execute("""
        SELECT f.id, NULLIF(f.mb_trackid, '') AS tag, a.status, a.recordings
        FROM files f
        LEFT JOIN acoustid_lookups a
          ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
    """).fetchall()
    out = []
    for r in rows:
        verdict, recs = classify(r["tag"], r["status"], r["recordings"])
        best = recs[0] if recs else None
        out.append((r["id"], verdict, r["tag"], best and best["id"], best and best["score"],
                    json.dumps([x["id"] for x in recs])))
    conn.executemany("INSERT INTO verify VALUES (?, ?, ?, ?, ?, ?)", out)
    conn.commit()
    return summary(conn, top)


def summary(conn: sqlite3.Connection, top: int = 10) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM verify").fetchone()[0]
    verdicts = {r[0]: r[1] for r in conn.execute(
        "SELECT verdict, COUNT(*) FROM verify GROUP BY verdict ORDER BY 2 DESC")}

    # Folders where tags look systematically wrong: many mismatches, or one recording ID repeated.
    folders = [dict(r) for r in conn.execute("""
        SELECT rtrim(f.path, replace(f.path, '/', '')) AS folder,
               COUNT(*) AS files,
               SUM(v.verdict = 'mismatch') AS mismatches,
               SUM(v.verdict = 'confirmed') AS confirmed,
               COUNT(v.tag_recording) - COUNT(DISTINCT v.tag_recording) AS repeated_tag_ids
        FROM files f JOIN verify v ON v.file_id = f.id
        GROUP BY folder
        HAVING mismatches > 0 OR repeated_tag_ids > 0
        ORDER BY mismatches + repeated_tag_ids DESC
    """)]
    suspect = [f for f in folders if f["mismatches"] + f["repeated_tag_ids"] >= f["files"] / 2]
    return {
        "files": total,
        "verdicts": {k: {"files": v, "pct": round(100 * v / total, 1)} for k, v in verdicts.items()},
        "folders_with_problems": len(folders),
        "suspect_folders": {"count": len(suspect), "files": sum(f["files"] for f in suspect),
                            "worst": suspect[:top]},
        "mismatch_samples": [dict(r) for r in conn.execute("""
            SELECT f.path, f.artist, f.title, v.tag_recording, v.best_recording, v.best_score
            FROM verify v JOIN files f ON f.id = v.file_id
            WHERE v.verdict = 'mismatch' ORDER BY random() LIMIT ?""", (top,))],
    }
