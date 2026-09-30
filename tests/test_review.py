import json

import pytest

from musiclib import db, importer, review


def cand(album_id, distance, penalties=(), extra_items=0, extra_tracks=0):
    return {"album_id": album_id, "artist": "A", "album": "B", "year": 2000, "country": "US",
            "media": "CD", "tracks": 10, "distance": distance, "penalties": list(penalties),
            "extra_items": extra_items, "extra_tracks": extra_tracks}


@pytest.mark.parametrize("kind,cands,expected", [
    ("close", [cand("r1", 0.05, ["artist"]), cand("r2", 0.4)], "approve"),
    ("close", [cand("r1", 0.05, ["artist"]), cand("r2", 0.1)], "look"),          # runner-up close
    ("close", [cand("r1", 0.05, ["unmatched_tracks"]), cand("r2", 0.6)], "look"),  # structural
    ("close", [cand("r1", 0.05, [], extra_tracks=1), cand("r2", 0.6)], "look"),   # incomplete
    ("none", [cand("r1", 0.7)], "asis"),
    ("error", [], "skip"),
])
def test_suggest(kind, cands, expected):
    assert review.suggest(kind, cands)[0] == expected


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "musiclib.db")
    importer.migrate(c)
    for key, dist, action in [("Close/", 0.05, "review"), ("Far/", 0.8, "review"), ("Auto/", 0.0, "auto")]:
        c.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, "
                  "album_id, candidates, matched_at) VALUES (?, '[]', '[]', ?, 'medium', ?, ?, ?, 'now')",
                  (key, action, dist, f"rel-{key}", json.dumps([cand(f"rel-{key}", dist)])))
    c.commit()
    return c


def test_listing_and_stats(conn):
    assert [a["album_key"] for a in review.listing(conn, "close")["albums"]] == ["Close/"]
    assert [a["album_key"] for a in review.listing(conn, "none")["albums"]] == ["Far/"]
    assert review.stats(conn)["close"]["open"] == 1


def test_decide_feeds_import_selection_and_audit(conn):
    out = review.decide(conn, [
        {"album_key": "Close/", "decision": "approve", "reason": "clear winner"},
        {"album_key": "Far/", "decision": "asis", "reason": "no match"}], "agent")
    assert out["recorded"] == 2
    approved = importer.select(conn, "approved")
    assert [(a["album_key"], a["pin"], a["decided_by"]) for a in approved] == [("Close/", "rel-Close/", "agent")]
    assert [a["album_key"] for a in importer.select(conn, "unsorted")] == ["Far/"]
    assert review.listing(conn, "close")["remaining"] == 0
    actions = {r[0] for r in conn.execute("SELECT action FROM audit_log")}
    assert actions == {"decide_approve", "decide_asis"}


def test_decide_is_all_or_nothing(conn):
    with pytest.raises(SystemExit):
        review.decide(conn, [
            {"album_key": "Close/", "decision": "approve", "reason": "ok"},
            {"album_key": "Nope/", "decision": "approve", "reason": "typo"}], "agent")
    assert review.stats(conn)["close"]["approve"] == 0


def test_decide_requires_reason(conn):
    with pytest.raises(SystemExit):
        review.decide(conn, [{"album_key": "Close/", "decision": "approve"}], "user")


def test_album_in_a_dupe_review_group_is_never_a_close_call(conn):
    from musiclib import dupes
    conn.executescript(dupes.SCHEMA)
    conn.execute("UPDATE matches SET dirs = '[\"Close/\"]' WHERE album_key = 'Close/'")
    gid = conn.execute("INSERT INTO dupe_groups (tier, scope, action, reason, keeper, reclaimable) "
                       "VALUES (3, 'folder', 'review', 'editions', 'Other/', 1)").lastrowid
    conn.executemany("INSERT INTO dupe_members (group_id, path, role) VALUES (?, ?, ?)",
                     [(gid, "Other/", "keep"), (gid, "Close/", "drop")])
    conn.commit()
    assert review.listing(conn, "close")["albums"] == []
    assert [a["album_key"] for a in review.listing(conn, "dupe")["albums"]] == ["Close/"]


def _album(cands, confirmed=10, mismatch=0, files=10):
    return {"candidates": cands, "files": files,
            "local": {"verify": {"confirmed": confirmed, "mismatch": mismatch}}}


@pytest.mark.parametrize("album,meta,ok", [
    (_album([cand("r1", 0.02, ["missing_tracks"], extra_tracks=1), cand("r2", 0.4)]), [], True),
    (_album([cand("r1", 0.02, ["missing_tracks"], extra_tracks=1), cand("r2", 0.1)]), [], False),   # gap
    (_album([cand("r1", 0.02), cand("r2", 0.5)], confirmed=8), [], False),                        # 80% confirmed
    (_album([cand("r1", 0.02), cand("r2", 0.5)], mismatch=2), [], False),
    (_album([cand("r1", 0.02, [], extra_items=1), cand("r2", 0.3)]), [], False),   # runner-up fits exactly
    (_album([cand("r1", 0.02, ["album_id"]), cand("r2", 0.5)]), [], False),        # structural penalty
    (_album([cand("r1", 0.02), cand("r2", 0.5)]), [("mp4", None)], False),         # video file
    (_album([cand("r1", 0.02), cand("r2", 0.5)]), [("mp3", "fingerprint: x")], False),
])
def test_d21_check(album, meta, ok):
    assert review._d21(album, meta)[0] is ok  # D21 alone; D22 is tested below


@pytest.mark.parametrize("album,ok", [
    (_album([cand("r1", 0.04, ["artist"]), cand("r2", 0.7)], confirmed=0), True),         # exact, no data
    (_album([cand("r1", 0.04), cand("r2", 0.25)], confirmed=0), False),                  # gap < 0.3
    (_album([cand("r1", 0.04), cand("r2", 0.7)], confirmed=0, mismatch=1), False),        # a mismatch
    (_album([cand("r1", 0.04, ["missing_tracks"], extra_tracks=1), cand("r2", 0.7)], confirmed=0), False),
])
def test_d22_exact_fit_without_acoustid_data(album, ok):
    got, why = review.d21_check(album, [])
    assert got is ok
    if ok:
        assert why.startswith("D22")


def test_album_whose_release_is_already_imported_is_never_auto_approved(conn):
    # Neither album has AcoustID data, so a shared-audio check can't rule a duplicate out.
    conn.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, album_id, "
                 "candidates, matched_at) VALUES ('Copy/', '[]', '[\"c.mp3\"]', 'review', 'medium', 0.02, 'rel-Auto/', ?, 'now')",
                 (json.dumps([cand("rel-Auto/", 0.02), cand("r9", 0.6)]),))
    auto_id = conn.execute("SELECT id FROM matches WHERE album_key = 'Auto/'").fetchone()[0]
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel-Auto/', '/lib/x', 1, NULL, 'now')",
                 (auto_id,))
    conn.commit()
    [a] = [a for a in review.listing(conn, "close")["albums"] if a["album_key"] == "Copy/"]
    assert a["already_in_library"] == ["Auto/"] and a["suggest"] == "skip"
    assert review.d21_check(a, [])[0] is False


def test_poor_candidate_already_imported_is_not_flagged(conn):
    conn.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, album_id, "
                 "candidates, matched_at) VALUES ('Vol3/', '[]', '[]', 'review', 'none', 0.53, 'rel-x', ?, 'now')",
                 (json.dumps([cand("rel-x", 0.53), cand("rel-Auto/", 0.64)]),))
    auto_id = conn.execute("SELECT id FROM matches WHERE album_key = 'Auto/'").fetchone()[0]
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel-Auto/', '/lib/x', 1, NULL, 'now')",
                 (auto_id,))
    conn.commit()
    [a] = [a for a in review.listing(conn, "none")["albums"] if a["album_key"] == "Vol3/"]
    assert a["already_in_library"] == []


def _none_album(local_album, cands, already=()):
    return {"candidates": cands, "files": 5, "already_in_library": list(already),
            "local": {"album": local_album, "verify": {}}}


@pytest.mark.parametrize("album,meta,ok", [
    (_none_album("Heat", [dict(cand("r", 0.58), album="Tekvision")]), [("mp3", None)], True),
    (_none_album("The Acid Test Reels 1966", [dict(cand("r", 0.52), album="The Acid Test Reels 1966")]), [], False),
    (_none_album("Bartók at the Piano", [dict(cand("r", 0.50), album="Bartók plays Bartók: Bartók at the Piano")]), [], False),
    (_none_album("Vol 2", [dict(cand("r", 0.51), album="Other")], already=["X/"]), [], False),
    (_none_album("Clips", [dict(cand("r", 0.6), album="Other")]), [("mp4", None)], False),
    (_none_album("Near", [dict(cand("r", 0.45), album="Other")]), [], False),
    (_none_album("Cup of Tea Sessions", [dict(cand("r", 0.6), album="Up")]), [], True),        # short title
    (_none_album("Merzbox", [dict(cand("r", 0.6), album="Merzbox")]), [], False),              # exact title
])
def test_d24_check(album, meta, ok):
    assert review.d24_check(album, meta)[0] is ok


def test_imported_best_candidate_without_shared_audio_is_not_a_duplicate(conn):
    from musiclib import acoustid
    acoustid.migrate(conn)
    for path, aid in (("vol5/1.mp3", "a5"), ("vol8/1.mp3", "a8")):
        conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, fingerprint, fp_duration, scanned_at) "
                     "VALUES (?, 'x', 'mp3', 1, 0, ?, 100, 'now')", (path, f"fp-{path}"))
        conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, acoustid_id, looked_up_at) "
                     "VALUES (?, 100, 'ok', ?, 'now')", (f"fp-{path}", aid))
    conn.execute("UPDATE matches SET files = '[\"vol8/1.mp3\"]' WHERE album_key = 'Auto/'")
    conn.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, album_id, "
                 "candidates, matched_at) VALUES ('Vol5/', '[]', '[\"vol5/1.mp3\"]', 'review', 'none', 0.53, "
                 "'rel-Auto/', ?, 'now')", (json.dumps([cand("rel-Auto/", 0.53)]),))
    auto_id = conn.execute("SELECT id FROM matches WHERE album_key = 'Auto/'").fetchone()[0]
    conn.execute("INSERT INTO imports VALUES (?, NULL, 'apply', 'imported', 'rel-Auto/', '/lib/x', 1, NULL, 'now')",
                 (auto_id,))
    conn.commit()
    [a] = [a for a in review.listing(conn, "none")["albums"] if a["album_key"] == "Vol5/"]
    assert a["already_in_library"] == []


@pytest.mark.parametrize("dist,ok,rule", [(0.15, True, "D26"), (0.25, False, None), (0.05, True, "D22")])
def test_d26_extends_exact_fit_rule_to_weak_matches(dist, ok, rule):
    album = _album([cand("r1", dist, ["tracks"]), cand("r2", dist + 0.5)], confirmed=0)
    got, why = review.d21_check(album, [])
    assert got is ok
    if ok:
        assert why.startswith(rule)


def test_auto_on_weak_uses_the_close_call_rules(conn):
    conn.execute("INSERT INTO matches (album_key, dirs, files, action, recommendation, distance, album_id, "
                 "candidates, matched_at) VALUES ('Weak/', '[]', '[]', 'review', 'medium', 0.15, 'rw', ?, 'now')",
                 (json.dumps([cand("rw", 0.15, ["tracks"]), cand("r2", 0.7)]),))
    conn.commit()
    out = review.auto_approve(conn, kind="weak", limit=10)
    assert [a["album_key"] for a in out["auto"]] == ["Weak/"] and out["auto"][0]["d21"].startswith("D26")


@pytest.mark.parametrize("local,key,cand_album,flag", [
    ("Ironman", "(1996) Ironman Instrumentals/", "Ironman", "instrumental"),
    ("Liquid Swords (instrumental)", "x/", "Liquid Swords (instrumental)", None),
    ("Figure 8", "x/", "Figure 8", None),
])
def test_version_mismatch(local, key, cand_album, flag):
    assert review.version_mismatch(local, key, cand_album) == flag
