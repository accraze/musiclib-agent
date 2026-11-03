import os
import shutil

import pytest

from test_audit import imported  # noqa: F401  (fixture)
from test_importer import env  # noqa: F401  (fixture)

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _setup(imported):  # noqa: F811
    cfg, conn, _, _, lib = imported
    from musiclib import wanted

    wanted.migrate(conn)
    album = lib.albums()[0]
    for item in album.items():  # the fixture's tiny files would count as low bitrate
        item.bitrate = 320000
        item.store()
    paths = sorted(os.fsdecode(i.path) for i in album.items())
    return cfg, conn, lib, wanted, album, paths


def _decoded(conn, path, decoded, expected=100.0):
    conn.execute("INSERT OR REPLACE INTO audit_decode (path, size, mtime, expected, decoded, errors, checked_at) "
                 "VALUES (?, 1, 0, ?, ?, 0, 'now')", (path, expected, decoded))
    conn.commit()


def test_damaged_album_is_wanted_until_fixed(imported):  # noqa: F811
    cfg, conn, lib, wanted, album, paths = _setup(imported)
    _decoded(conn, paths[0], 40.0)
    _decoded(conn, paths[1], 99.0)
    out = wanted.refresh(conn, lib, cfg.library_dir)
    assert out["open"] == {"damaged": 1} and out["added"] == 1
    [e] = wanted.entries(conn)
    assert e["files"] == [paths[0]] and e["detail"] == "1 of 3 tracks damaged" and e["artist"] == album.albumartist

    assert wanted.refresh(conn, lib, cfg.library_dir)["added"] == 0  # unchanged: not added twice
    _decoded(conn, paths[0], 100.0)  # a good copy replaced it
    out = wanted.refresh(conn, lib, cfg.library_dir)
    assert out["fulfilled"][0]["why"] == "no damaged files left there" and out["open"] == {}
    _decoded(conn, paths[0], 10.0)   # broken again: a new entry
    assert wanted.refresh(conn, lib, cfg.library_dir)["open"] == {"damaged": 1}


def test_low_bitrate_and_dismiss(imported):  # noqa: F811
    cfg, conn, lib, wanted, album, _ = _setup(imported)
    for item in album.items():
        item.bitrate = 64000
        item.format = "MP3"
        item.store()
    out = wanted.refresh(conn, lib, cfg.library_dir)
    assert out["open"] == {"low_bitrate": 1}
    [e] = wanted.entries(conn)
    assert e["detail"] == "64 kbps on average"
    wanted.dismiss(conn, [e["id"]], reason="fine as it is")
    assert wanted.refresh(conn, lib, cfg.library_dir)["open"] == {}   # dismissed stays dismissed
    assert wanted.entries(conn, "dismissed")[0]["closed_reason"] == "fine as it is"


def test_manual_entry_closes_on_a_later_import(imported):  # noqa: F811
    cfg, conn, lib, wanted, album, _ = _setup(imported)
    res = wanted.add(conn, lib, "the  REAL band", "Real Album!", note="the remaster")
    assert res["in_library_now"]            # said, but still wanted: a later import closes it
    assert wanted.add(conn, lib, "Real Band", "Real Album")["already_wanted"]
    assert wanted.refresh(conn, lib, cfg.library_dir)["open"] == {"manual": 1}
    conn.execute("UPDATE wanted SET had_albums = '[]'")  # as if this album came in after the add
    conn.commit()
    out = wanted.refresh(conn, lib, cfg.library_dir)
    assert out["open"] == {} and out["fulfilled"][0]["why"].startswith("imported: ")

    from musiclib import notify
    [(title, body, _)] = notify.wanted_messages(out["fulfilled"])
    assert title == "1 off the wanted list" and "Real Album" in body


def test_inbox_copy_of_a_damaged_album_is_asked_not_skipped():
    from musiclib.dupes import Folder
    from musiclib.ingest import _classify

    lib = Folder("/lib/Band/2000 - Album/", files=3, bitrate_sum=3 * 320000, keys={"aid:1", "aid:2", "aid:3"})
    mine = Folder("/inbox/x/", files=3, bitrate_sum=3 * 320000, keys={"aid:1", "aid:2", "aid:3"})
    same = {"aid:1", "aid:2", "aid:3"}
    assert _classify(mine, lib, same)[1] == "duplicate"
    out = _classify(mine, lib, same, frozenset({"aid:2"}))
    assert out[:2] == ("review", "upgrade") and "D47" in out[2]
    worse = Folder("/inbox/y/", files=3, bitrate_sum=3 * 128000, keys=same)
    assert _classify(worse, lib, same, frozenset({"aid:2"}))[1] == "wanted"
    single = Folder("/lib/Unsorted/12s/", files=9, bitrate_sum=9 * 320000, keys={"aid:2", "aid:7"})
    assert _classify(mine, single, {"aid:2"}, frozenset({"aid:2"}))[1] == "wanted"  # never replace a whole Unsorted folder


def test_page_has_no_library_paths(imported, tmp_path):  # noqa: F811
    cfg, conn, lib, wanted, album, paths = _setup(imported)
    _decoded(conn, paths[0], 40.0)
    wanted.refresh(conn, lib, cfg.library_dir)
    wanted.add(conn, lib, "Bill Evans", "Portrait in Jazz", note="</script>")
    res = wanted.write_page(conn, tmp_path / "w.html")
    html = (tmp_path / "w.html").read_text()
    assert res["entries"] == 2 and "Portrait in Jazz" in html
    assert str(cfg.library_dir) not in html and "<\\/script>" in html
