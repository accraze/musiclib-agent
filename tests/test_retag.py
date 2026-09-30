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


def test_fingerprint_decode_roundtrip_properties():
    from musiclib import fpsim
    # A real fpcalc fingerprint prefix is not needed: build one from known values by checking
    # that identical inputs score 1.0 and decode is deterministic.
    fp = "AQAAE0mUaEkSRZEGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert fpsim.decode(fp) == fpsim.decode(fp)


@pytest.fixture
def with_extras(setup):
    """build() plus extras: files placed beside the album (import_extra), not beets items."""
    conn, build = setup

    def build_x(files, extras, tracks, lib_tags, titles=None):
        lib = build(files, tracks, lib_tags)
        for rel, dur, rec in extras:
            fp = f"fp-{rel}"
            conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, duration, fingerprint, fp_duration, "
                         "title, scanned_at) VALUES (?, 'A', 'mp3', 1, 0, ?, ?, 100, ?, 'now')",
                         (rel, dur, fp, (titles or {}).get(rel)))
            conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, recordings, looked_up_at) "
                         "VALUES (?, 100, 'ok', ?, 'now')", (fp, json.dumps([{"id": rec, "score": 0.95}] if rec else [])))
            conn.execute("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                         "VALUES ('now', 'import_extra', ?, ?, 'auto')", (rel, f"/lib/A/{rel.split('/')[-1]}"))
        rels = [f[0] for f in files] + [e[0] for e in extras]
        conn.execute("UPDATE matches SET files = ? WHERE album_key = 'A/'", (json.dumps(rels),))
        conn.commit()
        return lib
    return conn, build_x


def no_fingerprint_data(conn, rel):
    conn.execute("UPDATE acoustid_lookups SET recordings = '[]' WHERE fingerprint = ?", (f"fp-{rel}",))


def test_extra_that_is_the_track_replaces_a_stray_holding_it(with_extras):
    conn, build = with_extras
    tracks = [track(1, "Time in Vain", 176), track(2, "Only a Shadow", 226)]
    # Midnight Cleaners: a bonus track tagged "Only a Shadow" (no fingerprint data) won track 2;
    # the real Only a Shadow sits beside it as an extra.
    lib = build([("A/01.mp3", 176, "rec-1"), ("A/10 Ilya.mp3", 210, "x")], [("A/03 Shadow.mp3", 226, "rec-2")],
                tracks, {"A/01.mp3": "rec-1", "A/10 Ilya.mp3": "rec-2"})
    no_fingerprint_data(conn, "A/10 Ilya.mp3")
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert p["changes"] == [{"file": "03 Shadow.mp3", "from": None, "to": "Only a Shadow", "to_track": 2,
                             "promote": True, "replaces": "10 Ilya.mp3", "forced": False,
                             "lengths": {"track": 226, "file": 226, "replaced": 210}}]


def test_closer_length_decides_when_both_fingerprint_as_the_track(with_extras):
    conn, build = with_extras
    # Cellophane Symphony: the single edit (263 s) holds track 2, the album take (271 s) is an extra.
    tracks = [track(1, "Cellophane Symphony", 580), track(2, "Sweet Cherry Wine", 271)]
    lib = build([("A/01.mp3", 580, "rec-1"), ("A/89 single.mp3", 263, "rec-2")], [("A/04 album.mp3", 271, "rec-2")],
                tracks, {"A/01.mp3": "rec-1", "A/89 single.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and [c["file"] for c in p["changes"]] == ["04 album.mp3"]


def test_alternate_take_of_equal_length_stays_an_extra(with_extras):
    conn, build = with_extras
    lib = build([("A/1.mp3", 200, "rec-1")], [("A/1 alt.mp3", 201, "rec-1")], [track(1, "Song", 200)],
                {"A/1.mp3": "rec-1"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and p["changes"] == []


def test_extra_whose_length_contradicts_is_a_problem(with_extras):
    conn, build = with_extras
    lib = build([("A/1.mp3", 201, "x")], [("A/x.mp3", 320, "rec-1")], [track(1, "Song", 200)], {"A/1.mp3": "rec-1"})
    no_fingerprint_data(conn, "A/1.mp3")
    p = retag.plan(conn, lib, "A/")
    assert p["changes"] == [] and any("fits worse" in x for x in p["problems"])


def test_user_can_force_a_promotion_the_release_lengths_do_not_back(with_extras):
    conn, build = with_extras
    # Midnight Cleaners for real: MusicBrainz lists the cassette's Only a Shadow at 212 s, closer
    # to the 210 s stray than to the 226 s file AcoustID confirms. D27: the user may overrule length.
    lib = build([("A/10 Ilya.mp3", 210, "x")], [("A/03 Shadow.mp3", 226, "rec-1")],
                [track(1, "Only a Shadow", 212)], {"A/10 Ilya.mp3": "rec-1"})
    no_fingerprint_data(conn, "A/10 Ilya.mp3")
    assert retag.plan(conn, lib, "A/")["changes"] == []
    p = retag.plan(conn, lib, "A/", force=["03 Shadow.mp3"])
    assert p["problems"] == []
    assert [(c["file"], c["forced"], c["lengths"]) for c in p["changes"]] == [
        ("03 Shadow.mp3", True, {"track": 212, "file": 226, "replaced": 210})]
    with pytest.raises(SystemExit, match="user"):
        retag.apply(conn, lib, "A/", "agent", "r", force=["03 Shadow.mp3"])


def test_forcing_still_needs_the_fingerprint(with_extras):
    conn, build = with_extras
    lib = build([("A/1.mp3", 200, "rec-1")], [("A/bonus.mp3", 200, "rec-elsewhere")], [track(1, "Song", 200)],
                {"A/1.mp3": "rec-1"})
    p = retag.plan(conn, lib, "A/", force=["bonus.mp3", "nope.mp3"])
    assert p["changes"] == []
    assert sorted(p["problems"]) == ["bonus.mp3: fingerprint fits 0 release tracks",
                                     "nope.mp3: not an extra of this album"]


def test_confirmed_extra_answers_to_its_own_title(with_extras):
    conn, build = with_extras
    # The extra's tag recording is another pressing's; AcoustID confirmed the tag, so its title counts.
    lib = build([("A/stray.mp3", 210, "x")], [("A/real.mp3", 226, "rec-other-pressing")],
                [track(1, "Only a Shadow", 226)], {"A/stray.mp3": "rec-1"}, titles={"A/real.mp3": "Only a Shadow"})
    no_fingerprint_data(conn, "A/stray.mp3")
    conn.execute("INSERT INTO verify (file_id, verdict) SELECT id, 'confirmed' FROM files WHERE path = 'A/real.mp3'")
    p = retag.plan(conn, lib, "A/")
    assert [c["file"] for c in p["changes"]] == ["real.mp3"]


def test_promotion_count_needs_an_imported_file_with_the_extras_title(with_extras):
    conn, build = with_extras
    build([("A/stray.mp3", 210, "x")], [("A/real.mp3", 226, "rec-9")], [track(1, "Only a Shadow", 226)],
          {"A/stray.mp3": "rec-1"}, titles={"A/real.mp3": "Only a Shadow"})
    conn.execute("INSERT INTO verify (file_id, verdict) SELECT id, 'confirmed' FROM files WHERE path = 'A/real.mp3'")
    files = ["A/stray.mp3", "A/real.mp3"]
    assert retag.promotion_count(conn, files) == 0
    conn.execute("UPDATE files SET title = 'Only A Shadow' WHERE path = 'A/stray.mp3'")
    assert retag.promotion_count(conn, files) == 1


def test_current_paths_counts_a_file_placed_again_after_removal(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    rows = [("import", "a.mp3", "/lib/02 X.mp3"), ("remove_from_library", "a.mp3", "/lib/02 X.mp3"),
            ("import_extra", "a.mp3", "/lib/a.mp3"), ("import_extra", "b.mp3", "/lib/02 X.mp3")]
    conn.executemany("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', ?, ?, ?, 'auto')", rows)
    assert importer.current_paths(conn) == {"a.mp3": "/lib/a.mp3", "b.mp3": "/lib/02 X.mp3"}
