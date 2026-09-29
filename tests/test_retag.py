import json
from types import SimpleNamespace as NS

import pytest

from musiclib import acoustid, db, importer, retag, verify

pytest.importorskip("beets")


def track(i, title, length):
    return NS(index=i, title=title, track_id=f"rec-{i}", length=length)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "musiclib.db")
    acoustid.migrate(conn)
    importer.migrate(conn)
    conn.executescript(verify.SCHEMA)

    def build(files, tracks, lib_tags):
        """files: [(rel, duration, acoustid recording id)], lib_tags: rel -> recording id beets applied."""
        info = NS(tracks=tracks)
        monkeypatch.setattr("beets.metadata_plugins.album_for_id", lambda _id: info)
        rels = [f[0] for f in files]
        mid = conn.execute("INSERT INTO matches (album_key, dirs, files, action, matched_at) VALUES "
                           "('A/', '[]', ?, 'auto', 'now')", (json.dumps(rels),)).lastrowid
        conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel', '/lib/A', ?, NULL, 'now')",
                     (mid, len(files)))
        items = []
        for rel, dur, rec in files:
            fp = f"fp-{rel}"
            conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, duration, fingerprint, fp_duration, "
                         "scanned_at) VALUES (?, 'A', 'mp3', 1, 0, ?, ?, 100, 'now')", (rel, dur, fp))
            conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, recordings, looked_up_at) "
                         "VALUES (?, 100, 'ok', ?, 'now')", (fp, json.dumps([{"id": rec, "score": 0.95}])))
            path = f"/lib/A/{rel.split('/')[-1]}"
            conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                         "VALUES ('now', 'import', ?, ?, 'auto')", (rel, path))
            items.append(NS(path=path.encode(), mb_trackid=lib_tags[rel]))
        conn.commit()
        return NS(items=lambda: items)
    return conn, build


def test_swap_confirmed_by_length_is_planned(setup):
    conn, build = setup
    tracks = [track(1, "Honey", 200), track(2, "James", 300)]
    # Files carry each other's titles: the 300 s file is tagged Honey, the 200 s file James.
    lib = build([("A/1 Honey.mp3", 300, "rec-2"), ("A/2 James.mp3", 200, "rec-1")], tracks,
                {"A/1 Honey.mp3": "rec-1", "A/2 James.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert sorted((c["file"], c["to"]) for c in p["changes"]) == [("1 Honey.mp3", "James"), ("2 James.mp3", "Honey")]


def test_length_contradicting_acoustid_blocks_the_change(setup):
    conn, build = setup
    tracks = [track(1, "Autobiography", 417), track(2, "Socca", 356)]
    # AcoustID claims a swap, but each file's length fits its current title: AcoustID is wrong.
    lib = build([("A/1.mp3", 417, "rec-2"), ("A/2.mp3", 356, "rec-1")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert any("length" in x for x in p["problems"])


def test_equal_length_tracks_rely_on_fingerprints(setup):
    conn, build = setup
    tracks = [track(1, "Part 1", 598), track(2, "Part 2", 598)]
    lib = build([("A/1.mp3", 600, "rec-2"), ("A/2.mp3", 600, "rec-1")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and len(p["changes"]) == 2


def test_file_from_another_release_is_a_problem(setup):
    conn, build = setup
    tracks = [track(1, "A", 100), track(2, "B", 200)]
    lib = build([("A/1.mp3", 100, "rec-1"), ("A/2.mp3", 200, "rec-from-elsewhere")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert any("0 candidate tracks" in x for x in p["problems"])


def test_tmp_name_keeps_extension():
    from pathlib import Path
    assert retag.tmp_name(Path("/l/05 Song.flac")) == Path("/l/05 Song.retag-tmp.flac")


def test_current_paths_follows_retags_and_removals(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    rows = [("import", "a.mp3", "/lib/01 A.mp3"), ("import", "b.mp3", "/lib/02 B.mp3"),
            ("retag_by_fingerprint", "a.mp3", "/lib/02 B2.mp3"), ("import", "c.mp3", "/lib/03 C.mp3"),
            ("remove_from_library", "c.mp3", "/lib/03 C.mp3")]
    conn.executemany("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', ?, ?, ?, 'auto')", rows)
    assert importer.current_paths(conn) == {"a.mp3": "/lib/02 B2.mp3", "b.mp3": "/lib/02 B.mp3"}


def test_after_import_does_nothing_without_swap_evidence(setup):
    conn, build = setup
    tracks = [track(1, "A", 100), track(2, "B", 200)]
    lib = build([("A/1.mp3", 100, "rec-1"), ("A/2.mp3", 200, "rec-2")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    assert retag.after_import(conn, lib, "A/", ["A/1.mp3", "A/2.mp3"]) is None


def test_file_whose_audio_fits_its_tag_stays_even_if_ambiguous(setup):
    conn, build = setup
    tracks = [track(1, "Intro", 60), track(2, "Song", 200), track(3, "Song (reprise)", 200)]
    lib = build([("A/1.mp3", 60, "rec-1"), ("A/2.mp3", 200, "rec-2")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    # 2.mp3's fingerprint also links to track 3's recording; its tag (track 2) is among them.
    conn.execute("UPDATE acoustid_lookups SET recordings = ? WHERE fingerprint = 'fp-A/2.mp3'",
                 (json.dumps([{"id": "rec-2", "score": 0.95}, {"id": "rec-3", "score": 0.95}]),))
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and p["changes"] == []


@pytest.mark.parametrize("text,expected", [
    ("Игорь Фёдорович Стравинский", True), ("久石譲", True), ("Béla Bartók", False),
    ("Leftöver Crack", False), ("Bonnie “Prince” Billy", False), ("Melt‐Banana", False), (None, False),
])
def test_non_latin(text, expected):
    from musiclib import resync
    assert resync.non_latin(text) is expected
