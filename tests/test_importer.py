import hashlib
import io
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from mutagen.easyid3 import EasyID3

from musiclib import beetsenv, config, db

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _tone(path, fmt_args=()):
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "sine=f=440:d=2", *fmt_args,
                    "-y", str(path)], check=True)


def _snapshot(root):
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


@pytest.fixture
def env(tmp_path):
    src = tmp_path / "dump"
    album = src / "Some Band - Demo"
    album.mkdir(parents=True)
    for i in (1, 2):
        _tone(album / f"0{i} Song {i}.mp3")
        t = EasyID3()
        t.update({"artist": "Some Band", "albumartist": "Some Band", "album": "Demo",
                  "title": f"Song {i}", "tracknumber": str(i)})
        t.save(album / f"0{i} Song {i}.mp3")
    _tone(album / "03 Song 3.wav")
    (album / "cover.jpg").write_bytes(b"\xff\xd8 not really a jpeg")

    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{src}"\nstate_dir = "{tmp_path}/state"\nlibrary_dir = "{tmp_path}/lib"\n')
    cfg = config.load(tmp_path / "musiclib.toml")
    beetsenv.setup(cfg)
    from musiclib import importer

    conn = db.connect(cfg.db_path)
    importer.migrate(conn)
    files = sorted(f"Some Band - Demo/{p.name}" for p in album.iterdir() if p.suffix != ".jpg")
    conn.execute(
        "INSERT INTO matches (album_key, dirs, files, action, recommendation, matched_at) "
        "VALUES ('Some Band - Demo/', ?, ?, 'unsorted', 'none', 'now')",
        (json.dumps(["Some Band - Demo/"]), json.dumps(files)))
    conn.commit()
    return cfg, conn, importer


def test_plan_is_a_dry_run(env):
    cfg, conn, importer = env
    out = importer.plan(conn, cfg.source_dir, "unsorted")
    assert out["albums"] == 1 and out["items"][0]["wav_to_flac"] == 1
    assert not cfg.library_dir.exists()


def test_asis_import_copies_to_unsorted_and_leaves_dump_untouched(env):
    cfg, conn, importer = env
    before = _snapshot(cfg.source_dir)
    res = importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "unsorted", progress=io.StringIO())

    assert res["imported"] == 1
    assert _snapshot(cfg.source_dir) == before                      # safety rules 1 and 2
    assert not any((cfg.state_dir / "staging").iterdir())           # staging cleaned up
    lib_files = sorted(p.relative_to(cfg.library_dir).as_posix()
                       for p in cfg.library_dir.rglob("*") if p.is_file())
    assert lib_files == ["Unsorted/Some Band - Demo/01 Song 1.mp3", "Unsorted/Some Band - Demo/02 Song 2.mp3",
                         "Unsorted/Some Band - Demo/03 Song 3.flac"]  # D13: dump names kept; D7 wav->flac
    rows = conn.execute("SELECT action, source_path, decided_by FROM audit_log ORDER BY source_path").fetchall()
    assert [r["source_path"] for r in rows] == ["Some Band - Demo/01 Song 1.mp3",
                                                 "Some Band - Demo/02 Song 2.mp3",
                                                 "Some Band - Demo/03 Song 3.wav"]
    assert {r["action"] for r in rows} == {"import_asis"}
    # Already imported: nothing left to do.
    assert importer.select(conn, "unsorted") == []


def test_asis_leaves_tags_untouched(env):
    cfg, conn, importer = env
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "unsorted", progress=io.StringIO())
    lib = cfg.library_dir / "Unsorted/Some Band - Demo/01 Song 1.mp3"
    src = cfg.source_dir / "Some Band - Demo/01 Song 1.mp3"
    assert dict(EasyID3(lib)) == dict(EasyID3(src))


def test_pinned_session_only_accepts_the_pinned_release(env):
    _, _, importer = env
    from beets.importer.tasks import Action

    Session = importer._session_class()
    s = Session.__new__(Session)
    s.mode, s.pin, s.tasks, s.note = "apply", "rel-2", [], None
    cands = [SimpleNamespace(info=SimpleNamespace(album_id=i)) for i in ("rel-1", "rel-2")]
    assert s.choose_match(SimpleNamespace(candidates=cands)) is cands[1]
    s.pin = "rel-9"
    assert s.choose_match(SimpleNamespace(candidates=cands)) is Action.SKIP
    assert "rel-9" in s.note


def test_apply_import_uses_pinned_release_and_d12_layout(env, monkeypatch):
    """Offline: a fake MusicBrainz answer stands in for the pinned release."""
    cfg, conn, importer = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks

    def fake_tag_album(items, search_ids=()):
        assert list(search_ids) == ["rel-1"]
        tracks = [TrackInfo(title=f"Real Title {i}", track_id=f"rec-{i}", index=i, medium=1,
                            medium_index=i, medium_total=3, length=2.0) for i in (1, 2, 3)]
        info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                         artist_id="art-1", year=2012, original_year=1999, mediums=1)
        items = sorted(items, key=lambda it: it.path)
        m = AlbumMatch(Distance(), info, dict(zip(items, tracks)), [], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.strong)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    conn.execute("UPDATE matches SET action = 'auto', recommendation = 'strong', distance = 0, "
                 "album_id = 'rel-1', albumartist = 'Real Band', album = 'Real Album', year = 1999")
    conn.commit()
    before = _snapshot(cfg.source_dir)
    res = importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "auto", progress=io.StringIO())

    assert res["imported"] == 1
    assert _snapshot(cfg.source_dir) == before
    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"  # D17: original year
    names = sorted(p.name for p in album_dir.iterdir() if p.suffix in (".mp3", ".flac"))
    assert names == ["01 Real Title 1.mp3", "02 Real Title 2.mp3", "03 Real Title 3.flac"]
    tags = EasyID3(album_dir / "01 Real Title 1.mp3")
    assert tags["musicbrainz_albumid"] == ["rel-1"]
    assert tags["date"][0].startswith("2012")                         # tags keep the reissue date
    # The dump copy keeps its original tags.
    assert EasyID3(cfg.source_dir / "Some Band - Demo/01 Song 1.mp3")["title"] == ["Song 1"]


def test_prune_removes_library_copy_of_a_later_found_duplicate(env):
    cfg, conn, importer = env
    from musiclib import dupes

    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "unsorted", progress=io.StringIO())
    # Pretend dupes later found 02 to be a second copy of 01.
    conn.executescript(dupes.SCHEMA)
    gid = conn.execute("INSERT INTO dupe_groups (tier, scope, action, reason, keeper, reclaimable) VALUES "
                       "(2, 'file', 'auto', 'second copy in the same folder', "
                       "'Some Band - Demo/01 Song 1.mp3', 1)").lastrowid
    conn.executemany("INSERT INTO dupe_members (group_id, path, role) VALUES (?, ?, ?)",
                     [(gid, "Some Band - Demo/01 Song 1.mp3", "keep"), (gid, "Some Band - Demo/02 Song 2.mp3", "drop")])
    conn.commit()
    before = _snapshot(cfg.source_dir)
    assert importer.prune_duplicates(conn)["would_remove"] == 1
    assert importer.prune_duplicates(conn, apply=True)["removed"] == 1
    lib = cfg.library_dir / "Unsorted/Some Band - Demo"
    assert sorted(p.name for p in lib.iterdir()) == ["01 Song 1.mp3", "03 Song 3.flac"]
    assert _snapshot(cfg.source_dir) == before
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'prune_duplicate'").fetchone()[0] == 1


def test_file_not_on_release_is_kept_in_album_folder_untouched(env, monkeypatch):
    cfg, conn, importer = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks

    def fake_tag_album(items, search_ids=()):
        tracks = [TrackInfo(title=f"Real Title {i}", track_id=f"rec-{i}", index=i, medium=1,
                            medium_index=i, medium_total=2, length=2.0) for i in (1, 2)]
        info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                         artist_id="art-1", year=1999, mediums=1)
        items = sorted(items, key=lambda it: it.path)
        m = AlbumMatch(Distance(), info, dict(zip(items[:2], tracks)), items[2:], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.medium)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    conn.execute("UPDATE matches SET action = 'review', decision = 'approve', decided_album_id = 'rel-1', "
                 "decided_by = 'agent'")
    conn.commit()
    res = importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "approved", progress=io.StringIO())
    assert res["imported"] == 1
    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    names = sorted(p.name for p in album_dir.iterdir() if p.suffix in (".mp3", ".flac"))
    assert names == ["01 Real Title 1.mp3", "02 Real Title 2.mp3", "03 Song 3.flac"]
    extra = conn.execute("SELECT source_path, reason FROM audit_log WHERE action = 'import_extra'").fetchone()
    assert extra["source_path"] == "Some Band - Demo/03 Song 3.wav" and "not on release rel-1" in extra["reason"]


def test_damage_is_judged_by_decoding_not_by_scan_errors(env, monkeypatch):
    cfg, conn, importer = env
    conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, duration, error, scanned_at) VALUES "
                 "('A/broken.mp3', 'A', 'mp3', 1, 0, 247, 'fingerprint: Empty fingerprint', 'now'), "
                 "('A/playable.mp3', 'A', 'mp3', 1, 0, 183, 'fingerprint: Empty fingerprint', 'now'), "
                 "('A/fine.mp3', 'A', 'mp3', 1, 0, 200, NULL, 'now')")
    decoded = {"broken.mp3": 13.8, "playable.mp3": 228.8, "fine.mp3": 0.0}
    monkeypatch.setattr(importer, "decodable_seconds", lambda p: decoded[p.name])
    assert importer.is_damaged(conn, cfg.source_dir, "A/broken.mp3")
    assert not importer.is_damaged(conn, cfg.source_dir, "A/playable.mp3")   # fpcalc failed, audio fine
    assert not importer.is_damaged(conn, cfg.source_dir, "A/fine.mp3")       # no scan error: not checked
    why = importer.extra_skip_reason(conn, cfg.source_dir, "A/broken.mp3", ["A/broken.mp3", "A/fine.mp3"])
    assert why.startswith("damaged")


def test_remove_from_library_refuses_paths_outside_it(env):
    cfg, conn, importer = env
    with pytest.raises(SystemExit):
        importer.remove_from_library(conn, str(cfg.source_dir / "Some Band - Demo/01 Song 1.mp3"), "x", "user")
    assert (cfg.source_dir / "Some Band - Demo/01 Song 1.mp3").exists()


def test_undecodable_file_never_reaches_the_library(env, monkeypatch):
    cfg, conn, importer = env
    monkeypatch.setattr(importer, "decodable_seconds", lambda p: 0.6)
    conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, error, scanned_at) VALUES "
                 "('Some Band - Demo/02 Song 2.mp3', 'x', 'mp3', 1, 0, 'fingerprint: Empty fingerprint', 'now')")
    conn.execute("UPDATE files SET duration = 277 WHERE path = 'Some Band - Demo/02 Song 2.mp3'")
    conn.commit()
    res = importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "unsorted", progress=io.StringIO())
    assert res["imported"] == 1
    lib = cfg.library_dir / "Unsorted/Some Band - Demo"
    assert sorted(p.name for p in lib.iterdir()) == ["01 Song 1.mp3", "03 Song 3.flac"]
    row = conn.execute("SELECT reason FROM audit_log WHERE action = 'skip_extra'").fetchone()
    assert row["reason"].startswith("damaged")
