import json

import pytest

from musiclib import acoustid, db, verify


def recs(*pairs):
    return json.dumps([{"id": i, "score": s} for i, s in pairs])


@pytest.mark.parametrize("tag,status,recordings,expected", [
    ("A", "ok", recs(("A", 0.95)), "confirmed"),
    ("A", "ok", recs(("B", 0.95), ("A", 0.95)), "confirmed"),
    ("A", "ok", recs(("B", 0.95)), "mismatch"),
    ("A", "ok", recs(("B", 0.5)), "unverifiable"),   # below MIN_SCORE
    ("A", "no_match", "[]", "unverifiable"),
    (None, "ok", recs(("B", 0.9)), "suggest"),
    (None, "no_match", "[]", "unknown"),
    ("A", None, None, "no_lookup"),
])
def test_classify(tag, status, recordings, expected):
    assert verify.classify(tag, status, recordings)[0] == expected


def test_run_flags_repeated_ids_in_a_folder(tmp_path):
    conn = db.connect(tmp_path / "musiclib.db")
    conn.executescript(acoustid.SCHEMA)
    # Three tracks of one album all tagged with recording X; audio is really A, B, C.
    for i, real in enumerate("ABC"):
        conn.execute("INSERT INTO files (path, top_dir, ext, size, mtime, mb_trackid, fingerprint, "
                     "fp_duration, scanned_at) VALUES (?, 'Album', 'mp3', 1, 0, 'X', ?, 100, 'now')",
                     (f"Album/{i}.mp3", f"fp{real}"))
        conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, best_score, "
                     "acoustid_id, recordings, looked_up_at) VALUES (?, 100, 'ok', 0.95, 'a', ?, 'now')",
                     (f"fp{real}", recs((real, 0.95))))
    conn.commit()
    out = verify.run(conn)
    assert out["verdicts"]["mismatch"]["files"] == 3
    worst = out["suspect_folders"]["worst"][0]
    assert worst["folder"] == "Album/" and worst["repeated_tag_ids"] == 2


def test_alt_recording_when_titles_agree():
    titles = json.dumps({"B": {"title": "Help Us", "artist": "Burning Spear"}})
    assert verify.classify("A", "ok", recs(("B", 0.95)), "Help Us (Dub)", titles)[0] == "alt_recording"
    assert verify.classify("A", "ok", recs(("B", 0.95)), "Clint Eastwood", titles)[0] == "mismatch"


@pytest.mark.parametrize("a,b,agree", [
    ("Coming At La Mer To A World Left Beh", "Coming At La Mer To A World Left Behind", True),
    ("Café del Mar", "Cafe Del Mar [Remastered]", True),
    ("Untitled 2", "[untitled]", False),
    ("Bonnie", "Find Me a Moment", False),
])
def test_titles_agree(a, b, agree):
    assert verify.titles_agree(a, b) is agree
