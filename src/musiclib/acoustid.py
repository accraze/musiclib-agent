"""M2: look up every inventoried fingerprint on AcoustID. Read-only on files; resumable.

Identical fingerprints are looked up once. Requests are batched (fingerprint.N params) and
held under AcoustID's 3 requests/second limit.
"""

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.acoustid.org/v2/lookup"
BATCH = 10
MIN_INTERVAL = 0.4  # seconds between requests (< 3/s)
MAX_RETRIES = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS acoustid_lookups (
    fingerprint   TEXT NOT NULL,
    fp_duration   INTEGER NOT NULL,
    status        TEXT NOT NULL,      -- ok | no_recording | no_match | error
    best_score    REAL,
    acoustid_id   TEXT,               -- best result
    recordings    TEXT,               -- JSON [{"id", "score", "acoustid_id"}], best first
    error         TEXT,
    looked_up_at  TEXT NOT NULL,
    PRIMARY KEY (fingerprint, fp_duration)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_results(results: list[dict]) -> dict:
    """Flatten AcoustID results into best score/id and a de-duplicated recording list."""
    results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
    if not results:
        return {"status": "no_match", "best_score": None, "acoustid_id": None, "recordings": "[]"}
    recs, seen = [], set()
    for r in results:
        for rec in r.get("recordings") or []:
            if rec["id"] not in seen:
                seen.add(rec["id"])
                recs.append({"id": rec["id"], "score": round(r["score"], 4), "acoustid_id": r["id"]})
    return {"status": "ok" if recs else "no_recording", "best_score": round(results[0]["score"], 4),
            "acoustid_id": results[0]["id"], "recordings": json.dumps(recs)}


def _post(key: str, batch: list[tuple[str, int]]) -> list[list[dict]]:
    params = [("client", key), ("meta", "recordingids"), ("format", "json"), ("batch", "1")]
    for i, (fp, dur) in enumerate(batch):
        params += [(f"duration.{i}", str(dur)), (f"fingerprint.{i}", fp)]
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=body, headers={"User-Agent": "musiclib/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if data.get("status") != "ok":
        raise RuntimeError(f"acoustid: {data.get('error')}")
    out: list[list[dict]] = [[] for _ in batch]
    for entry in data.get("fingerprints", []):
        out[int(entry["index"])] = entry.get("results") or []
    return out


def run(conn: sqlite3.Connection, key: str | None, *, limit: int | None = None,
        retry_errors: bool = False, progress=sys.stderr, post=_post, sleep=time.sleep) -> dict:
    if not key:
        raise SystemExit("no AcoustID key: set acoustid_key in musiclib.local.toml or ACOUSTID_KEY")
    conn.executescript(SCHEMA)
    skip = "status != 'error'" if retry_errors else "1"
    todo = conn.execute(f"""
        SELECT DISTINCT f.fingerprint, f.fp_duration FROM files f
        WHERE f.fingerprint IS NOT NULL AND f.fp_duration > 0
          AND NOT EXISTS (SELECT 1 FROM acoustid_lookups a
                          WHERE a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
                            AND {skip})
    """).fetchall()
    todo = [(r[0], r[1]) for r in todo][:limit] if limit else [(r[0], r[1]) for r in todo]
    print(f"acoustid: {len(todo)} distinct fingerprints to look up "
          f"(~{len(todo) / BATCH * MIN_INTERVAL / 60:.0f} min)", file=progress, flush=True)

    counts = {"ok": 0, "no_recording": 0, "no_match": 0, "error": 0}
    last = 0.0
    started = time.monotonic()
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
            last = time.monotonic()
            try:
                results = post(key, batch)
                rows = [(fp, dur, *parse_results(res).values(), None, now())
                        for (fp, dur), res in zip(batch, results)]
                break
            except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
                if "invalid API key" in str(e):
                    raise SystemExit("AcoustID rejected the API key") from e
                if attempt == MAX_RETRIES - 1:
                    rows = [(fp, dur, "error", None, None, None, str(e)[:300], now())
                            for fp, dur in batch]
                else:
                    sleep(2 ** attempt)
        conn.executemany(
            "INSERT OR REPLACE INTO acoustid_lookups (fingerprint, fp_duration, status, best_score, "
            "acoustid_id, recordings, error, looked_up_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        for r in rows:
            counts[r[2]] += 1
        done = start + len(batch)
        if done % 1000 < BATCH or done == len(todo):
            elapsed = time.monotonic() - started
            eta = (len(todo) - done) * elapsed / done if done else 0
            print(f"  {done}/{len(todo)} {counts} ETA {eta / 60:.0f} min", file=progress, flush=True)

    return {"looked_up": len(todo), **counts, "elapsed_s": round(time.monotonic() - started, 1)}
