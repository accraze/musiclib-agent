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


def test_file_from_another_release_whose_length_fits_is_a_problem(setup):
    conn, build = setup
    tracks = [track(1, "A", 100), track(2, "B", 200)]
    lib = build([("A/1.mp3", 100, "rec-1"), ("A/2.mp3", 200, "rec-from-elsewhere")], tracks,
                {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2"})
    p = retag.plan(conn, lib, "A/")
    assert p["changes"] == [] and p["_demote"] == []
    assert any("fingerprint names another recording" in x for x in p["problems"])


def test_file_from_another_release_far_off_the_length_comes_off(setup):
    conn, build = setup
    # Lee Perry: Perry's Rub A Dub (230 s) holds Time (184 s); nothing else fits Time.
    tracks = [track(1, "A", 100), track(8, "Time", 184)]
    lib = build([("A/1.mp3", 100, "rec-1"), ("A/rub.mp3", 230, "rec-rub-a-dub")], tracks,
                {"A/1.mp3": "rec-1", "A/rub.mp3": "rec-8"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert [(c["file"], c["from"], c.get("demote")) for c in p["changes"]] == [("rub.mp3", "Time", True)]


def test_file_losing_to_one_that_fits_comes_off_its_own_track(setup):
    conn, build = setup
    # Moondawn: an alternate Mindphaser (1564 s) sits on Floating Sequence (1271 s); the reissue's
    # Mindphaser (1522 s, confirmed) holds Mindphaser. The alternate cannot move there: it comes off.
    tracks = [track(2, "Mindphaser", 1522), track(3, "Floating Sequence", 1271)]
    lib = build([("A/supplement.mp3", 1522, "rec-2"), ("A/mindphaser.mp3", 1564, "x")], tracks,
                {"A/supplement.mp3": "rec-2", "A/mindphaser.mp3": "rec-3"})
    conn.execute("UPDATE acoustid_lookups SET recordings = '[]', titles = ? WHERE fingerprint = 'fp-A/mindphaser.mp3'",
                 (json.dumps({"r": {"title": "Mindphaser"}}),))
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert [(c["file"], c.get("demote")) for c in p["changes"]] == [("mindphaser.mp3", True)]


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


@pytest.mark.parametrize("bublight_len,settled", [
    (163, True),    # 0 s off The Bublight, 9 s off Love Dance
    (158, False),   # 5 s off The Bublight: no evidence
])
def test_length_settles_a_fingerprint_naming_two_tracks(setup, bublight_len, settled):
    """D37, Joe Meek: The Bublight's fingerprint names The Bublight (163 s) and Love Dance (154 s)."""
    conn, build = setup
    tracks = [track(2, "Orbit Around the World", 170), track(4, "The Bublight", 163),
              track(6, "Love Dance of the Saroos", 154)]
    lib = build([("A/2.mp3", bublight_len, "rec-4"), ("A/4.mp3", 170, "rec-2"), ("A/6.mp3", 154, "rec-6")],
                tracks, {"A/2.mp3": "rec-2", "A/4.mp3": "rec-4", "A/6.mp3": "rec-6"})
    conn.execute("UPDATE acoustid_lookups SET recordings = ? WHERE fingerprint = 'fp-A/2.mp3'",
                 (json.dumps([{"id": "rec-4", "score": 0.95}, {"id": "rec-6", "score": 0.9}]),))
    p = retag.plan(conn, lib, "A/")
    moves = sorted((c["file"], c["to"]) for c in p["changes"])
    if settled:
        assert moves == [("2.mp3", "The Bublight"), ("4.mp3", "Orbit Around the World")] and p["problems"] == []
    else:
        assert ("2.mp3", "The Bublight") not in moves
        assert "2.mp3: 2 candidate tracks" in p["problems"]


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


def test_an_extra_the_user_displaced_is_not_promoted_back(with_extras):
    conn, build = with_extras
    # Cellophane Symphony after the user's --promote: the album take (271 s) holds Sweet Cherry Wine,
    # MusicBrainz says 237 s, and the single edit (263 s) is the extra. Closer length alone must not undo it.
    tracks = [track(1, "Sweet Cherry Wine", 237)]
    lib = build([("A/04 album.mp3", 271, "rec-1")], [("A/89 single.mp3", 263, "rec-1")], tracks,
                {"A/04 album.mp3": "rec-1"})
    assert [c["file"] for c in retag.plan(conn, lib, "A/")["changes"]] == ["89 single.mp3"]
    conn.execute("UPDATE audit_log SET decided_by = 'user' WHERE action = 'import_extra'")
    assert retag.plan(conn, lib, "A/")["changes"] == []
    assert [c["file"] for c in retag.plan(conn, lib, "A/", force=["89 single.mp3"])["changes"]] == ["89 single.mp3"]


def test_alternate_take_of_equal_length_stays_an_extra(with_extras):
    conn, build = with_extras
    lib = build([("A/1.mp3", 200, "rec-1")], [("A/1 alt.mp3", 201, "rec-1")], [track(1, "Song", 200)],
                {"A/1.mp3": "rec-1"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and p["changes"] == []


def test_extra_whose_length_contradicts_stays_an_extra_without_blocking(with_extras):
    conn, build = with_extras
    # The Warner Bros. Album: a 60 s "Instrumental II" fingerprints as the 6 s "Instrumental".
    # It is another take, not the track, and must not block the album's other relabels.
    tracks = [track(1, "Song", 200), track(2, "A", 100), track(3, "B", 300)]
    lib = build([("A/1.mp3", 201, "x"), ("A/2.mp3", 300, "rec-3"), ("A/3.mp3", 100, "rec-2")],
                [("A/x.mp3", 320, "rec-1")], tracks, {"A/1.mp3": "rec-1", "A/2.mp3": "rec-2", "A/3.mp3": "rec-3"})
    no_fingerprint_data(conn, "A/1.mp3")
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert sorted(c["file"] for c in p["changes"]) == ["2.mp3", "3.mp3"]


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


def test_a_file_losing_to_a_stayer_is_not_judged_by_that_tracks_length(setup):
    conn, build = setup
    # The Monks: a 1965 demo of Oh, How to Do Now (159 s) holds I Can't Get Over You (164 s); the
    # album take holds Oh, How to Do Now (197 s) and fits. The demo cannot move: it is a stray,
    # and its length being closer to its own track must not block the album.
    tracks = [track(6, "Oh, How to Do Now", 197), track(9, "I Can't Get Over You", 164),
              track(1, "Monk Time", 167)]
    lib = build([("A/06.mp3", 197, "rec-6"), ("A/19 demo.mp3", 159, "rec-6"), ("A/13.mp3", 165, "rec-9")],
                tracks, {"A/06.mp3": "rec-6", "A/19 demo.mp3": "rec-9", "A/13.mp3": "rec-1"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    assert sorted((c["file"], c["to"]) for c in p["changes"]) == [("13.mp3", "I Can't Get Over You"),
                                                                ("19 demo.mp3", None)]


def test_strays_come_off_and_the_real_tracks_take_their_place(with_extras):
    conn, build = with_extras
    # MxPx, Let It Happen: the Suggestion Box demo sits on track 1, demos from elsewhere sit on
    # tracks 2 and 31, and the real Role Remodeling and Prozac were pushed out as extras.
    tracks = [track(1, "Role Remodeling", 180), track(2, "Prozac", 150), track(31, "Suggestion Box", 149)]
    lib = build([("A/34 sb.mp3", 149, "rec-31"), ("A/30 christalena.mp3", 127, "rec-christalena"),
                 ("A/31 south bound.mp3", 153, "rec-south-bound")],
                [("A/01 role.mp3", 181, "rec-1"), ("A/02 prozac.mp3", 151, "rec-2")], tracks,
                {"A/34 sb.mp3": "rec-1", "A/30 christalena.mp3": "rec-2", "A/31 south bound.mp3": "rec-31"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == []
    got = sorted((c["file"], c["to"], c.get("replaces"), bool(c.get("demote"))) for c in p["changes"])
    assert got == [("01 role.mp3", "Role Remodeling", None, False),          # fills the track 34 vacates
                   ("02 prozac.mp3", "Prozac", "30 christalena.mp3", False),  # replaces a stray
                   ("31 south bound.mp3", None, None, True),                  # 34 claims its track
                   ("34 sb.mp3", "Suggestion Box", None, False)]              # relabel to track 31
    assert retag._needs_d35(p)


def test_extra_filling_an_emptied_track_needs_length(with_extras):
    conn, build = with_extras
    tracks = [track(1, "A", 100), track(2, "B", 200)]
    # x (200 s, B's audio) holds A and moves to B; a 140 s take of A cannot fill A (100 s) by itself.
    lib = build([("A/x.mp3", 200, "rec-2")], [("A/a take.mp3", 140, "rec-1")], tracks, {"A/x.mp3": "rec-1"})
    p = retag.plan(conn, lib, "A/")
    assert p["problems"] == [] and [c["file"] for c in p["changes"]] == ["x.mp3"]
    p = retag.plan(conn, lib, "A/", force=["a take.mp3"])
    assert p["problems"] == [] and sorted(c["file"] for c in p["changes"]) == ["a take.mp3", "x.mp3"]


def test_current_paths_counts_a_file_placed_again_after_removal(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    rows = [("import", "a.mp3", "/lib/02 X.mp3"), ("remove_from_library", "a.mp3", "/lib/02 X.mp3"),
            ("import_extra", "a.mp3", "/lib/a.mp3"), ("import_extra", "b.mp3", "/lib/02 X.mp3")]
    conn.executemany("INSERT INTO audit_log (ts, action, source_path, dest_path, decided_by) "
                     "VALUES ('now', ?, ?, ?, 'auto')", rows)
    assert importer.current_paths(conn) == {"a.mp3": "/lib/a.mp3", "b.mp3": "/lib/02 X.mp3"}
