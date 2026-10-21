"""D42 unattended runner, offline: quiet period, claim -> pipeline -> imports, standing
approvals as 'auto', library lock back-off, one runner at a time."""

import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from musiclib import beetsenv, config, db, importer, ingest, review, runner

from test_importer import _snapshot

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")), reason="needs ffmpeg and fpcalc")
DAY = "2026-10-21"
LATER = lambda: time.time() + 3600  # noqa: E731  an hour on: every folder is quiet


def _album(inbox, name, freq):
    album = inbox / name
    album.mkdir(parents=True)
    for i in (1, 2):
        p = album / f"0{i} Song {i}.mp3"
        subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", f"sine=f={freq + 100 * i}:d=2",
                        "-y", str(p)], check=True)
        t = EasyID3()
        t.update({"artist": name, "album": "Demo", "title": f"Song {i}", "tracknumber": str(i)})
        t.save(p)
    return album


def _no_lookup(conn, key, progress=None):
    return {"looked_up": 0, "elapsed_s": 0}


@pytest.fixture
def env(tmp_path):
    _album(tmp_path / "inbox", "Band A", 300)
    (tmp_path / "dump").mkdir()
    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{tmp_path}/dump"\nstate_dir = "{tmp_path}/state"\n'
        f'library_dir = "{tmp_path}/lib"\ninbox_dir = "{tmp_path}/inbox"\n')
    cfg = config.load(tmp_path / "musiclib.toml")
    beetsenv.setup(cfg)
    return cfg, db.connect(cfg.db_path)


def _run(cfg, conn, result, **kw):
    kw.setdefault("clock", LATER)
    return runner.run(conn, cfg, matcher=lambda source, files, search_id: dict(result), lookup=_no_lookup,
                      day=DAY, progress=io.StringIO(), **kw)


UNMATCHED = {"action": "unsorted", "recommendation": "none", "candidates": "[]"}
NONE_REVIEW = {"action": "review", "recommendation": "none", "distance": 0.7, "candidates": "[]"}


def _lib_files(cfg):
    return sorted(p.relative_to(cfg.library_dir).as_posix() for p in cfg.library_dir.rglob("*") if p.is_file())


def test_a_folder_still_being_written_is_left_alone(env):
    cfg, conn = env
    assert [f["folder"] for f in runner.waiting(conn, cfg, 10)["busy"]] == ["Band A"]
    res = _run(cfg, conn, UNMATCHED, clock=time.time)
    assert res["still_changing"] == ["Band A"] and res["claimed"] == []
    assert (cfg.inbox_dir / "Band A").is_dir() and "written" not in res   # quiet run: no report file


def test_dry_run_changes_nothing(env):
    cfg, conn = env
    before = _snapshot(cfg.inbox_dir)
    p = runner.plan(conn, cfg, clock=LATER)
    assert [f["folder"] for f in p["would_claim"]] == ["Band A"] and p["would_resume"] == []
    assert _snapshot(cfg.inbox_dir) == before and not cfg.library_dir.exists()


def test_unmatched_album_is_claimed_and_imported_as_auto(env):
    cfg, conn = env
    res = _run(cfg, conn, UNMATCHED)
    assert res["claimed"] == [{"batch": 1, "folder": "Band A"}] and res["errors"] == []
    assert _lib_files(cfg) == ["Unsorted/Band A/01 Song 1.mp3", "Unsorted/Band A/02 Song 2.mp3"]
    b = ingest.batch(conn, 1)
    assert (b["status"], b["claimed_by"]) == ("imported", "auto")
    assert {r[0] for r in conn.execute("SELECT decided_by FROM audit_log")} == {"auto"}
    report = json.loads(Path(res["written"]).read_text())
    assert report["batches"]["1"]["status"] == "imported" and Path(res["manifests"][1]).exists()


def test_standing_approval_is_recorded_by_auto_and_imported(env, monkeypatch):
    cfg, conn = env
    monkeypatch.setattr(review, "d24_check", lambda album, meta: (True, "D24: test"))
    res = _run(cfg, conn, NONE_REVIEW)
    assert res["standing"]["asis"] == [ingest.batch(conn, 1)["path"] + "/"]
    assert res["standing"]["imported"]["unsorted"]["imported"] == 1
    m = conn.execute("SELECT decision, decided_by FROM matches").fetchone()
    assert tuple(m) == ("asis", "auto")
    assert ingest.batch(conn, 1)["note"] is None and res["waiting_for_review"] == []


def test_album_outside_the_standing_approvals_waits_for_review(env, monkeypatch):
    cfg, conn = env
    monkeypatch.setattr(review, "d24_check", lambda album, meta: (False, "plausible candidate"))
    res = _run(cfg, conn, NONE_REVIEW)
    assert res["standing"]["ask"] == 1 and res["standing"]["imported"] == {}
    assert [w["batch"] for w in res["waiting_for_review"]] == [1] and not cfg.library_dir.exists()
    assert conn.execute("SELECT decision FROM matches").fetchone()[0] is None


def test_dupe_review_is_never_decided(env, monkeypatch):
    cfg, conn = env
    monkeypatch.setattr(review, "d21_check", lambda album, meta: (True, "D21: test"))
    monkeypatch.setattr(review, "d24_check", lambda album, meta: (True, "D24: test"))
    orig = ingest._dupe_notes
    monkeypatch.setattr(ingest, "_dupe_notes",
                        lambda conn, bid, dirs: ("ingest duplicate review: upgrade of /lib/Old/", None))
    res = _run(cfg, conn, UNMATCHED)
    monkeypatch.setattr(ingest, "_dupe_notes", orig)
    assert res["standing"]["approved"] == [] and res["standing"]["asis"] == []
    assert review.stats(conn)["dupe"]["open"] == 1 and not cfg.library_dir.exists()


def test_held_library_lock_defers_imports_to_the_next_run(env):
    cfg, conn = env
    with importer.library_lock(cfg.state_dir):
        res = _run(cfg, conn, UNMATCHED)
    assert "library_busy" in res and ingest.batch(conn, 1)["status"] == "matched"
    assert not cfg.library_dir.exists()
    again = _run(cfg, conn, UNMATCHED)
    assert again["claimed"] == [] and again["batches"][1]["status"] == "imported"
    assert len(_lib_files(cfg)) == 2


def test_batches_claimed_by_hand_are_not_touched(env):
    cfg, conn = env
    ingest.claim(conn, cfg, "Band A", apply=True, day=DAY)           # claimed_by user
    res = _run(cfg, conn, UNMATCHED)
    assert res["batches"] == {} and ingest.batch(conn, 1)["status"] == "claimed"


def test_one_runner_at_a_time(env):
    cfg, conn = env
    with runner.runner_lock(cfg.state_dir) as mine:
        assert mine
        assert "skipped" in _run(cfg, conn, UNMATCHED)
    assert (cfg.inbox_dir / "Band A").is_dir()
