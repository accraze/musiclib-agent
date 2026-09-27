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
