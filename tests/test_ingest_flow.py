"""M5 end to end, offline: claim -> scan -> dedupe -> match -> import -> report, plus D32."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from musiclib import beetsenv, config, db, ingest, review

from test_importer import _snapshot

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")), reason="needs ffmpeg and fpcalc")
DAY = "2026-09-30"


def _tone(path, freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", f"sine=f={freq}:d=2", "-y", str(path)],
                   check=True)


def _no_lookup(conn, key, progress=None):
    return {"looked_up": 0, "elapsed_s": 0}


@pytest.fixture
def env(tmp_path):
    album = tmp_path / "inbox" / "New Band - Demo"
    for i in (1, 2):
        _tone(album / f"0{i} Song {i}.mp3", 440 + 100 * i)
        t = EasyID3()
        t.update({"artist": "New Band", "album": "Demo", "title": f"Song {i}", "tracknumber": str(i)})
        t.save(album / f"0{i} Song {i}.mp3")
    _tone(album / "03 Song 3.wav", 800)
    (tmp_path / "dump").mkdir()
    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{tmp_path}/dump"\nstate_dir = "{tmp_path}/state"\n'
        f'library_dir = "{tmp_path}/lib"\ninbox_dir = "{tmp_path}/inbox"\n')
    cfg = config.load(tmp_path / "musiclib.toml")
    beetsenv.setup(cfg)
    conn = db.connect(cfg.db_path)
    ingest.claim(conn, cfg, "New Band - Demo", apply=True, day=DAY)
    ingest.scan(conn, cfg, 1, workers=2, lookup=_no_lookup, progress=io.StringIO())
    ingest.dedupe(conn, 1)
    root = cfg.inbox_dir / ".processed" / DAY / "New Band - Demo"
    return cfg, conn, root


def _matcher(result):
    return lambda source, files, search_id: dict(result)


def test_unmatched_album_goes_to_unsorted(env):
    cfg, conn, root = env
    ingest.match(conn, 1, matcher=_matcher({"action": "unsorted", "recommendation": "none",
                                            "candidates": "[]"}), progress=io.StringIO())
    plan = ingest.import_batch(conn, cfg, 1)
    assert plan["dry_run"] and plan["unsorted"]["albums"] == 1 and not cfg.library_dir.exists()

    before = _snapshot(root)
    res = ingest.import_batch(conn, cfg, 1, apply=True, progress=io.StringIO())
    assert res["unsorted"]["imported"] == 1 and res["waiting_for_review"] == []
    assert _snapshot(root) == before                                  # D27: inbox is read-only
    names = sorted(p.relative_to(cfg.library_dir).as_posix() for p in cfg.library_dir.rglob("*") if p.is_file())
    assert names == ["Unsorted/New Band - Demo/01 Song 1.mp3", "Unsorted/New Band - Demo/02 Song 2.mp3",
                     "Unsorted/New Band - Demo/03 Song 3.flac"]
    sources = [r[0] for r in conn.execute("SELECT source_path FROM audit_log WHERE action = 'import_asis'")]
    assert all(s.startswith(str(root) + "/") for s in sources) and len(sources) == 3

    rep = ingest.manifest(conn, cfg, 1)
    assert rep["by_status"] == {"imported": 3} and Path(rep["written"]).exists()
    assert ingest.batch(conn, 1)["status"] == "imported"


def test_strong_match_imports_with_pinned_release(env, monkeypatch):
    cfg, conn, root = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks

    def fake_tag_album(items, search_ids=()):
        tracks = [TrackInfo(title=f"Real {i}", track_id=f"rec-{i}", index=i, medium=1, medium_index=i,
                            medium_total=3, length=2.0) for i in (1, 2, 3)]
        info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                         artist_id="art-1", year=2001, mediums=1)
        m = AlbumMatch(Distance(), info, dict(zip(sorted(items, key=lambda it: it.path), tracks)), [], [])
        return "New Band", "Demo", Proposal([m], Recommendation.strong)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    ingest.match(conn, 1, matcher=_matcher({"action": "auto", "recommendation": "strong", "distance": 0,
                                            "album_id": "rel-1", "candidates": "[]"}), progress=io.StringIO())
    res = ingest.import_batch(conn, cfg, 1, apply=True, progress=io.StringIO())
    assert res["auto"]["imported"] == 1
    album = cfg.library_dir / "Real Band" / "2001 - Real Album"
    assert sorted(p.name for p in album.iterdir() if p.suffix in (".mp3", ".flac")) == [
        "01 Real 1.mp3", "02 Real 2.mp3", "03 Real 3.flac"]
    assert EasyID3(root / "01 Song 1.mp3")["title"] == ["Song 1"]    # inbox copy untouched


def test_dedupe_review_holds_a_strong_match_for_review(env):
    cfg, conn, root = env
    conn.execute("INSERT INTO ingest_dupes VALUES (1, ?, 'folder', 'review', 'upgrade', '/lib/Old/', 1.0, "
                 "'better audio', NULL)", (str(root) + "/",))
    conn.commit()
    out = ingest.match(conn, 1, matcher=_matcher({"action": "auto", "recommendation": "strong", "distance": 0,
                                                  "album_id": "rel-1", "candidates": "[]"}), progress=io.StringIO())
    assert out["review"] == 1
    res = ingest.import_batch(conn, cfg, 1, apply=True, progress=io.StringIO())
    assert "auto" not in res and [w["album_key"] for w in res["waiting_for_review"]] == [str(root) + "/"]
    assert review.stats(conn)["dupe"]["open"] == 1                    # D31: shared queue, dupe kind
    assert ingest.manifest(conn, cfg, 1)["by_status"] == {"review": 3}


def _weak(env):
    cfg, conn, root = env
    ingest.match(conn, 1, matcher=_matcher({"action": "review", "recommendation": "low", "distance": 0.3,
                                            "album_id": "rel-1", "candidates": "[]"}), progress=io.StringIO())
    ingest.import_batch(conn, cfg, 1, apply=True, progress=io.StringIO())
    b = ingest.batch(conn, 1)
    assert (b["status"], b["note"]) == ("imported", "1 album(s) not imported yet (review or pending)")
    return str(root) + "/"


def test_album_imported_through_review_completes_its_batch(env):
    cfg, conn, root = env
    key = _weak(env)
    review.decide(conn, [{"album_key": key, "decision": "asis", "reason": "test"}], "user")
    assert ingest.batch(conn, 1)["note"]                              # decided, not imported yet
    from musiclib import importer
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "unsorted", only=[key],
                 progress=io.StringIO())                              # /review's import step
    b = ingest.batch(conn, 1)
    assert (b["status"], b["note"]) == ("imported", None)


def test_skipping_the_last_album_completes_its_batch(env):
    cfg, conn, root = env
    key = _weak(env)
    review.decide(conn, [{"album_key": key, "decision": "skip", "reason": "test"}], "user")
    b = ingest.batch(conn, 1)
    assert (b["status"], b["note"]) == ("imported", None)


def test_strong_match_on_a_release_already_in_the_library_is_held(env):
    cfg, conn, root = env
    mid = conn.execute("INSERT INTO matches (album_key, dirs, files, action, album_id, matched_at) "
                       "VALUES ('Old/', '[\"Old/\"]', '[]', 'auto', 'rel-1', 'now')").lastrowid
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel-1', '/lib/Old', 3, NULL, 'now')",
                 (mid,))
    conn.commit()
    out = ingest.match(conn, 1, matcher=_matcher({"action": "auto", "recommendation": "strong", "distance": 0,
                                                  "album_id": "rel-1", "candidates": "[]"}), progress=io.StringIO())
    assert out == {"batch": 1, "matched": 1, "review": 1}
    note = conn.execute("SELECT note FROM matches WHERE batch_id = 1").fetchone()[0]
    assert "release already in the library (from Old/)" in note
    assert review.stats(conn)["dupe"]["open"] == 1


def test_upgrade_removes_the_old_copy_only_after_approved_import(env):
    cfg, conn, root = env
    old_dir = cfg.library_dir / "Old Band" / "Demo"
    old_dir.mkdir(parents=True)
    old = [old_dir / f"0{i} Song {i}.mp3" for i in (1, 2)]
    for i, p in enumerate(old, 1):
        p.write_bytes(b"old copy")
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', 'import', ?, ?, 'auto')", (f"Old Demo/0{i}.mp3", str(p)))
    conn.execute("INSERT INTO ingest_dupes VALUES (1, ?, 'folder', 'review', 'upgrade', ?, 1.0, 'better', NULL)",
                 (str(root) + "/", str(old_dir) + "/"))
    ingest.match(conn, 1, matcher=_matcher({"action": "review", "recommendation": "low", "distance": 0.2,
                                            "album_id": "rel-1", "candidates": "[]"}), progress=io.StringIO())
    assert ingest.upgrades(conn, cfg, 1)["upgrades"] == []            # not approved/imported yet

    mid = conn.execute("SELECT id FROM matches WHERE batch_id = 1").fetchone()[0]
    conn.execute("UPDATE matches SET decision = 'approve', decided_by = 'user' WHERE id = ?", (mid,))
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel-1', '/lib/New', 3, NULL, 'now')",
                 (mid,))
    conn.commit()
    plan = ingest.upgrades(conn, cfg, 1)
    assert plan["upgrades"][0]["remove"] == sorted(map(str, old))
    res = ingest.upgrades(conn, cfg, 1, apply=True)
    assert res["removed"] == 2 and not any(p.exists() for p in old)
    reasons = [r[0] for r in conn.execute("SELECT reason FROM audit_log WHERE action = 'remove_from_library'")]
    assert len(reasons) == 2 and all(r.startswith("D32: replaced by") for r in reasons)
    assert ingest.upgrades(conn, cfg, 1)["upgrades"] == []            # idempotent


def test_fully_skipped_batch_imports_nothing_from_other_batches(env):
    cfg, conn, root = env
    conn.execute("INSERT INTO ingest_dupes VALUES (1, ?, 'folder', 'skip', 'duplicate', '/lib/Old/', 1.0, "
                 "'already in the library', NULL)", (str(root) + "/",))
    conn.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, album_id, "
                 "matched_at, batch_id) VALUES ('/elsewhere/Other/', '[]', '[]', 'auto', 'strong', 0, "
                 "'rel-2', '2026-10-02', 2)")
    conn.commit()
    assert ingest.match(conn, 1, matcher=_matcher({"action": "auto"}), progress=io.StringIO())["matched"] == 0
    plan = ingest.import_batch(conn, cfg, 1)
    assert plan["auto"]["albums"] == 0 and plan["unsorted"]["albums"] == 0  # not batch 2's album
    res = ingest.import_batch(conn, cfg, 1, apply=True, progress=io.StringIO())
    assert "auto" not in res and not cfg.library_dir.exists()
