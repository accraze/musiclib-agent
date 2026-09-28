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
