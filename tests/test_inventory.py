import hashlib
import io
import os
import shutil
import subprocess

import pytest
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC

from musiclib import db, inventory, report
from musiclib.scan import scan_file

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")), reason="needs ffmpeg and fpcalc")

REC_ID = "d6118046-407d-4e06-a1ba-49c399a4c42f"


def _tone(path, seconds=3):
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                    "-y", str(path)], check=True)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "dump"
    (src / "Some Album (1999)").mkdir(parents=True)
    flac = src / "Some Album (1999)" / "01 - Track.flac"
    mp3 = src / "Some Album (1999)" / "01 - Track.mp3"
    _tone(flac)
    _tone(mp3)
    f = FLAC(flac)
    f.update({"artist": "Artist", "album": "Some Album", "title": "Track",
              "musicbrainz_trackid": REC_ID})
    f.save()
    t = EasyID3()
    t.update({"artist": "Artist", "title": "Track", "musicbrainz_trackid": REC_ID})
    t.save(mp3)
    shutil.copy(mp3, src / "loose copy.mp3")  # exact duplicate at the root
    (src / "Some Album (1999)" / "cover.jpg").write_bytes(b"\xff\xd8not really a jpeg")
    return src


def _snapshot(root):
    snap = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            st = os.stat(p)
            snap[p] = (st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
    return snap


def test_scan_reads_tags_and_stream_info(source):
    rec = scan_file(source / "Some Album (1999)" / "01 - Track.flac")
    assert rec.get("error") is None
    assert rec["codec"] == "flac" and rec["lossless"] == 1
    assert rec["artist"] == "Artist" and rec["album"] == "Some Album"
    assert rec["mb_trackid"] == REC_ID
    assert rec["fingerprint"] and len(rec["sha256"]) == 64

    rec = scan_file(source / "Some Album (1999)" / "01 - Track.mp3")
    assert rec["codec"] == "mp3" and rec["lossless"] == 0
    assert rec["mb_trackid"] == REC_ID


def test_inventory_leaves_source_untouched(source, tmp_path):
    before = _snapshot(source)
    conn = db.connect(tmp_path / "state" / "musiclib.db")
    result = inventory.run(conn, source, workers=2, progress=io.StringIO())
    assert _snapshot(source) == before
    assert result["audio_files"] == 3 and result["scanned"] == 3 and result["errors"] == 0
    assert result["other_files"] == 1


def test_inventory_is_incremental(source, tmp_path):
    conn = db.connect(tmp_path / "state" / "musiclib.db")
    inventory.run(conn, source, workers=2, progress=io.StringIO())
    again = inventory.run(conn, source, workers=2, progress=io.StringIO())
    assert again["scanned"] == 0


def test_report_finds_duplicates(source, tmp_path):
    conn = db.connect(tmp_path / "state" / "musiclib.db")
    inventory.run(conn, source, workers=2, progress=io.StringIO())
    summary = report.inventory_summary(conn)
    assert summary["files"] == 3
    assert summary["loose_root_files"] == 1
    assert summary["duplicates"]["tier1_identical_bytes"]["groups"] == 1
    assert summary["duplicates"]["tier2_preview_same_mb_recording_tag"]["files"] == 3
    assert summary["tag_coverage"]["mb_trackid"]["pct"] == 100.0
