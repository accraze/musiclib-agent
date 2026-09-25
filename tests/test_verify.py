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
        conn.execute("INSERT INTO acoustid_lookups VALUES (?, 100, 'ok', 0.95, 'a', ?, NULL, 'now')",
                     (f"fp{real}", recs((real, 0.95))))
    conn.commit()
    out = verify.run(conn)
    assert out["verdicts"]["mismatch"]["files"] == 3
    worst = out["suspect_folders"]["worst"][0]
    assert worst["folder"] == "Album/" and worst["repeated_tag_ids"] == 2
