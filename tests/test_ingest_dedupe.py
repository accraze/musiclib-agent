"""D30: an inbox batch against itself, the library (via the originals) and the dump."""

import json

import pytest

from musiclib import acoustid, db, importer, ingest, verify

ROOT = "/inbox/.processed/2026-09-30/Drop"


def add(conn, path, aid, *, top="", sha=None, lossless=0, bitrate=320000, album=None, size=100,
        fp=None, duration=120.0):
    fp = fp or f"fp-{path}"
    conn.execute(
        "INSERT INTO files (path, top_dir, ext, size, mtime, sha256, lossless, bitrate, mb_albumid, "
        "fingerprint, fp_duration, duration, scanned_at) VALUES (?, ?, 'x', ?, 0, ?, ?, ?, ?, ?, 100, ?, 'now')",
        (path, top, size, sha or f"sha-{path}", lossless, bitrate, album, fp, duration))
    conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, acoustid_id, "
                 "recordings, looked_up_at) VALUES (?, 100, 'ok', ?, '[]', 'now')", (fp, aid))


def inbox(conn, rel, aid, **kw):
    add(conn, f"{ROOT}/{rel}", aid, top=ROOT, **kw)


def library_album(conn, key, n, *, release="R1", lossless=0, bitrate=320000, prefix="t"):
    """A dump folder imported into the library the way musiclib import records it."""
    files = [f"{key}/{i}.mp3" for i in range(n)]
    for i, f in enumerate(files):
        add(conn, f, f"{prefix}{i}", lossless=lossless, bitrate=bitrate, album=release)
    mid = conn.execute("INSERT INTO matches (album_key, dirs, files, action, matched_at) "
                       "VALUES (?, ?, ?, 'auto', 'now')", (key + "/", json.dumps([key + "/"]),
                                                            json.dumps(files))).lastrowid
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', ?, ?, ?, NULL, 'now')",
                 (mid, release, f"/lib/{key}", n))
    conn.executemany("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', 'import', ?, ?, 'auto')",
                     [(f, f"/lib/{key}/{i:02d} x.mp3") for i, f in enumerate(files)])


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    acoustid.migrate(c)
    importer.migrate(c)
    ingest.migrate(c)
    c.execute("INSERT INTO ingest_batches (folder, path, status, claimed_at, claimed_by) "
              "VALUES ('Drop', ?, 'scanned', 'now', 'user')", (ROOT,))
    return c


def run(conn):
    verify.run(conn)
    return {(i["path"].removeprefix(ROOT + "/"), i["outcome"], i["kind"]): i
            for i in ingest.dedupe(conn, 1)["items"]}


def test_same_album_same_quality_is_skipped(conn):
    library_album(conn, "Lib Album", 8)
    for i in range(8):
        inbox(conn, f"New/{i}.mp3", f"t{i}", album="R1")
    out = run(conn)
    assert list(out) == [("New/", "skip", "duplicate")]
    assert out[("New/", "skip", "duplicate")]["other"] == "/lib/Lib Album/"


def test_flac_over_library_mp3_is_an_upgrade(conn):
    library_album(conn, "Lib Album", 8)
    for i in range(8):
        inbox(conn, f"New/{i}.flac", f"t{i}", lossless=1, bitrate=900000)
    assert list(run(conn)) == [("New/", "review", "upgrade")]


def test_worse_copy_is_skipped(conn):
    library_album(conn, "Lib Album", 8, lossless=1, bitrate=900000)
    for i in range(8):
        inbox(conn, f"New/{i}.mp3", f"t{i}", bitrate=128000)
    assert list(run(conn)) == [("New/", "skip", "duplicate")]


def test_bonus_track_goes_to_review(conn):
    library_album(conn, "Lib Album", 9)
    for i in range(9):
        inbox(conn, f"New/{i}.mp3", f"t{i}")
    inbox(conn, "New/bonus.mp3", "bonus")
    assert list(run(conn)) == [("New/", "review", "extra_tracks")]


def test_other_release_is_an_edition(conn):
    library_album(conn, "Lib Album", 8, release="R1")
    for i in range(8):
        inbox(conn, f"New/{i}.mp3", f"t{i}", album="R2")
    assert list(run(conn)) == [("New/", "review", "edition")]


def test_compilation_sharing_one_track_is_kept_except_identical_bytes(conn):
    library_album(conn, "Lib Album", 8)
    for i in range(12):
        inbox(conn, f"Comp/{i}.mp3", f"c{i}")
    inbox(conn, "Comp/same.mp3", "t0", sha="sha-Lib Album/0.mp3")   # byte-identical original
    inbox(conn, "Comp/reencode.mp3", "t1")                          # same audio, other bytes: kept
    out = run(conn)
    assert list(out) == [("Comp/same.mp3", "skip", "identical")]


def test_copies_inside_the_batch(conn):
    for i in range(6):
        inbox(conn, f"MP3/{i}.mp3", f"t{i}")
        inbox(conn, f"FLAC/{i}.flac", f"t{i}", lossless=1, bitrate=900000)
    out = run(conn)
    assert list(out) == [("MP3/", "skip", "in_batch")]
    assert out[("MP3/", "skip", "in_batch")]["other"] == f"{ROOT}/FLAC/"


def test_overlap_with_unimported_dump_album_is_only_flagged(conn):
    for i in range(6):
        add(conn, f"Dump Album/{i}.mp3", f"t{i}")   # in the dump, never imported
        inbox(conn, f"New/{i}.mp3", f"t{i}")
    assert list(run(conn)) == [("New/", "flag", "dump_overlap")]


def test_imported_dump_album_is_not_flagged_as_dump_overlap(conn):
    library_album(conn, "Lib Album", 6)
    add(conn, "Lib Album/skipped extra.mp3", "t0")  # a leftover of an imported folder
    for i in range(6):
        inbox(conn, f"New/{i}.mp3", f"t{i}")
    assert list(run(conn)) == [("New/", "skip", "duplicate")]


def test_unrelated_album_has_no_rows_and_rerun_replaces(conn):
    library_album(conn, "Lib Album", 6)
    for i in range(6):
        inbox(conn, f"New/{i}.mp3", f"n{i}")
    assert run(conn) == {}
    assert run(conn) == {}
    assert ingest.batch(conn, 1)["status"] == "deduped"


@pytest.fixture
def same_audio(monkeypatch):
    """Fake fingerprints 'audio:X' match when X agrees (fpsim needs real Chromaprint data)."""
    from musiclib import fpsim
    monkeypatch.setattr(fpsim, "similarity", lambda a, b, **kw: float(a.split(":")[-1] == b.split(":")[-1]))


def test_remaster_with_split_acoustids_is_still_a_duplicate(conn, same_audio):
    """Leave Home case: AcoustID gives 3 of 14 remastered tracks new ids (79% < 90%)."""
    library_album(conn, "Lib Album", 14)
    for i in range(14):
        aid = f"t{i}" if i >= 3 else f"remaster{i}"
        inbox(conn, f"New/{i}.mp3", aid, fp=f"audio:{i}", duration=120.0 + (2 if i < 3 else 0))
    for i in range(3):  # give the library originals matching fake fingerprints
        conn.execute("UPDATE files SET fingerprint = ? WHERE path = ?", (f"lib:{i}", f"Lib Album/{i}.mp3"))
        conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, acoustid_id, recordings, "
                     "looked_up_at) VALUES (?, 100, 'ok', ?, '[]', 'now')", (f"lib:{i}", f"t{i}"))
    assert list(run(conn)) == [("New/", "skip", "duplicate")]


def test_fingerprints_do_not_rescue_a_weak_overlap(conn, same_audio):
    library_album(conn, "Lib Album", 14)
    for i in range(14):  # only 5 of 14 shared by AcoustID: below REFINE_SHARE, not compared
        inbox(conn, f"New/{i}.mp3", f"t{i}" if i < 5 else f"other{i}", fp=f"audio:{i}")
    assert run(conn) == {}


def test_different_audio_under_split_ids_stays_extra(conn, same_audio):
    library_album(conn, "Lib Album", 12)
    for i in range(12):
        inbox(conn, f"New/{i}.mp3", f"t{i}" if i < 11 else "new-song", fp=f"audio:x{i}")
    assert list(run(conn)) == [("New/", "review", "extra_tracks")]


def test_dedupe_needs_a_scan(conn):
    conn.execute("UPDATE ingest_batches SET status = 'claimed'")
    with pytest.raises(SystemExit, match="not scanned"):
        ingest.dedupe(conn, 1)
