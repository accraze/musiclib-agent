import io
import json

import pytest

from musiclib import beetsenv, config, db, dupes, verify

pytest.importorskip("beets")


@pytest.fixture
def env(tmp_path):
    src = tmp_path / "dump"
    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{src}"\nstate_dir = "{tmp_path}/state"\nlibrary_dir = "{tmp_path}/lib"\n')
    cfg = config.load(tmp_path / "musiclib.toml")
    beetsenv.setup(cfg)
    from musiclib import match

    conn = db.connect(cfg.db_path)
    from musiclib import acoustid
    acoustid.migrate(conn)
    return src, conn, match


def add(src, conn, rel, aid, **kw):
    p = src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    fp = f"fp-{rel}"
    conn.execute(
        "INSERT INTO files (path, top_dir, ext, size, mtime, sha256, lossless, bitrate, fingerprint, "
        "fp_duration, scanned_at) VALUES (?, '', 'mp3', 1, 0, ?, ?, ?, ?, 100, 'now')",
        (rel, f"sha-{rel}", kw.get("lossless", 0), kw.get("bitrate", 320000), fp))
    conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, acoustid_id, "
                 "recordings, looked_up_at) VALUES (?, 100, 'ok', ?, '[]', 'now')", (fp, aid))


def test_config_isolated_from_global_beets(env, tmp_path):
    import beets
    assert beets.config["directory"].get() == str(tmp_path / "lib")
    assert beets.config["import"]["copy"].get() is True
    assert beets.config["import"]["move"].get() is False


def test_albums_skip_dropped_copies_and_merge_discs(env):
    src, conn, match = env
    for i in range(3):
        add(src, conn, f"Good FLAC/{i}.flac", f"t{i}", lossless=1)
        add(src, conn, f"Bad MP3/{i}.mp3", f"t{i}", bitrate=128000)
        add(src, conn, f"Box/CD1/{i}.mp3", f"a{i}")
        add(src, conn, f"Box/CD2/{i}.mp3", f"b{i}")
    (src / "Good FLAC" / "cover.jpg").write_bytes(b"jpg")  # not in inventory: ignored
    verify.run(conn)
    dupes.find(conn)
    got = {key: (dirs, files) for key, dirs, files in match.albums(conn, src)}
    assert set(got) == {"Good FLAC/", "Box/"}
    dirs, files = got["Box/"]
    assert set(dirs) >= {"Box/CD1/", "Box/CD2/"} and len(files) == 6
    assert got["Good FLAC/"][1] == [f"Good FLAC/{i}.flac" for i in range(3)]


def test_run_records_actions_and_resumes(env, monkeypatch):
    src, conn, match = env
    for i in range(2):
        add(src, conn, f"A/{i}.mp3", f"a{i}")
        add(src, conn, f"B/{i}.mp3", f"b{i}")
        add(src, conn, f"C/{i}.mp3", f"c{i}")
    verify.run(conn)
    dupes.find(conn)
    results = {"A/": ("strong", [{"album_id": "r1"}]), "B/": ("medium", [{"album_id": "r2"}]),
               "C/": ("none", [])}

    def fake(source, files, search_id):
        rec, cands = results[files[0].split("/")[0] + "/"]
        if not cands:
            return {"recommendation": rec, "candidates": "[]", "action": "unsorted"}
        return {"recommendation": rec, "candidates": json.dumps(cands),
                "action": "auto" if rec == "strong" else "review", "album_id": cands[0]["album_id"]}

    monkeypatch.setattr(match, "match_album", fake)
    res = match.run(conn, src, progress=io.StringIO())
    assert (res["auto"], res["review"], res["unsorted"]) == (1, 1, 1)
    assert match.run(conn, src, progress=io.StringIO())["matched"] == 0


def test_merge_combines_folders_and_rematches(env, monkeypatch):
    src, conn, match = env
    for disc in (1, 2):
        for i in range(2):
            add(src, conn, f"Box cd {disc}/{i}.mp3", f"d{disc}t{i}")
    verify.run(conn)
    dupes.find(conn)
    same = json.dumps([{"album_id": "box", "artist": "A", "album": "Box", "year": 1991, "country": "US",
                        "media": "CD", "tracks": 4, "distance": 0.5, "penalties": ["missing_tracks"],
                        "extra_items": 0, "extra_tracks": 2}])
    monkeypatch.setattr(match, "match_album",
                        lambda s, files, sid: {"recommendation": "strong" if len(files) == 4 else "none",
                                               "candidates": same, "action": "auto" if len(files) == 4 else "review",
                                               "album_id": "box", "distance": 0.01 if len(files) == 4 else 0.5})
    match.run(conn, src, progress=io.StringIO())
    [sug] = match.merge_suggestions(conn)
    assert (sug["release"], sug["albums"], sug["files"]) == ("box", ["Box cd 1/", "Box cd 2/"], 4)
    out = match.merge(conn, src, ["Box cd 1/", "Box cd 2/"], apply=True)
    assert out["files"] == 4 and out["action"] == "auto"
    keys = [r[0] for r in conn.execute("SELECT album_key FROM matches")]
    assert keys == ["Box cd 1/"]
