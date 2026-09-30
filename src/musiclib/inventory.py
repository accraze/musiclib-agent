"""M1: walk the source dump and record every file in the state DB. Read-only on the source."""

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .scan import AUDIO_EXTS, scan_file

COMMIT_EVERY = 250

FILE_COLUMNS = [
    "path", "top_dir", "ext", "size", "mtime", "sha256", "codec", "lossless", "bitrate",
    "bitrate_mode", "sample_rate", "bit_depth", "channels", "duration", "has_art",
    "artist", "albumartist", "album", "title", "track", "disc", "date",
    "mb_trackid", "mb_releasetrackid", "mb_albumid", "mb_artistid", "mb_releasegroupid",
    "acoustid_id", "tags_json", "fp_duration", "fingerprint", "error", "warnings", "scanned_at", "run_id",
]
_UPSERT = (
    f"INSERT INTO files ({', '.join(FILE_COLUMNS)}) VALUES ({', '.join('?' * len(FILE_COLUMNS))}) "
    f"ON CONFLICT(path) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in FILE_COLUMNS if c != "path")
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def walk(root: Path, subdir: str | None = None):
    """Yield (relative path, ext, size, mtime) for every regular file, sorted for stable runs."""
    start = root / subdir if subdir else root
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            try:
                st = full.lstat()
            except OSError:
                continue
            if not full.is_file() or full.is_symlink():
                continue
            ext = full.suffix[1:].lower()
            yield full.relative_to(root).as_posix(), ext, st.st_size, st.st_mtime


def _top_dir(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else ""


def _needs_scan(existing: sqlite3.Row | None, size: int, mtime: float, fingerprint: bool) -> bool:
    if existing is None or existing["size"] != size or existing["mtime"] != mtime:
        return True
    if existing["error"]:
        return True
    return fingerprint and existing["fingerprint"] is None


def _scan_job(source: str, rel: str, do_fingerprint: bool) -> tuple[str, dict]:
    return rel, scan_file(Path(source) / rel, do_fingerprint=do_fingerprint)


def run(conn: sqlite3.Connection, source: Path, *, workers: int, fingerprint: bool = True,
        subdir: str | None = None, limit: int | None = None, progress=sys.stderr) -> dict:
    """Scan the dump. Paths are stored relative to `source`."""
    if not source.is_dir():
        raise SystemExit(f"source_dir not found: {source}")
    args = {"source": str(source), "workers": workers, "fingerprint": fingerprint,
            "subdir": subdir, "limit": limit}
    found = ((rel, _top_dir(rel), ext, size, mtime) for rel, ext, size, mtime in walk(source, subdir))
    # Only a full dump scan can tell that a file is gone; absolute (inbox) rows aren't the dump's (D29).
    prune = subdir is None and limit is None
    return _scan(conn, source, found, args, workers=workers, fingerprint=fingerprint, limit=limit,
                 prune=db.is_dump_path if prune else None, progress=progress)


def scan_root(conn: sqlite3.Connection, root: Path, *, workers: int, fingerprint: bool = True,
              progress=sys.stderr) -> dict:
    """Scan a folder outside the dump (an inbox batch, D29). Paths are stored absolute,
    top_dir is `root` itself. Read-only on files, like run()."""
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a folder: {root}")
    args = {"root": str(root), "workers": workers, "fingerprint": fingerprint}
    found = ((str(root / rel), str(root), ext, size, mtime) for rel, ext, size, mtime in walk(root))
    return _scan(conn, root, found, args, workers=workers, fingerprint=fingerprint,
                 prune=lambda p: p.startswith(f"{root}/"), progress=progress)


def _scan(conn, source: Path, found, args: dict, *, workers: int, fingerprint: bool,
          limit: int | None = None, prune=None, progress=sys.stderr) -> dict:
    """Scan (path, top_dir, ext, size, mtime) entries into `files`/`other_files`. `prune`
    selects existing rows this walk covers; those not found again are deleted."""
    run_id = conn.execute(
        "INSERT INTO runs (command, args, started_at, status) VALUES ('inventory', ?, ?, 'running')",
        (json.dumps(args), now()),
    ).lastrowid
    conn.commit()

    existing = {r["path"]: r for r in conn.execute(
        "SELECT path, size, mtime, error, fingerprint FROM files")}
    seen_audio, seen_other, todo = set(), [], []
    for path, top, ext, size, mtime in found:
        if ext in AUDIO_EXTS:
            seen_audio.add(path)
            if _needs_scan(existing.get(path), size, mtime, fingerprint):
                todo.append((path, top, ext, size, mtime))
        else:
            seen_other.append((path, top, ext, size, run_id))
    unchanged = len(seen_audio) - len(todo)
    if limit:
        todo = todo[:limit]

    conn.executemany(
        "INSERT INTO other_files (path, top_dir, ext, size, run_id) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, run_id=excluded.run_id",
        seen_other,
    )
    removed = 0
    if prune is not None:
        gone = [p for p in existing if prune(p) and p not in seen_audio]
        conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
        removed = len(gone)
    conn.commit()

    total_bytes = sum(t[3] for t in todo)
    print(f"inventory: {len(seen_audio)} audio files found, {len(todo)} to scan "
          f"({total_bytes / 1e9:.1f} GB), {unchanged} unchanged skipped", file=progress, flush=True)

    meta = {t[0]: t for t in todo}
    done = errors = done_bytes = 0
    started = time.monotonic()
    batch = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_scan_job, str(source), t[0], fingerprint) for t in todo]
        for fut in as_completed(futures):
            path, rec = fut.result()
            _, top, ext, size, mtime = meta[path]
            rec.update(path=path, top_dir=top, ext=ext, size=size, mtime=mtime,
                       scanned_at=now(), run_id=run_id)
            batch.append(tuple(rec.get(c) for c in FILE_COLUMNS))
            done += 1
            done_bytes += size
            errors += bool(rec.get("error"))
            if len(batch) >= COMMIT_EVERY:
                conn.executemany(_UPSERT, batch)
                conn.commit()
                batch.clear()
                elapsed = time.monotonic() - started
                rate = done_bytes / elapsed if elapsed else 0
                eta = (total_bytes - done_bytes) / rate if rate else 0
                print(f"  {done}/{len(todo)} files, {done_bytes / 1e9:.1f} GB, "
                      f"{rate / 1e6:.0f} MB/s, {errors} errors, ETA {eta / 60:.0f} min",
                      file=progress, flush=True)
    if batch:
        conn.executemany(_UPSERT, batch)

    elapsed = round(time.monotonic() - started, 1)
    conn.execute("UPDATE runs SET finished_at = ?, status = 'ok' WHERE id = ?", (now(), run_id))
    conn.commit()
    return {"run_id": run_id, "audio_files": len(seen_audio), "scanned": done,
            "errors": errors, "removed": removed, "other_files": len(seen_other),
            "elapsed_s": elapsed}
