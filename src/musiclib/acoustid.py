"""M2: look up every inventoried fingerprint on AcoustID. Read-only on files; resumable.

Identical fingerprints are looked up once. Requests are batched (fingerprint.N params) and
held under AcoustID's 3 requests/second limit.
"""

import http.client
import json
import sqlite3
import sys
import time
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
    titles        TEXT,               -- JSON {recording id: {title, artist}}, fetched on demand
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


def _post(key: str, batch: list[tuple[str, int]], meta: str = "recordingids") -> list[list[dict]]:
    params = [("client", key), ("meta", meta), ("format", "json"), ("batch", "1")]
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


def _batches(key, todo, post, sleep, progress, **post_kw):
    """Yield (batch, results or Exception), throttled and retried. Results are None-free lists."""
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
                result = post(key, batch, **post_kw)
                break
            # OSError covers URLError, SSL errors, resets and timeouts.
            except (OSError, http.client.HTTPException, RuntimeError, ValueError) as e:
                if "invalid API key" in str(e):
                    raise SystemExit("AcoustID rejected the API key") from e
                result = e
                if attempt < MAX_RETRIES - 1:
                    sleep(2 ** attempt)
        yield batch, result
        done = start + len(batch)
        if done % 1000 < BATCH or done == len(todo):
            elapsed = time.monotonic() - started
            eta = (len(todo) - done) * elapsed / done if done else 0
            print(f"  {done}/{len(todo)} ETA {eta / 60:.0f} min", file=progress, flush=True)


def _require_key(key: str | None) -> str:
    if not key:
        raise SystemExit("no AcoustID key: set acoustid_key in musiclib.local.toml or ACOUSTID_KEY")
    return key


def run(conn: sqlite3.Connection, key: str | None, *, limit: int | None = None,
        retry_errors: bool = False, progress=sys.stderr, post=_post, sleep=time.sleep) -> dict:
    key = _require_key(key)
    migrate(conn)
    skip = "status != 'error'" if retry_errors else "1"
    todo = [(r[0], r[1]) for r in conn.execute(f"""
        SELECT DISTINCT f.fingerprint, f.fp_duration FROM files f
        WHERE f.fingerprint IS NOT NULL AND f.fp_duration > 0
          AND NOT EXISTS (SELECT 1 FROM acoustid_lookups a
                          WHERE a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
                            AND {skip})
    """)][:limit]
    print(f"acoustid: {len(todo)} distinct fingerprints to look up", file=progress, flush=True)

    counts = {"ok": 0, "no_recording": 0, "no_match": 0, "error": 0}
    started = time.monotonic()
    for batch, results in _batches(key, todo, post, sleep, progress):
        if isinstance(results, Exception):
            rows = [(fp, dur, "error", None, None, None, str(results)[:300], now())
                    for fp, dur in batch]
        else:
            rows = [(fp, dur, *parse_results(res).values(), None, now())
                    for (fp, dur), res in zip(batch, results)]
        conn.executemany(
            "INSERT OR REPLACE INTO acoustid_lookups (fingerprint, fp_duration, status, best_score, "
            "acoustid_id, recordings, error, looked_up_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        for r in rows:
            counts[r[2]] += 1
    return {"looked_up": len(todo), **counts, "elapsed_s": round(time.monotonic() - started, 1)}


def parse_titles(results: list[dict]) -> str:
    """{recording id: {"title", "artist"}} from a meta=recordings lookup."""
    out = {}
    for r in results:
        for rec in r.get("recordings") or []:
            if rec["id"] not in out and rec.get("title"):
                artist = " & ".join(a.get("name", "") for a in rec.get("artists") or [])
                out[rec["id"]] = {"title": rec["title"], "artist": artist}
    return json.dumps(out, ensure_ascii=False)


def fetch_titles(conn: sqlite3.Connection, key: str | None, *, verdicts=("mismatch", "suggest"),
                 progress=sys.stderr, post=_post, sleep=time.sleep) -> dict:
    """Fetch recording titles/artists for lookups behind files with the given verify verdicts."""
    key = _require_key(key)
    migrate(conn)
    marks = ", ".join("?" * len(verdicts))
    todo = [(r[0], r[1]) for r in conn.execute(f"""
        SELECT DISTINCT a.fingerprint, a.fp_duration FROM acoustid_lookups a
        JOIN files f ON f.fingerprint = a.fingerprint AND f.fp_duration = a.fp_duration
        JOIN verify v ON v.file_id = f.id
        WHERE v.verdict IN ({marks}) AND a.titles IS NULL
    """, verdicts)]
    print(f"acoustid titles: {len(todo)} fingerprints", file=progress, flush=True)
    fetched = failed = 0
    for batch, results in _batches(key, todo, post, sleep, progress, meta="recordings"):
        if isinstance(results, Exception):
            failed += len(batch)
            continue
        conn.executemany("UPDATE acoustid_lookups SET titles = ? WHERE fingerprint = ? AND fp_duration = ?",
                         [(parse_titles(res), fp, dur) for (fp, dur), res in zip(batch, results)])
        conn.commit()
        fetched += len(batch)
    return {"fetched": fetched, "failed": failed}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(acoustid_lookups)")}
    if "titles" not in cols:
        conn.execute("ALTER TABLE acoustid_lookups ADD COLUMN titles TEXT")
