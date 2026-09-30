"""M5: ingest new folders from the inbox (SPEC Workflow 2, D27-D32).

  list    folders waiting in inbox_dir, and batches already claimed
  claim   rename a folder to inbox/.processed/<date>/<folder> (D28); dry run without apply.
          From then on the folder is read-only, like the dump (D27), and its files are
          recorded with absolute paths (D29).
  scan    inventory + AcoustID + verify, on the batch's files only

Later steps (dedupe against the library, match, import) reuse the dump pipeline.
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import acoustid, inventory, verify
from .config import Config
from .scan import AUDIO_EXTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_batches (
    id          INTEGER PRIMARY KEY,
    folder      TEXT NOT NULL,          -- name as dropped into the inbox
    path        TEXT NOT NULL UNIQUE,   -- absolute, under inbox/.processed/<date>/ (D28)
    status      TEXT NOT NULL,          -- claimed | scanned | deduped | matched | imported
    claimed_at  TEXT NOT NULL,
    claimed_by  TEXT NOT NULL,
    note        TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _inbox(cfg: Config) -> Path:
    if cfg.inbox_dir is None:
        raise SystemExit("inbox_dir is not set in musiclib.toml")
    return cfg.inbox_dir


def _tally(folder: Path) -> dict:
    audio = size = other = 0
    for dirpath, _, names in os.walk(folder):
        for n in names:
            p = Path(dirpath) / n
            if p.is_symlink() or not p.is_file():
                continue
            size += p.stat().st_size
            if p.suffix[1:].lower() in AUDIO_EXTS:
                audio += 1
            else:
                other += 1
    return {"audio_files": audio, "other_files": other, "bytes": size}


def listing(conn: sqlite3.Connection, cfg: Config) -> dict:
    migrate(conn)
    inbox = _inbox(cfg)
    waiting, ignored = [], []
    if inbox.is_dir():
        for p in sorted(inbox.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_symlink() or not p.is_dir():
                ignored.append({"name": p.name, "why": "not a folder: put loose files in a folder"})
                continue
            waiting.append({"folder": p.name, **_tally(p)})
    batches = [dict(r) for r in conn.execute(
        "SELECT id, folder, path, status, claimed_at, note FROM ingest_batches ORDER BY id DESC")]
    return {"inbox": str(inbox), "exists": inbox.is_dir(), "waiting": waiting,
            "ignored": ignored, "batches": batches}


def _target(cfg: Config, folder: str, day: str) -> Path:
    """inbox/.processed/<day>/<folder>, with ' (2)' etc. if that name is taken."""
    base = cfg.processed_dir / day / folder
    dest, n = base, 2
    while dest.exists():
        dest = base.with_name(f"{folder} ({n})")
        n += 1
    return dest


def claim(conn: sqlite3.Connection, cfg: Config, folder: str, *, apply: bool = False,
          decided_by: str = "user", day: str | None = None) -> dict:
    """D28: move a dropped folder into .processed/<date>/ before anything else touches it.
    A rename inside the inbox: same filesystem, instant, reversible. Logged."""
    migrate(conn)
    inbox = _inbox(cfg)
    src = inbox / folder
    if "/" in folder or folder.startswith(".") or not folder:
        raise SystemExit(f"not an inbox folder name: {folder!r}")
    if src.is_symlink() or not src.is_dir():
        raise SystemExit(f"no such folder in the inbox: {src}")
    dest = _target(cfg, folder, day or date.today().isoformat())
    plan = {"folder": folder, "from": str(src), "to": str(dest), **_tally(src)}
    if not plan["audio_files"]:
        raise SystemExit(f"{src} has no audio files")
    if not apply:
        return {"dry_run": True, **plan}
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)  # never a copy+delete: fails across filesystems instead
    with conn:
        bid = conn.execute(
            "INSERT INTO ingest_batches (folder, path, status, claimed_at, claimed_by) "
            "VALUES (?, ?, 'claimed', ?, ?)", (folder, str(dest), now(), decided_by)).lastrowid
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                     "VALUES (?, 'ingest_claim', ?, ?, ?, ?)",
                     (now(), str(src), str(dest), f"claimed as ingest batch {bid} (D28)", decided_by))
    return {"batch": bid, **plan}


def batch(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row:
    """A batch by id, or by folder name when that is unambiguous."""
    migrate(conn)
    rows = conn.execute("SELECT * FROM ingest_batches WHERE id = ? OR folder = ?",
                        (ref if str(ref).isdigit() else -1, str(ref))).fetchall()
    if not rows:
        raise SystemExit(f"no ingest batch {ref!r}")
    if len(rows) > 1:
        raise SystemExit(f"{ref!r} names several batches: use the id ({[r['id'] for r in rows]})")
    return rows[0]


def _set_status(conn: sqlite3.Connection, bid: int, status: str, note: str | None = None) -> None:
    conn.execute("UPDATE ingest_batches SET status = ?, note = ? WHERE id = ?", (status, note, bid))
    conn.commit()


def scan(conn: sqlite3.Connection, cfg: Config, ref: str | int, *, workers: int,
         lookup=acoustid.run, progress=sys.stderr) -> dict:
    """Inventory, AcoustID lookups and verify verdicts for one batch. Read-only on files."""
    b = batch(conn, ref)
    root = Path(b["path"])
    if not root.is_dir():
        raise SystemExit(f"batch folder is missing: {root}")
    inv = inventory.scan_root(conn, root, workers=workers, progress=progress)
    looked = lookup(conn, cfg.acoustid_key, progress=progress)
    verdicts = verify.run_under(conn, str(root))
    errors = [dict(r) for r in conn.execute(
        "SELECT path, error FROM files WHERE top_dir = ? AND error IS NOT NULL", (str(root),))]
    _set_status(conn, b["id"], "scanned")
    return {"batch": b["id"], "folder": b["folder"], "path": str(root),
            "audio_files": inv["audio_files"], "scanned": inv["scanned"], "scan_errors": errors,
            "acoustid": {k: v for k, v in looked.items() if k != "elapsed_s"}, "verify": verdicts}
