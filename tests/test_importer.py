import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
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


@pytest.mark.parametrize("name,title,minutes,others,expected", [
    ("01 The Faust Tapes.mp3", "Several Hands on Our Piano", 43.7, [1.5] * 26, True),        # named like the album
    ("Nipponjin (Full Album).mp3", "The Cave", 54.0, [5.0] * 10, True),                       # says full album
    ("07 Gong ORFT Invasion 1971.mp3", "The Switch Doctor", 28.6, [5.3, 3.8, 3.9, 4.8, 2.0, 4.2], False),
    ("03 - Cake (Who Shit On The ).mp3", "Cake", 9.1, [0.8, 3.6, 4.5], False),                 # only 3 others
])
def test_whole_album_file_needs_length_and_name(env, name, title, minutes, others, expected):
    cfg, conn, importer = env
    album = {"01 The Faust Tapes.mp3": "The Faust Tapes", "Nipponjin (Full Album).mp3": "Nipponjin"}.get(
        name, "Some Album")
    files = [f"X/{name}"] + [f"X/{i:02d} t.mp3" for i in range(len(others))]
    rows = [(files[0], title, album, minutes * 60)] + [(f, "t", album, m * 60) for f, m in zip(files[1:], others)]
    conn.executemany("INSERT INTO files (path, top_dir, ext, size, mtime, title, album, duration, scanned_at) "
                     "VALUES (?, 'X', 'mp3', 1, 0, ?, ?, ?, 'now')", rows)
    assert importer._is_whole_album_file(conn, files[0], files) is expected


def _magic_city(conn):
    """Sun Ra, The Magic City: a 26-minute title track named like the album, plus 4 others."""
    from musiclib import verify

    conn.executescript(verify.SCHEMA)
    files = ["M/01 The Magic City.flac", "M/02 The Shadow World.flac", "M/03 Abstract Eye.flac",
             "M/04 Abstract I.flac", "M/06 The Magic City [Mono Version Ending].flac"]
    for f, secs in zip(files, [1587, 636, 164, 245, 98]):
        fid = conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, title, album, duration, scanned_at) "
                           "VALUES (?, 'M', 'flac', 1, 0, 'The Magic City', 'The Magic City', ?, 'now')",
                           (f, secs)).lastrowid
        if f.startswith("M/01"):
            conn.execute("INSERT INTO verify (file_id, verdict) VALUES (?, 'confirmed')", (fid,))
    return files


def test_title_track_confirmed_as_one_recording_is_not_a_whole_album(env):
    cfg, conn, importer = env
    files = _magic_city(conn)
    assert importer._is_whole_album_file(conn, files[0], files) is False
    conn.execute("DELETE FROM verify")
    assert importer._is_whole_album_file(conn, files[0], files) is True   # what the old rule did


def test_repair_re_places_a_skip_the_current_rules_no_longer_support(env, monkeypatch):
    cfg, conn, importer = env
    files = _magic_city(conn)
    monkeypatch.setattr(importer, "_duplicate_of", lambda *a: None)
    lib_dir = cfg.library_dir / "Sun Ra" / "1966 - The Magic City"
    lib_dir.mkdir(parents=True)
    src = cfg.source_dir / "M"
    src.mkdir()
    (src / "01 The Magic City.flac").write_bytes(b"title track")
    mid = conn.execute("INSERT INTO matches (album_key, dirs, files, action, matched_at) "
                       "VALUES ('M/', '[]', ?, 'review', 'now')", (json.dumps(files),)).lastrowid
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel', ?, 4, NULL, 'now')",
                 (mid, str(lib_dir)))
    for f in files[1:]:
        conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', 'import', ?, ?, 'agent')", (f, str(lib_dir / Path(f).name)))
    conn.execute("INSERT INTO audit_log (ts, action, source_path, reason, decided_by) VALUES "
                 "('now', 'skip_extra', ?, 'whole album as one file; the split tracks were imported', 'agent')",
                 (files[0],))
    conn.commit()
    dry = importer.repair_extras(conn, cfg.source_dir)
    assert [s["file"] for s in dry["skips_no_longer_apply"]] == [files[0]]
    assert not (lib_dir / "01 The Magic City.flac").exists()
    assert importer.repair_extras(conn, cfg.source_dir, apply=True)["re_placed_skips"] == 1
    assert (lib_dir / "01 The Magic City.flac").read_bytes() == b"title track"
    assert importer.current_paths(conn)[files[0]] == str(lib_dir / "01 The Magic City.flac")
    assert importer.repair_extras(conn, cfg.source_dir)["skips_no_longer_apply"] == []


def test_extra_duplicating_an_imported_track_is_skipped_but_bonus_with_copied_title_is_kept(env):
    cfg, conn, importer = env
    rows = [("W/GH 07 Greasy Legs.mp3", "Greasy Legs", 147.0),     # mapped by beets
            ("W/07 - greasy legs.mp3", "Greasy Legs", 148.0),      # leftover second copy
            ("W/GH 20 In The First Place.mp3", "In the Park", 197.0),  # bonus with a copied title
            ("W/GH 04 In The Park.mp3", "In the Park", 248.0)]     # mapped by beets
    conn.executemany("INSERT INTO files (path, top_dir, ext, size, mtime, title, duration, scanned_at) "
                     "VALUES (?, 'W', 'mp3', 1, 0, ?, ?, 'now')", rows)
    files = [r[0] for r in rows]
    placed = ["W/GH 07 Greasy Legs.mp3", "W/GH 04 In The Park.mp3"]
    why = importer.extra_skip_reason(conn, cfg.source_dir, "W/07 - greasy legs.mp3", files, placed)
    assert why and why.startswith("duplicate of 'Greasy Legs'")
    assert importer.extra_skip_reason(conn, cfg.source_dir, "W/GH 20 In The First Place.mp3", files, placed) is None


def test_retag_promotes_the_extra_that_is_the_track_and_puts_the_stray_back(env, monkeypatch):
    """Midnight Cleaners: a bonus track tagged with track 2's title wins track 2 and the real
    track 2 lands beside it as an extra. retag swaps them; the dump stays untouched."""
    cfg, conn, importer = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks
    from musiclib import acoustid, retag, verify

    tracks = [TrackInfo(title=f"Real Title {i}", track_id=f"rec-{i}", index=i, medium=1,
                        medium_index=i, medium_total=2, length=2.0) for i in (1, 2)]
    info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                     artist_id="art-1", year=1999, mediums=1)

    def fake_tag_album(items, search_ids=()):
        items = sorted(items, key=lambda it: it.path)  # 01 Song 1, 02 Song 2, 03 Song 3 (the stray)
        m = AlbumMatch(Distance(), info, {items[0]: tracks[0], items[2]: tracks[1]}, [items[1]], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.medium)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    monkeypatch.setattr("beets.metadata_plugins.album_for_id", lambda _id: info)
    monkeypatch.setattr(importer, "extra_skip_reason", lambda *a, **k: None)  # fake fingerprints
    acoustid.migrate(conn)
    conn.executescript(verify.SCHEMA)
    for name, title, recs, verdict in [("01 Song 1.mp3", "Song 1", ["rec-1"], "confirmed"),
                                       ("02 Song 2.mp3", "Song 2", ["rec-2"], "confirmed"),
                                       ("03 Song 3.wav", "Song 2", [], "unverifiable")]:
        rel = f"Some Band - Demo/{name}"
        fid = conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, duration, title, fingerprint, "
                           "fp_duration, scanned_at) VALUES (?, 'Some Band - Demo', 'mp3', 1, 0, 2.0, ?, ?, 2, 'now')",
                           (rel, title, f"fp-{name}")).lastrowid
        conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, recordings, looked_up_at) "
                     "VALUES (?, 2, 'ok', ?, 'now')", (f"fp-{name}", json.dumps([{"id": r, "score": 0.9} for r in recs])))
        conn.execute("INSERT INTO verify (file_id, verdict) VALUES (?, ?)", (fid, verdict))
    conn.execute("UPDATE matches SET action = 'review', decision = 'approve', decided_album_id = 'rel-1', "
                 "decided_by = 'agent'")
    conn.commit()
    before = _snapshot(cfg.source_dir)
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "approved", progress=io.StringIO())
    note = conn.execute("SELECT note FROM imports").fetchone()[0]
    assert "real audio of a track" in note                     # flagged after import, not applied

    lib = importer.open_library()
    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    assert (album_dir / "02 Song 2.mp3").exists()              # the real track 2, as an extra
    out = retag.apply(conn, lib, "Some Band - Demo/", "user", "test", source=cfg.source_dir)
    assert out == {"album_key": "Some Band - Demo/", "retagged": 0, "promoted": 1, "demoted": 0}

    names = sorted(p.name for p in album_dir.iterdir() if p.suffix in (".mp3", ".flac"))
    assert names == ["01 Real Title 1.mp3", "02 Real Title 2.mp3", "03 Song 3.flac"]
    by_track = {i.mb_trackid: os.path.basename(os.fsdecode(i.path)) for i in lib.items()}
    assert by_track == {"rec-1": "01 Real Title 1.mp3", "rec-2": "02 Real Title 2.mp3"}
    assert EasyID3(album_dir / "02 Real Title 2.mp3")["title"] == ["Real Title 2"]
    assert importer.current_paths(conn) == {
        "Some Band - Demo/01 Song 1.mp3": str(album_dir / "01 Real Title 1.mp3"),
        "Some Band - Demo/02 Song 2.mp3": str(album_dir / "02 Real Title 2.mp3"),
        "Some Band - Demo/03 Song 3.wav": str(album_dir / "03 Song 3.flac")}
    assert _snapshot(cfg.source_dir) == before                 # safety rules 1 and 2
    assert retag.plan(conn, lib, "Some Band - Demo/")["changes"] == []


def test_retag_takes_a_stray_off_relabels_and_fills_the_emptied_track(env, monkeypatch):
    """D35, as on MxPx Let It Happen: beets put track 2's audio on track 1 and a stray on track 2,
    and track 1's real audio became an extra. One retag run takes the stray off (back as an
    extra from the dump), moves track 2's audio home and fills track 1 with the extra."""
    cfg, conn, importer = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks
    from musiclib import acoustid, retag, verify

    tracks = [TrackInfo(title=f"Real Title {i}", track_id=f"rec-{i}", index=i, medium=1,
                        medium_index=i, medium_total=2, length=2.0) for i in (1, 2)]
    info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                     artist_id="art-1", year=1999, mediums=1)

    def fake_tag_album(items, search_ids=()):
        items = sorted(items, key=lambda it: it.path)  # 01 (track 2's audio), 02 (stray), 03 (track 1)
        m = AlbumMatch(Distance(), info, {items[0]: tracks[0], items[1]: tracks[1]}, [items[2]], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.medium)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    monkeypatch.setattr("beets.metadata_plugins.album_for_id", lambda _id: info)
    monkeypatch.setattr(importer, "extra_skip_reason", lambda *a, **k: None)  # fake fingerprints
    acoustid.migrate(conn)
    conn.executescript(verify.SCHEMA)
    for name, tag, recs, heard in [("01 Song 1.mp3", "Song 1", ["rec-2"], "Song 2"),
                                   ("02 Song 2.mp3", "Song 2", ["rec-from-elsewhere"], "Elsewhere"),
                                   ("03 Song 3.wav", "Song 3", ["rec-1"], "Song 1")]:
        rel, verdict = f"Some Band - Demo/{name}", "mismatch"
        fid = conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, duration, title, fingerprint, "
                           "fp_duration, scanned_at) VALUES (?, 'Some Band - Demo', 'mp3', 1, 0, 2.0, ?, ?, 2, 'now')",
                           (rel, tag, f"fp-{name}")).lastrowid
        conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, recordings, titles, looked_up_at) "
                     "VALUES (?, 2, 'ok', ?, ?, 'now')", (f"fp-{name}", json.dumps([{"id": r, "score": 0.9} for r in recs]),
                                                          json.dumps({recs[0]: {"title": heard}})))
        conn.execute("INSERT INTO verify (file_id, verdict) VALUES (?, ?)", (fid, verdict))
    conn.execute("UPDATE matches SET action = 'review', decision = 'approve', decided_album_id = 'rel-1', "
                 "decided_by = 'agent'")
    conn.commit()
    before = _snapshot(cfg.source_dir)
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "approved", progress=io.StringIO())
    assert "(D35)" in conn.execute("SELECT note FROM imports").fetchone()[0]  # flagged, not applied

    lib = importer.open_library()
    with pytest.raises(SystemExit, match="D35"):
        retag.apply(conn, lib, "Some Band - Demo/", "auto", "test", source=cfg.source_dir, promote=False)
    out = retag.apply(conn, lib, "Some Band - Demo/", "user", "test", source=cfg.source_dir)
    assert out == {"album_key": "Some Band - Demo/", "retagged": 1, "promoted": 1, "demoted": 1}

    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    names = sorted(p.name for p in album_dir.iterdir() if p.suffix in (".mp3", ".flac"))
    assert names == ["01 Real Title 1.flac", "02 Real Title 2.mp3", "02 Song 2.mp3"]
    assert EasyID3(album_dir / "02 Song 2.mp3")["title"] == ["Song 2"]   # the stray, tags untouched
    by_track = {i.mb_trackid: os.path.basename(os.fsdecode(i.path)) for i in lib.items()}
    assert by_track == {"rec-1": "01 Real Title 1.flac", "rec-2": "02 Real Title 2.mp3"}
    assert importer.current_paths(conn) == {
        "Some Band - Demo/01 Song 1.mp3": str(album_dir / "02 Real Title 2.mp3"),
        "Some Band - Demo/02 Song 2.mp3": str(album_dir / "02 Song 2.mp3"),
        "Some Band - Demo/03 Song 3.wav": str(album_dir / "01 Real Title 1.flac")}
    assert _snapshot(cfg.source_dir) == before                 # safety rules 1 and 2
    assert retag.plan(conn, lib, "Some Band - Demo/")["changes"] == []


def test_restore_puts_a_removed_file_back_on_its_track(env, monkeypatch):
    """MF DOOM, Live From Planet X: an Intro removed on a wrong scan length goes back on track 1."""
    cfg, conn, importer = env
    from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
    from beets.autotag.distance import Distance
    from beets.autotag.match import Proposal, Recommendation
    import beets.importer.tasks as tasks

    tracks = [TrackInfo(title=f"Real Title {i}", track_id=f"rec-{i}", index=i, medium=1,
                        medium_index=i, medium_total=3, length=2.0) for i in (1, 2, 3)]
    info = AlbumInfo(tracks=tracks, album="Real Album", album_id="rel-1", artist="Real Band",
                     artist_id="art-1", year=1999, mediums=1)

    def fake_tag_album(items, search_ids=()):
        items = sorted(items, key=lambda it: it.path)
        m = AlbumMatch(Distance(), info, dict(zip(items, tracks)), [], [])
        return "Some Band", "Demo", Proposal([m], Recommendation.medium)

    monkeypatch.setattr(tasks.autotag, "tag_album", fake_tag_album)
    monkeypatch.setattr("beets.metadata_plugins.album_for_id", lambda _id: info)
    conn.execute("UPDATE matches SET action = 'review', decision = 'approve', decided_album_id = 'rel-1', "
                 "decided_by = 'agent'")
    conn.commit()
    before = _snapshot(cfg.source_dir)
    importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", "approved", progress=io.StringIO())
    album_dir = cfg.library_dir / "Real Band" / "1999 - Real Album"
    rel = "Some Band - Demo/01 Song 1.mp3"
    importer.remove_from_library(conn, str(album_dir / "01 Real Title 1.mp3"), "test", "user")
    assert rel not in importer.current_paths(conn)

    with pytest.raises(SystemExit, match="user"):
        importer.restore(conn, cfg.source_dir, rel, "r", "agent", track=1)
    with pytest.raises(SystemExit, match="held by"):
        importer.restore(conn, cfg.source_dir, rel, "r", "user", track=2)
    dry = importer.restore(conn, cfg.source_dir, rel, "r", "user", track=1)
    assert dry["dry_run"] and not (album_dir / "01 Real Title 1.mp3").exists()
    out = importer.restore(conn, cfg.source_dir, rel, "r", "user", track=1, apply=True)
    assert out["restored"] == str(album_dir / "01 Real Title 1.mp3")
    lib = importer.open_library()
    assert {i.mb_trackid for i in lib.items()} == {"rec-1", "rec-2", "rec-3"}
    assert EasyID3(album_dir / "01 Real Title 1.mp3")["title"] == ["Real Title 1"]
    assert importer.current_paths(conn)[rel] == str(album_dir / "01 Real Title 1.mp3")
    with pytest.raises(SystemExit, match="already in the library"):
        importer.restore(conn, cfg.source_dir, rel, "r", "user")
    assert _snapshot(cfg.source_dir) == before                 # safety rules 1 and 2
