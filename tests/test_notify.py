import sqlite3
from datetime import datetime, timedelta, timezone

from musiclib import notify

T0 = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def _conn():
    return sqlite3.connect(":memory:")


def _run(waiting=(), imported=0, errors=(), busy=None, standing=0):
    res = {"claimed": [], "not_claimed": [], "errors": list(errors), "batches": {},
           "waiting_for_review": [{"batch": b, "folder": f"Folder {b}", "note": "1 album(s) not imported yet"}
                                  for b in waiting]}
    if imported:
        res["batches"][9] = {"folder": "New Album", "status": "imported",
                             "imported": {"auto": {"imported": imported}}}
    if standing:
        res["standing"] = {"imported": {"unsorted": {"imported": standing}}}
    if busy:
        res["library_busy"] = busy
    return res


def test_first_run_records_the_review_backlog_silently():
    conn = _conn()
    assert notify.runner_messages(conn, _run(waiting=[1, 2])) == []
    assert notify.runner_messages(conn, _run(waiting=[1, 2])) == []           # still known
    [(title, body, _)] = notify.runner_messages(conn, _run(waiting=[1, 2, 3]))
    assert title == "1 new folder needs /review" and body == "Folder 3."


def test_imports_are_told_every_time_and_count_standing_approvals():
    conn = _conn()
    notify.runner_messages(conn, _run())
    [(title, body, urgency)] = notify.runner_messages(conn, _run(imported=2, standing=3))
    assert (title, body, urgency) == ("Imported 5 albums", "From New Album.", "low")
    assert notify.runner_messages(conn, _run()) == []                          # quiet hour: nothing


def test_a_settled_batch_is_forgotten_so_it_is_told_again_if_it_comes_back():
    conn = _conn()
    notify.runner_messages(conn, _run())
    assert len(notify.runner_messages(conn, _run(waiting=[4]))) == 1
    notify.runner_messages(conn, _run())
    assert len(notify.runner_messages(conn, _run(waiting=[4]))) == 1


def test_errors_and_failed_claims_are_critical_even_on_the_first_run():
    conn = _conn()
    res = _run(errors=[{"batch": 3, "step": "pipeline", "error": "OSError: disk full"}])
    res["not_claimed"] = [{"folder": "X", "why": "permission denied"}]
    [(title, body, urgency)] = notify.runner_messages(conn, res)
    assert title == "Unattended ingest: 2 errors" and "disk full" in body and urgency == "critical"


def test_a_lock_held_for_hours_is_told_once():
    conn = _conn()
    busy = _run(busy="library lock held")
    assert notify.runner_messages(conn, busy, clock=lambda: T0) == []
    assert notify.runner_messages(conn, busy, clock=lambda: T0 + timedelta(hours=1)) == []
    [(title, _, urgency)] = notify.runner_messages(conn, busy, clock=lambda: T0 + timedelta(hours=3))
    assert title == "Unattended ingest is stuck" and urgency == "critical"
    assert notify.runner_messages(conn, busy, clock=lambda: T0 + timedelta(hours=9)) == []
    notify.runner_messages(conn, _run(), clock=lambda: T0 + timedelta(hours=10))   # lock freed: reset
    assert notify.runner_messages(conn, busy, clock=lambda: T0 + timedelta(hours=11)) == []


def test_a_skipped_run_says_nothing():
    assert notify.runner_messages(_conn(), {"skipped": "another runner is still going"}) == []


def _summary(**problems):
    return {"problems": {"damaged": 82, "originals_missing": 0, "orphan_rows": 10, **problems}}


def test_audit_tells_only_problems_that_grew():
    conn = _conn()
    assert notify.audit_messages(conn, _summary()) == []                      # baseline: 82 known
    assert notify.audit_messages(conn, _summary()) == []
    [(title, body, urgency)] = notify.audit_messages(conn, _summary(damaged=84))
    assert title == "Library audit: new problems" and "damaged: 84 (+2)" in body and urgency == "normal"
    assert notify.audit_messages(conn, _summary(damaged=84)) == []
    assert notify.audit_messages(conn, _summary(damaged=80)) == []           # fewer: silent
    assert "damaged: 81 (+1)" in notify.audit_messages(conn, _summary(damaged=81))[0][1]
    assert notify.audit_messages(conn, _summary(damaged=81, orphan_rows=50)) == []  # not a problem kind


def test_a_changed_dump_is_critical():
    conn = _conn()
    notify.audit_messages(conn, _summary())
    [(title, body, urgency)] = notify.audit_messages(conn, _summary(originals_changed=3))
    assert urgency == "critical" and "dump or archive" in title and "originals changed: 3 (+3)" in body


def test_send_reports_failures_and_never_raises(monkeypatch):
    shown = []
    out = notify.send([("a", "b", "low"), ("c", "d", "critical")],
                      sender=lambda t, b, u: shown.append((t, u)) or ("no display" if u == "critical" else None))
    assert shown == [("a", "low"), ("c", "critical")]
    assert out == [{"title": "a", "body": "b"}, {"title": "c", "body": "d", "failed": "no display"}]
    monkeypatch.setattr(notify.shutil, "which", lambda _: None)
    assert notify.desktop("t", "b") == "notify-send not found"
