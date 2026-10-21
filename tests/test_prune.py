"""D43: pruning settled inbox-archive batches whose library copy is verified."""

import io
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from musiclib import audit, importer, ingest, runner

from test_importer import _snapshot
from test_runner import DAY, LATER, UNMATCHED, _lib_files, _no_lookup, env  # noqa: F401

LATE = lambda: datetime.now(timezone.utc) + timedelta(days=31)  # noqa: E731


def _imported(cfg, conn):
    runner.run(conn, cfg, matcher=lambda s, f, i: dict(UNMATCHED), lookup=_no_lookup, day=DAY, clock=LATER,
               prune_clock=lambda: datetime.now(timezone.utc), progress=io.StringIO())
    b = ingest.batch(conn, 1)
    assert (b["status"], b["note"]) == ("imported", None)
    return Path(b["path"])


def _audited(conn, cfg):
    """What audit check (audit_files) and audit decode (audit_decode) record for the library."""
    audit.migrate(conn)
    for p in cfg.library_dir.rglob("*.mp3"):
        st = p.stat()
        conn.execute("INSERT OR REPLACE INTO audit_files VALUES (?, ?, ?, 'now')", (str(p), st.st_size, st.st_mtime))
        conn.execute("INSERT OR REPLACE INTO audit_decode VALUES (?, ?, ?, 2.0, 2.0, 0, 'now')",
                     (str(p), st.st_size, st.st_mtime))
    conn.commit()


def test_a_recent_batch_is_not_due(env):
    cfg, conn = env
    _imported(cfg, conn)
    _audited(conn, cfg)
    p = ingest.prune(conn, cfg)
    assert p["would_prune"] == [] and p["not_due"] == 1


def test_unaudited_library_copy_holds_the_batch(env):
    cfg, conn = env
    _imported(cfg, conn)
    p = ingest.prune(conn, cfg, clock=LATE)
    assert p["would_prune"] == [] and "not seen unchanged by audit check" in p["held"][0]["why"][0]


def test_verified_batch_is_pruned_and_logged(env):
    cfg, conn = env
    root = _imported(cfg, conn)
    _audited(conn, cfg)
    lib_before = _snapshot(cfg.library_dir)
    (root / "folder.jpg").write_bytes(b"art")               # non-audio files go with it (D43)
    dry = ingest.prune(conn, cfg, clock=LATE)
    assert [r["batch"] for r in dry["would_prune"]] == [1] and dry["would_prune"][0]["files"] == 3
    assert root.is_dir()

    res = ingest.prune(conn, cfg, apply=True, clock=LATE)
    assert [r["batch"] for r in res["pruned"]] == [1] and res["freed_bytes"] > 0
    assert not root.exists() and not root.parent.exists()   # empty <date> folder removed too
    assert _snapshot(cfg.library_dir) == lib_before         # the library is untouched
    rows = conn.execute("SELECT source_path, decided_by FROM audit_log WHERE action = 'prune_original'").fetchall()
    assert len(rows) == 3 and {r[1] for r in rows} == {"auto"}
    assert ingest.batch(conn, 1)["pruned_at"]
    assert audit.originals(conn, cfg.source_dir)["missing"]["count"] == 0   # no longer expected
    assert ingest.prune(conn, cfg, clock=LATE)["would_prune"] == []         # once only


def test_a_file_removed_from_the_library_holds_the_batch(env):
    cfg, conn = env
    root = _imported(cfg, conn)
    _audited(conn, cfg)
    dest = sorted(cfg.library_dir.rglob("*.mp3"))[0]
    importer.remove_from_library(conn, str(dest), "test: user removed it", "user")
    p = ingest.prune(conn, cfg, clock=LATE)
    assert p["would_prune"] == [] and p["held"][0]["why"][0].startswith("not in the library (remove_from_library")
    assert root.is_dir()


def test_a_changed_library_copy_holds_the_batch(env):
    cfg, conn = env
    _imported(cfg, conn)
    _audited(conn, cfg)
    dest = sorted(cfg.library_dir.rglob("*.mp3"))[0]
    os.utime(dest, (time.time() + 99, time.time() + 99))
    p = ingest.prune(conn, cfg, clock=LATE)
    assert p["would_prune"] == [] and "not seen unchanged" in p["held"][0]["why"][0]


def test_a_short_decode_holds_the_batch(env):
    cfg, conn = env
    _imported(cfg, conn)
    _audited(conn, cfg)
    conn.execute("UPDATE audit_decode SET decoded = 0.5")
    conn.commit()
    assert "decodes short" in ingest.prune(conn, cfg, clock=LATE)["held"][0]["why"][0]


def test_an_unsettled_batch_is_left_alone(env):
    cfg, conn = env
    _imported(cfg, conn)
    _audited(conn, cfg)
    conn.execute("UPDATE ingest_batches SET note = '1 album(s) not imported yet (review or pending)'")
    conn.commit()
    p = ingest.prune(conn, cfg, clock=LATE)
    assert p["would_prune"] == [] and p["unsettled"] == 1


def test_a_batch_outside_the_archive_is_never_deleted(env, tmp_path):
    cfg, conn = env
    root = _imported(cfg, conn)
    _audited(conn, cfg)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    conn.execute("UPDATE ingest_batches SET path = ?", (str(elsewhere),))
    conn.commit()
    p = ingest.prune(conn, cfg, apply=True, clock=LATE)
    assert p["pruned"] == [] and "not under" in p["held"][0]["why"][0] and elsewhere.is_dir()


def test_restore_and_redo_refuse_after_prune(env):
    cfg, conn = env
    root = _imported(cfg, conn)
    _audited(conn, cfg)
    ingest.prune(conn, cfg, apply=True, clock=LATE)
    with pytest.raises(SystemExit, match="pruned from the inbox archive"):
        ingest.refuse_pruned(conn, [str(root / "01 Song 1.mp3")], "restore")
    ingest.refuse_pruned(conn, ["/somewhere/else.mp3"], "restore")          # other originals pass


def test_runner_prunes_on_the_timer(env):
    cfg, conn = env
    root = _imported(cfg, conn)
    _audited(conn, cfg)
    res = runner.run(conn, cfg, matcher=lambda s, f, i: dict(UNMATCHED), lookup=_no_lookup, day=DAY,
                     clock=LATER, prune_clock=LATE, progress=io.StringIO())
    assert [r["batch"] for r in res["pruned"]] == [1] and not root.exists() and "written" in res
    assert len(_lib_files(cfg)) == 2
