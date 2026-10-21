"""M6 unattended runner (D42): `musiclib ingest run`, started by a systemd user timer.

No LLM and no new judgment. Each run:
  1. claims every inbox folder left unchanged for `quiet_minutes` (claimed by 'auto', D28)
  2. takes each batch it claimed through scan -> dedupe -> match, resuming where an
     earlier run stopped
  3. imports strong matches (D15) and no-candidate albums (D13), as `ingest import` does
  4. applies the standing approvals D21/D22/D24/D26 (D31) to its batches' review albums,
     recorded as decided by 'auto', and imports them
  5. writes state/reports/runner-<time>.json and a manifest per batch it touched

Everything else (dupe kind, upgrades, editions, extra tracks, albums the standing
approvals don't cover) waits in the review queue for /review and /dedupe. Upgrades (D32)
are never applied here. If the library lock is held, imports wait for the next run.
"""

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import acoustid, ingest
from .config import Config

STANDING_KINDS = ("close", "weak", "none")  # D21/D22 + D26, D24; never dupe or error
PIPELINE = ("claimed", "scanned", "deduped", "matched")  # statuses a run picks up


def last_change(folder: Path) -> float:
    """Newest mtime or ctime in the tree. ctime catches copies that restore old mtimes
    (rsync -a, cp -p)."""
    newest = 0.0
    for dirpath, dirnames, names in os.walk(folder):
        for p in [dirpath, *(os.path.join(dirpath, n) for n in names)]:
            try:
                st = os.lstat(p)
            except FileNotFoundError:  # renamed away mid-walk: still being written
                return time.time()
            newest = max(newest, st.st_mtime, st.st_ctime)
    return newest


def waiting(conn: sqlite3.Connection, cfg: Config, quiet_minutes: float, clock=time.time) -> dict:
    """Inbox folders split into ready (quiet long enough) and busy (still changing)."""
    ready, busy = [], []
    cutoff = clock() - quiet_minutes * 60
    for f in ingest.listing(conn, cfg)["waiting"]:
        (ready if last_change(cfg.inbox_dir / f["folder"]) <= cutoff else busy).append(f)
    return {"ready": ready, "busy": busy}


def _mine(conn: sqlite3.Connection, statuses=None) -> list[sqlite3.Row]:
    ingest.migrate(conn)
    sql = "SELECT * FROM ingest_batches WHERE claimed_by = 'auto'"
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
    return conn.execute(sql + " ORDER BY id", statuses or ()).fetchall()


@contextmanager
def runner_lock(state_dir: Path):
    """One runner at a time; a second one (a slow run still going) just exits."""
    import fcntl

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "runner.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def plan(conn: sqlite3.Connection, cfg: Config, *, quiet_minutes: float = 10, clock=time.time) -> dict:
    """Dry run: what the next run would claim and resume. Changes nothing."""
    w = waiting(conn, cfg, quiet_minutes, clock)
    return {"dry_run": True, "would_claim": w["ready"], "still_changing": w["busy"],
            "would_resume": [{"batch": b["id"], "folder": b["folder"], "status": b["status"]}
                             for b in _mine(conn, PIPELINE)]}


def _advance(conn, cfg, b, *, workers, lookup, matcher, progress) -> str:
    """Take one batch as far as match. Returns the status it reached."""
    status = b["status"]
    if status == "claimed":
        ingest.scan(conn, cfg, b["id"], workers=workers, lookup=lookup, progress=progress)
        status = "scanned"
    if status == "scanned":
        ingest.dedupe(conn, b["id"])
        status = "deduped"
    if status == "deduped":
        ingest.match(conn, b["id"], matcher=matcher, progress=progress)
        status = "matched"
    return status


def _standing(conn, cfg, batches: list[int], progress) -> dict:
    """D21/D22/D24/D26 on these batches' open review albums, then import what they approved."""
    from . import importer, review

    out: dict = {"approved": [], "asis": [], "ask": 0, "imported": {}}
    for kind in STANDING_KINDS:  # batches are small: the whole queue for them in one page
        r = review.auto_approve(conn, kind=kind, limit=100_000, apply=True, batches=batches, decided_by="auto")
        out["asis" if kind == "none" else "approved"] += [a["album_key"] for a in r["auto"]]
        out["ask"] += len(r["ask"])
    marks = ",".join("?" * len(batches))
    todo = {w: [k for (k,) in conn.execute(
        f"SELECT album_key FROM matches WHERE decided_by = 'auto' AND decision = ? AND batch_id IN ({marks})",
        (d, *batches))] for w, d in (("approved", "approve"), ("unsorted", "asis"))}
    todo = {w: keys for w, keys in todo.items() if keys and importer.select(conn, w, albums=keys)}
    if todo:  # only then take the lock: most hours there is nothing to import
        with importer.library_lock(cfg.state_dir):
            for which, keys in todo.items():
                out["imported"][which] = importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", which,
                                                      only=keys, progress=progress)
    return out


def run(conn: sqlite3.Connection, cfg: Config, *, quiet_minutes: float = 10, workers: int = 2,
        lookup=acoustid.run, matcher=None, clock=time.time, day: str | None = None,
        progress=sys.stderr) -> dict:
    """One unattended pass (D42). Callers must have run beetsenv.setup()."""
    from .importer import LibraryBusy

    with runner_lock(cfg.state_dir) as mine:
        if not mine:
            return {"skipped": "another runner is still going (state/runner.lock)"}
        started = datetime.now()
        res: dict = {"started": started.isoformat(timespec="seconds"), "claimed": [], "not_claimed": [],
                     "still_changing": [], "batches": {}, "errors": []}

        w = waiting(conn, cfg, quiet_minutes, clock)
        res["still_changing"] = [f["folder"] for f in w["busy"]]
        for f in w["ready"]:
            try:
                c = ingest.claim(conn, cfg, f["folder"], apply=True, decided_by="auto", day=day)
                res["claimed"].append({"batch": c["batch"], "folder": f["folder"]})
            except SystemExit as e:  # no audio files, vanished: leave it in the inbox
                res["not_claimed"].append({"folder": f["folder"], "why": str(e)})

        for b in _mine(conn, PIPELINE):
            try:
                res["batches"][b["id"]] = {"folder": b["folder"],
                                           "status": _advance(conn, cfg, b, workers=workers, lookup=lookup,
                                                              matcher=matcher, progress=progress)}
            except (Exception, SystemExit) as e:
                res["errors"].append({"batch": b["id"], "step": "pipeline", "error": f"{type(e).__name__}: {e}"})

        busy = None
        for b in _mine(conn, ("matched",)):
            try:
                r = ingest.import_batch(conn, cfg, b["id"], apply=True, progress=progress)
                res["batches"].setdefault(b["id"], {"folder": b["folder"]}).update(
                    status="imported", imported={k: r[k] for k in ("auto", "unsorted") if k in r})
            except LibraryBusy as e:
                busy = str(e)
                break
            except (Exception, SystemExit) as e:
                res["errors"].append({"batch": b["id"], "step": "import", "error": f"{type(e).__name__}: {e}"})

        ids = [b["id"] for b in _mine(conn, ("imported",))]
        if ids and not busy:
            try:
                res["standing"] = _standing(conn, cfg, ids, progress)
            except LibraryBusy as e:
                busy = str(e)
            except (Exception, SystemExit) as e:
                res["errors"].append({"step": "standing approvals", "error": f"{type(e).__name__}: {e}"})
        if busy:
            res["library_busy"] = busy + " (imports resume on the next run)"

        res["waiting_for_review"] = [{"batch": b["id"], "folder": b["folder"], "note": b["note"]}
                                     for b in _mine(conn, ("imported",)) if b["note"]]
        touched = sorted(set(res["batches"]) | {c["batch"] for c in res["claimed"]})
        res["manifests"] = {}
        for bid in touched:
            try:
                res["manifests"][bid] = ingest.manifest(conn, cfg, bid)["written"]
            except (Exception, SystemExit) as e:
                res["errors"].append({"batch": bid, "step": "report", "error": f"{type(e).__name__}: {e}"})
        res["finished"] = datetime.now().isoformat(timespec="seconds")
        if touched or res["errors"] or res["not_claimed"] or busy:  # quiet hours leave no files
            cfg.reports_dir.mkdir(parents=True, exist_ok=True)
            out = cfg.reports_dir / f"runner-{started.strftime('%Y-%m-%dT%H%M%S')}.json"
            out.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
            res["written"] = str(out)
        return res
