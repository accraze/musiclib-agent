import io
import json
import os
import shutil

import pytest

from test_importer import _snapshot, env  # noqa: F401  (fixture)

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _fake_release(album_title="Real Album", titles=("Real Title 1", "Real Title 2", "Real Title 3")):
    from beets.autotag import AlbumInfo, TrackInfo

    tracks = [TrackInfo(title=t, track_id=f"rec-{i}", release_track_id=f"rt-{i}", index=i, medium=1,
                        medium_index=i, medium_total=3, length=2.0) for i, t in enumerate(titles, 1)]
    return AlbumInfo(tracks=tracks, album=album_title, album_id="rel-1", artist="Real Band",
                     artist_id="art-1", year=2012, original_year=1999, mediums=1)


@pytest.fixture
def imported(env, monkeypatch):  # noqa: F811
    """One album imported through beets on a fake release, plus the audit module."""
    cfg, conn, importer = env
    from beets.autotag import AlbumMatch
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks

    def fake_tag_album(items, search_ids=()):
        info = _fake_release()
        m = AlbumMatch(Distance(), info, dict(zip(sorted(items, key=lambda it: it.path), info.tracks)), [], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.strong)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    conn.execute("UPDATE matches SET action = 'auto', recommendation = 'strong', distance = 0, "
                 "album_id = 'rel-1', albumartist = 'Real Band', album = 'Real Album', year = 1999")
    conn.commit()
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "auto", progress=io.StringIO())
    from musiclib import audit

    audit.migrate(conn)
    return cfg, conn, importer, audit, importer.open_library()


def test_clean_library_has_no_problems(imported):
    cfg, conn, _, audit, lib = imported
    rep = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)
    problems = audit.summary(rep)["problems"]
    assert problems == {k: 0 for k in problems}
    assert rep["integrity"]["library_files"] == 3


def test_check_finds_missing_untracked_and_changed_files(imported):
    cfg, conn, _, audit, lib = imported
    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    audit.check(conn, lib, cfg.library_dir, cfg.source_dir)  # baseline
    (album_dir / "01 Real Title 1.mp3").unlink()
    shutil.copy(album_dir / "02 Real Title 2.mp3", album_dir / "99 stray.mp3")
    os.utime(album_dir / "02 Real Title 2.mp3", (1, os.stat(album_dir / "02 Real Title 2.mp3").st_mtime + 60))
    dump_file = cfg.source_dir / "Some Band - Demo/01 Song 1.mp3"
    conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, scanned_at) VALUES (?, ?, 'mp3', ?, ?, 'now')",
                 ("Some Band - Demo/01 Song 1.mp3", "Some Band - Demo", 1, dump_file.stat().st_mtime))
    conn.commit()

    rep = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)
    i = rep["integrity"]
    assert [os.path.basename(p) for p in i["beets_missing"]["examples"]] == ["01 Real Title 1.mp3"]
    assert [os.path.basename(p) for p in i["logged_missing"]["examples"]] == ["01 Real Title 1.mp3"]
    assert [os.path.basename(p) for p in i["untracked"]["examples"]] == ["99 stray.mp3"]
    assert [os.path.basename(r["path"]) for r in i["modified"]["examples"]] == ["02 Real Title 2.mp3"]
    assert rep["originals"]["changed"]["examples"] == ["Some Band - Demo/01 Song 1.mp3"]  # size differs


def test_rows_placing_no_inventoried_file_are_orphans_not_missing_files(imported):
    cfg, conn, _, audit, lib = imported
    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) VALUES "
                 "('now', 'retag_by_fingerprint', 'Some Band - Demo/', ?, 'old sweep', 'user')",
                 (str(cfg.library_dir / "Real Band/1999 - Real Album/02 x.retag-tmp.mp3"),))
    conn.commit()
    i = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)["integrity"]
    assert i["orphan_rows"]["examples"][0]["source"] == "Some Band - Demo/"
    assert i["logged_missing"]["count"] == 0


def test_modified_files_stay_flagged_until_accepted(imported):
    cfg, conn, _, audit, lib = imported
    f = cfg.library_dir / "Real Band" / "1999 - Real Album" / "02 Real Title 2.mp3"
    # First sighting: embedart's few seconds after beets' mtime are not a modification.
    os.utime(f, (1, lib.items()[1].mtime + 30))
    assert audit.check(conn, lib, cfg.library_dir, cfg.source_dir)["integrity"]["modified"]["count"] == 0
    os.utime(f, (1, f.stat().st_mtime + 3600))  # another tagger, an hour later
    for _ in range(2):
        mod = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)["integrity"]["modified"]
        assert [os.path.basename(r["path"]) for r in mod["examples"]] == ["02 Real Title 2.mp3"]
    audit.check(conn, lib, cfg.library_dir, cfg.source_dir, accept_modified=True)
    assert audit.check(conn, lib, cfg.library_dir, cfg.source_dir)["integrity"]["modified"]["count"] == 0


def test_relink_pairs_follow_an_aunique_suffix_that_moved():
    from musiclib.audit import relink_pairs

    stale = ["/lib/不失者/1989 - X/01 a.flac", "/lib/不失者/1991 - X [1991]/01 a.flac", "/lib/A/2000 - Y/01 b.mp3"]
    unlogged = ["/lib/Fushitsusha/1989 - X [1989]/01 a.flac", "/lib/Fushitsusha/1991 - X/01 a.flac",
                "/lib/B/2000 - Y/01 b.mp3"]
    assert relink_pairs(stale, unlogged) == {
        "/lib/A/2000 - Y/01 b.mp3": "/lib/B/2000 - Y/01 b.mp3",
        "/lib/不失者/1989 - X/01 a.flac": "/lib/Fushitsusha/1989 - X [1989]/01 a.flac",
        "/lib/不失者/1991 - X [1991]/01 a.flac": "/lib/Fushitsusha/1991 - X/01 a.flac"}


def test_incomplete_album_and_missing_art(imported):
    cfg, conn, _, audit, lib = imported
    album = lib.albums()[0]
    item = list(album.items())[0]
    item.remove(delete=True)  # 2 of 3 tracks left
    rep = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)
    assert rep["incomplete_albums"]["examples"][0]["tracks"] == 2
    assert rep["incomplete_albums"]["examples"][0]["of"] == 3
    assert rep["no_art"]["count"] == 0
    album.artpath = None
    album.store()
    assert audit.check(conn, lib, cfg.library_dir, cfg.source_dir)["no_art"]["count"] == 1


def test_decode_is_incremental_and_flags_truncated_files(imported):
    cfg, conn, _, audit, lib = imported
    out = audit.run_decode(conn, lib, cfg.library_dir, workers=2, progress=io.StringIO())
    assert out == {"decoded": 3, "damaged_now": 0, "still_pending": 0}
    assert audit.run_decode(conn, lib, cfg.library_dir, progress=io.StringIO())["decoded"] == 0
    # Truncate one file: it is decoded again and flagged.
    f = cfg.library_dir / "Real Band" / "1999 - Real Album" / "03 Real Title 3.flac"
    f.write_bytes(f.read_bytes()[: f.stat().st_size // 4])
    out = audit.run_decode(conn, lib, cfg.library_dir, limit=5, progress=io.StringIO())
    assert out["decoded"] == 1 and out["damaged_now"] == 1
    assert [os.path.basename(r["path"]) for r in audit.decode_findings(conn)["damaged"]["examples"]] == \
        ["03 Real Title 3.flac"]


def test_mbsync_dry_run_records_changes_and_touches_nothing(imported, monkeypatch):
    cfg, conn, _, audit, lib = imported
    from beets import metadata_plugins

    monkeypatch.setattr(metadata_plugins, "album_for_id", lambda _id: _fake_release())
    before = _snapshot(cfg.library_dir)
    assert audit.run_mbsync(conn, lib, progress=io.StringIO())["same"] == 1

    monkeypatch.setattr(metadata_plugins, "album_for_id",
                        lambda _id: _fake_release(titles=("Real Title 1", "Corrected Title", "Real Title 3")))
    out = audit.run_mbsync(conn, lib, progress=io.StringIO())
    assert out["changed"] == 1
    assert _snapshot(cfg.library_dir) == before                     # dry run: nothing written
    album_id = lib.albums()[0].id
    detail = audit.mbsync_detail(conn, album_id)
    assert detail["fields"] == {"title": 1}
    assert detail["tracks"][0]["changes"]["title"] == ["Real Title 2", "Corrected Title"]
    assert [os.path.basename(m["to"]) for m in detail["moves"]] == ["02 Corrected Title.mp3"]
    assert lib.get_album(album_id).items()[1].title == "Real Title 2"  # beets DB untouched too


def test_apply_mbsync_moves_and_logs(imported, monkeypatch):
    cfg, conn, importer, audit, lib = imported
    from beets import metadata_plugins

    monkeypatch.setattr(metadata_plugins, "album_for_id",
                        lambda _id: _fake_release(album_title="Real Album (Remastered)"))
    audit.run_mbsync(conn, lib, progress=io.StringIO())
    album_id = lib.albums()[0].id
    old_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    (old_dir / "bonus.mp3").write_bytes(b"extra")  # a placed extra (D20) must follow the album
    conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, reason, decided_by) "
                 "VALUES ('now', 'import_extra', 'Some Band - Demo/bonus.mp3', ?, 'test', 'agent')",
                 (str(old_dir / "bonus.mp3"),))
    conn.commit()
    src_before = _snapshot(cfg.source_dir)

    out = audit.apply_mbsync(conn, lib, [album_id], "approved in test", "agent")
    assert out["applied"] == 1 and out["albums"][0]["files_moved"] == 3 and out["albums"][0]["extras_moved"] == 1
    new_dir = cfg.library_dir / "Real Band" / "1999 - Real Album (Remastered)"
    assert sorted(p.name for p in new_dir.iterdir() if p.suffix in (".mp3", ".flac")) == \
        ["01 Real Title 1.mp3", "02 Real Title 2.mp3", "03 Real Title 3.flac", "bonus.mp3"]
    assert not old_dir.exists()
    assert _snapshot(cfg.source_dir) == src_before
    # The audit log follows every file, so a fresh check is clean.
    assert all(d.startswith(str(new_dir)) for d in importer.current_paths(conn).values())
    problems = audit.summary(audit.check(conn, lib, cfg.library_dir, cfg.source_dir))["problems"]
    assert problems == {k: 0 for k in problems}
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'mbsync'").fetchone()[0] == 1
    assert audit.mbsync_findings(conn)["changed"] == 0


def test_apply_skips_albums_without_pending_changes(imported):
    cfg, conn, _, audit, lib = imported
    out = audit.apply_mbsync(conn, lib, [lib.albums()[0].id], "test", "agent")
    assert out["applied"] == 0


def test_relink_logs_files_beets_moved_without_a_row(imported):
    cfg, conn, importer, audit, lib = imported
    # Simulate D25 before D39: beets renamed the artist folder, the audit log wasn't told.
    for src in importer.current_paths(conn):  # the originals, as inventoried
        st = (cfg.source_dir / src).stat()
        conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, scanned_at) VALUES (?, '', '', ?, ?, '')",
                     (src, st.st_size, st.st_mtime))
    album = lib.albums()[0]
    album.albumartist = "Renamed Band"
    for item in album.items():
        item.albumartist = "Renamed Band"
        item.store()
    album.store()
    album.move()
    rep = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)
    assert rep["integrity"]["unlogged"]["count"] == 3 and rep["integrity"]["relinkable"] == 3

    assert audit.relink(conn, lib, cfg.library_dir, "agent")["pairs"] == 3
    assert audit.relink(conn, lib, cfg.library_dir, "agent", apply=True) == {"relinked": 3}
    problems = audit.summary(audit.check(conn, lib, cfg.library_dir, cfg.source_dir))["problems"]
    assert problems == {k: 0 for k in problems}
    assert json.loads(json.dumps(importer.current_paths(conn)))  # still a plain mapping
