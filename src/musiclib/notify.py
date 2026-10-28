"""Desktop notifications for the unattended runs (D45): only what is new.

`ingest run --notify` and `audit check --notify` (the systemd services) call these. What was
already told is kept in `notify_seen`, so a batch waiting for review, or the 82 damaged files
the audit already reported, is told once. The first run of each only records what is
already known (the review backlog, the audit's problems). A notification that can't be shown (no desktop session, notify-send missing)
never fails the run.
"""

import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_seen (
    key      TEXT PRIMARY KEY,     -- review:<batch> | audit:<problem> | busy | runner:/audit:baseline
    value    TEXT,                 -- audit: the count told; busy: since when
    seen_at  TEXT NOT NULL
);
"""

# audit summary problems worth a message when they grow; the rest are counts in the report
AUDIT_PROBLEMS = ("originals_missing", "originals_changed", "beets_missing", "logged_missing", "untracked",
                  "modified_since_import", "damaged")
CRITICAL = {"originals_missing", "originals_changed"}  # safety rule 1: should never happen
BUSY_HOURS = 3   # the library lock held this long across runs: say so once
APP = "musiclib"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def desktop(title: str, body: str, urgency: str = "normal") -> str | None:
    """Show one notification; returns why it couldn't be shown, or None."""
    exe = shutil.which("notify-send")
    if not exe:
        return "notify-send not found"
    try:
        r = subprocess.run([exe, "--app-name", APP, "--urgency", urgency, title, body],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return (r.stderr.strip() or f"exit {r.returncode}") if r.returncode else None


def _seen(conn: sqlite3.Connection) -> dict[str, str | None]:
    conn.executescript(SCHEMA)
    return {k: v for k, v in conn.execute("SELECT key, value FROM notify_seen")}


def _mark(conn: sqlite3.Connection, key: str, value=None) -> None:
    conn.execute("INSERT OR REPLACE INTO notify_seen VALUES (?, ?, ?)", (key, None if value is None else str(value), now()))


def _names(folders: list[str], n: int = 3) -> str:
    return ", ".join(folders[:n]) + (f" and {len(folders) - n} more" if len(folders) > n else "")


def runner_messages(conn: sqlite3.Connection, res: dict, *, clock=None) -> list[tuple[str, str, str]]:
    """(title, body, urgency) for what this runner pass did that is new. Records what it told."""
    if "skipped" in res:
        return []
    seen = _seen(conn)
    first = "runner:baseline" not in seen  # the review backlog before D45 was already known
    _mark(conn, "runner:baseline")
    t = clock() if clock else datetime.now(timezone.utc)
    out = []
    albums = sum(r.get("imported", 0) for b in res.get("batches", {}).values()
                 for r in (b.get("imported") or {}).values())
    albums += sum(r.get("imported", 0) for r in (res.get("standing") or {}).get("imported", {}).values())
    if albums:
        folders = sorted({b["folder"] for b in res["batches"].values()})
        out.append(("Imported " + (f"{albums} album" if albums == 1 else f"{albums} albums"),
                    f"From {_names(folders)}.", "low"))
    waiting = res.get("waiting_for_review", [])
    new = [w for w in waiting if f"review:{w['batch']}" not in seen]
    if new and not first:
        out.append((f"{len(new)} new folder{'s need' if len(new) > 1 else ' needs'} /review",
                    _names([w["folder"] for w in new]) + ".", "normal"))
    for w in waiting:
        _mark(conn, f"review:{w['batch']}")
    still = {f"review:{w['batch']}" for w in waiting}
    for k in [k for k in seen if k.startswith("review:") and k not in still]:  # settled: forget
        conn.execute("DELETE FROM notify_seen WHERE key = ?", (k,))
    errs = res.get("errors", []) + [{"step": "claim", "error": f"{n['folder']}: {n['why']}"}
                                    for n in res.get("not_claimed", [])]
    if errs:
        e = errs[0]
        out.append((f"Unattended ingest: {len(errs)} error{'s' if len(errs) > 1 else ''}",
                    f"{e.get('step')}: {e.get('error')}"[:300] + "\nSee state/reports/runner-*.json.", "critical"))
    if res.get("library_busy"):
        since = seen.get("busy") or t.isoformat()
        if "busy" not in seen:
            _mark(conn, "busy", since)
        elif since != "told" and (t - datetime.fromisoformat(since)).total_seconds() >= BUSY_HOURS * 3600:
            out.append(("Unattended ingest is stuck",
                        f"The library lock has been held for {BUSY_HOURS}+ hours; imports are waiting.\n"
                        "state/library.lock", "critical"))
            _mark(conn, "busy", "told")
    elif "busy" in seen:
        conn.execute("DELETE FROM notify_seen WHERE key = 'busy'")
    conn.commit()
    return out


def audit_messages(conn: sqlite3.Connection, summary: dict) -> list[tuple[str, str, str]]:
    """(title, body, urgency) for audit problems that grew since they were last told."""
    seen = _seen(conn)
    first = "audit:baseline" not in seen  # problems known before D45 aren't news
    _mark(conn, "audit:baseline")
    problems = summary.get("problems", {})
    grew = []
    for p in AUDIT_PROBLEMS:
        n = problems.get(p, 0)
        told = int(seen.get(f"audit:{p}") or 0)
        if n > told:
            grew.append((p, n, n - told))
        if n != told:  # falls silently, so a later rise is news again
            _mark(conn, f"audit:{p}", n)
    conn.commit()
    if first or not grew:
        return []
    lines = [f"{p.replace('_', ' ')}: {n} (+{d})" for p, n, d in grew]
    urgent = any(p in CRITICAL for p, _, _ in grew)
    return [("Library audit: new problems" if not urgent else "Library audit: the dump or archive changed",
             "\n".join(lines) + "\nRun /audit to look.", "critical" if urgent else "normal")]


def send(messages: list[tuple[str, str, str]], sender=desktop) -> list[dict]:
    """Show each message; returns what was sent and any failure, for the run's JSON."""
    out = []
    for title, body, urgency in messages:
        failed = sender(title, body, urgency)
        out.append({"title": title, "body": body, **({"failed": failed} if failed else {})})
    return out
