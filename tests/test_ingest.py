"""M5 ingest: claim (D28) and scan. Inbox files are read-only after the claim rename (D27)."""

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from musiclib import config, db, ingest

from test_importer import _snapshot

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")), reason="needs ffmpeg and fpcalc")


def _tone(path, seconds=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                    "-y", str(path)], check=True)


@pytest.fixture
def env(tmp_path):
    inbox = tmp_path / "inbox"
    _tone(inbox / "New Album" / "01 One.mp3")
    _tone(inbox / "New Album" / "02 Two.mp3", seconds=4)
    (inbox / "New Album" / "cover.jpg").write_bytes(b"x")
    (inbox / "loose.mp3").write_bytes(b"x")
    (inbox / "Empty").mkdir()
    (tmp_path / "dump").mkdir()
    (tmp_path / "musiclib.toml").write_text(
        f'source_dir = "{tmp_path}/dump"\nstate_dir = "{tmp_path}/state"\n'
        f'library_dir = "{tmp_path}/lib"\ninbox_dir = "{inbox}"\n')
    cfg = config.load(tmp_path / "musiclib.toml")
    return cfg, db.connect(cfg.db_path)


def _no_lookup(conn, key, progress=None):
    return {"looked_up": 0, "elapsed_s": 0}


def test_listing(env):
    cfg, conn = env
    out = ingest.listing(conn, cfg)
    assert [w["folder"] for w in out["waiting"]] == ["Empty", "New Album"]
    assert out["waiting"][1]["audio_files"] == 2 and out["waiting"][1]["other_files"] == 1
    assert [i["name"] for i in out["ignored"]] == ["loose.mp3"]


def test_claim_dry_run_moves_nothing(env):
    cfg, conn = env
    plan = ingest.claim(conn, cfg, "New Album", day="2026-09-30")
    assert plan["dry_run"] and plan["to"] == str(cfg.inbox_dir / ".processed" / "2026-09-30" / "New Album")
    assert (cfg.inbox_dir / "New Album").is_dir()
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def _tree(root):
    """{path below root: (size, mtime, sha256)}"""
    return {Path(k).relative_to(root): v for k, v in _snapshot(root).items()}


def test_claim_moves_and_logs(env):
    cfg, conn = env
    before = _tree(cfg.inbox_dir / "New Album")
    out = ingest.claim(conn, cfg, "New Album", apply=True, day="2026-09-30")
    dest = cfg.inbox_dir / ".processed" / "2026-09-30" / "New Album"
    assert out["batch"] == 1 and not (cfg.inbox_dir / "New Album").exists()
    assert _tree(dest) == before  # a rename: same names, bytes and mtimes
    [a] = conn.execute("SELECT action, source_path, dest_path, decided_by FROM audit_log").fetchall()
    assert tuple(a) == ("ingest_claim", str(cfg.inbox_dir / "New Album"), str(dest), "user")
    assert ingest.batch(conn, "New Album")["status"] == "claimed"


def test_claim_same_name_twice_gets_a_suffix(env):
    cfg, conn = env
    ingest.claim(conn, cfg, "New Album", apply=True, day="2026-09-30")
    _tone(cfg.inbox_dir / "New Album" / "01 Again.mp3")
    out = ingest.claim(conn, cfg, "New Album", apply=True, day="2026-09-30")
    assert out["to"].endswith("/2026-09-30/New Album (2)")
    with pytest.raises(SystemExit, match="several batches"):
        ingest.batch(conn, "New Album")


@pytest.mark.parametrize("name, err", [
    ("loose.mp3", "no such folder"), ("Empty", "no audio"), (".processed", "not an inbox folder"),
    ("../dump", "not an inbox folder"), ("Missing", "no such folder")])
def test_claim_refuses(env, name, err):
    cfg, conn = env
    with pytest.raises(SystemExit, match=err):
        ingest.claim(conn, cfg, name, apply=True)


def test_scan_records_the_batch_read_only(env):
    cfg, conn = env
    ingest.claim(conn, cfg, "New Album", apply=True, day="2026-09-30")
    root = cfg.inbox_dir / ".processed" / "2026-09-30" / "New Album"
    before = _snapshot(root)
    out = ingest.scan(conn, cfg, 1, workers=2, lookup=_no_lookup, progress=io.StringIO())
    assert _snapshot(root) == before
    assert out["audio_files"] == 2 and out["scan_errors"] == [] and out["verify"] == {"no_lookup": 2}
    paths = [r[0] for r in conn.execute("SELECT path FROM files ORDER BY path")]
    assert paths == [str(root / "01 One.mp3"), str(root / "02 Two.mp3")]
    assert ingest.batch(conn, 1)["status"] == "scanned"
