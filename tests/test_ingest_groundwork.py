"""M5 groundwork: inbox files (absolute paths, D29) live beside dump files without
changing anything the dump pipeline does."""

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from musiclib import config, db, dupes, importer, inventory, report, verify

from test_dupes import add, setup

needs_tools = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")), reason="needs ffmpeg and fpcalc")


def _tone(path, seconds=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                    "-y", str(path)], check=True)


@pytest.fixture
def roots(tmp_path):
    dump = tmp_path / "dump"
    _tone(dump / "Old Album" / "01 Track.mp3")
    batch = tmp_path / "inbox" / ".processed" / "2026-09-30" / "New Album"
    _tone(batch / "01 New.mp3")
    _tone(batch / "CD2" / "01 Other.mp3", seconds=4)
    (batch / "cover.jpg").write_bytes(b"x")
    return dump, batch


@needs_tools
def test_scan_root_stores_absolute_paths(roots, tmp_path):
    dump, batch = roots
    conn = db.connect(tmp_path / "state.db")
    before = {p: p.stat().st_mtime_ns for p in batch.rglob("*")}
    res = inventory.scan_root(conn, batch, workers=2, progress=io.StringIO())
    assert res["scanned"] == 2 and res["other_files"] == 1
    assert {p: p.stat().st_mtime_ns for p in batch.rglob("*")} == before  # read-only
    paths = {r[0]: r[1] for r in conn.execute("SELECT path, top_dir FROM files")}
    assert paths == {str(batch / "01 New.mp3"): str(batch), str(batch / "CD2" / "01 Other.mp3"): str(batch)}


@needs_tools
def test_full_dump_inventory_keeps_inbox_rows(roots, tmp_path):
    dump, batch = roots
    conn = db.connect(tmp_path / "state.db")
    inventory.scan_root(conn, batch, workers=2, progress=io.StringIO())
    res = inventory.run(conn, dump, workers=2, progress=io.StringIO())
    assert res["removed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3
    # ...and the dump-only views and reports don't see them.
    assert [r[0] for r in conn.execute("SELECT path FROM dump_files")] == ["Old Album/01 Track.mp3"]
    assert report.inventory_summary(conn)["files"] == 1
    summary, rows = report.not_imported(conn)
    assert [r["path"] for r in rows] == ["Old Album/01 Track.mp3"]


@needs_tools
def test_rescanning_a_batch_prunes_only_that_batch(roots, tmp_path):
    dump, batch = roots
    conn = db.connect(tmp_path / "state.db")
    inventory.run(conn, dump, workers=2, progress=io.StringIO())
    inventory.scan_root(conn, batch, workers=2, progress=io.StringIO())
    (batch / "CD2" / "01 Other.mp3").unlink()
    res = inventory.scan_root(conn, batch, workers=2, progress=io.StringIO())
    assert res["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_dump_dupes_ignore_inbox_files(tmp_path):
    conn = setup(tmp_path)
    for i in range(5):
        add(conn, f"Album/{i}.mp3", f"t{i}")
        add(conn, f"/inbox/.processed/d/Album/{i}.flac", f"t{i}", lossless=1)
    verify.run(conn)
    dupes.find(conn)
    assert conn.execute("SELECT COUNT(*) FROM dupe_groups").fetchone()[0] == 0


def test_verify_run_under_touches_only_the_batch(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Album/1.mp3", "t1")
    add(conn, "/in/b/1.mp3", "t1")
    add(conn, "/in/bb/1.mp3", "t1")  # shares the prefix "/in/b" but isn't under it
    counts = verify.run_under(conn, "/in/b")
    assert counts == {"unknown": 1}
    assert conn.execute("SELECT COUNT(*) FROM verify").fetchone()[0] == 1


@pytest.mark.parametrize("rel, name", [
    ("Artist - Album/01 x.mp3", "Artist - Album/01 x.mp3"),
    ("/srv/inbox/.processed/2026-09-30/New Album/01 x.mp3", "New Album/01 x.mp3"),
    ("/srv/inbox/.processed/2026-09-30/New Album/CD2/01 x.mp3", "New Album/CD2/01 x.mp3"),
    ("/elsewhere/Folder/01 x.mp3", "Folder/01 x.mp3"),
])
def test_unsorted_name(rel, name):
    assert importer.unsorted_name(rel) == Path(name)


def test_import_asis_places_inbox_files_under_unsorted(tmp_path):
    lib, staging = tmp_path / "lib", tmp_path / "staging"
    staging.mkdir()
    staged = staging / "000 01 x.mp3"
    staged.write_bytes(b"audio")
    rel = str(tmp_path / "inbox" / ".processed" / "2026-09-30" / "New Album" / "01 x.mp3")
    [(_, dest)] = importer.import_asis(lib, {str(staged): rel})
    assert Path(dest) == lib / "Unsorted" / "New Album" / "01 x.mp3"
    assert not Path(rel).exists()


def test_library_lock_is_exclusive(tmp_path):
    with importer.library_lock(tmp_path):
        with pytest.raises(SystemExit, match="changing the library"):
            with importer.library_lock(tmp_path):
                pass
    with importer.library_lock(tmp_path):  # released
        pass


@pytest.mark.parametrize("inbox, ok", [
    ("inbox", True), ("dump/inbox", False), ("lib/inbox", False), (".", False)])
def test_inbox_dir_placement(tmp_path, inbox, ok):
    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{tmp_path}/dump"\nstate_dir = "{tmp_path}/state"\n'
        f'library_dir = "{tmp_path}/lib"\ninbox_dir = "{tmp_path}/{inbox}"\n')
    if ok:
        cfg = config.load(tmp_path / "musiclib.toml")
        assert cfg.processed_dir == tmp_path / "inbox" / ".processed"
    else:
        with pytest.raises(SystemExit):
            config.load(tmp_path / "musiclib.toml")
