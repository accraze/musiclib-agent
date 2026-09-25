import io
import json

import pytest

from musiclib import acoustid, config, db

REC_A, REC_B = "rec-a", "rec-b"


def _conn(tmp_path, fps):
    conn = db.connect(tmp_path / "musiclib.db")
    for i, fp in enumerate(fps):
        conn.execute(
            "INSERT INTO files (path, top_dir, ext, size, mtime, fingerprint, fp_duration, scanned_at) "
            "VALUES (?, '', 'mp3', 1, 0, ?, 200, 'now')", (f"f{i}.mp3", fp))
    conn.commit()
    return conn


def fake_post(key, batch):
    out = []
    for fp, _ in batch:
        if fp == "unknown":
            out.append([])
        else:
            out.append([{"id": "aid-1", "score": 0.97, "recordings": [{"id": REC_A}]},
                        {"id": "aid-2", "score": 0.5, "recordings": [{"id": REC_B}, {"id": REC_A}]}])
    return out


def test_parse_results_orders_and_dedupes():
    r = acoustid.parse_results(fake_post("k", [("x", 1)])[0][::-1])
    assert r["status"] == "ok" and r["best_score"] == 0.97 and r["acoustid_id"] == "aid-1"
    assert [x["id"] for x in json.loads(r["recordings"])] == [REC_A, REC_B]


def test_run_dedupes_fingerprints_and_resumes(tmp_path):
    conn = _conn(tmp_path, ["fpX", "fpX", "unknown"] + [f"fp{i}" for i in range(12)])
    calls = []

    def post(key, batch):
        calls.append(len(batch))
        return fake_post(key, batch)

    res = acoustid.run(conn, "k", progress=io.StringIO(), post=post, sleep=lambda s: None)
    assert res["looked_up"] == 14 and res["no_match"] == 1 and res["ok"] == 13
    assert calls == [10, 4]
    again = acoustid.run(conn, "k", progress=io.StringIO(), post=post, sleep=lambda s: None)
    assert again["looked_up"] == 0


def test_run_requires_key(tmp_path):
    with pytest.raises(SystemExit):
        acoustid.run(_conn(tmp_path, []), None, progress=io.StringIO())


def test_config_rejects_library_inside_source(tmp_path):
    cfg = tmp_path / "musiclib.toml"
    cfg.write_text(f'source_dir = "{tmp_path}/dump"\nlibrary_dir = "{tmp_path}/dump/clean"\n')
    with pytest.raises(SystemExit):
        config.load(cfg)


def test_config_merges_local_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("ACOUSTID_KEY", raising=False)
    (tmp_path / "musiclib.toml").write_text(f'source_dir = "{tmp_path}/dump"\n')
    (tmp_path / "musiclib.local.toml").write_text('acoustid_key = "secret"\n')
    assert config.load(tmp_path / "musiclib.toml").acoustid_key == "secret"


def test_run_retries_transient_network_errors(tmp_path):
    import ssl

    conn = _conn(tmp_path, ["fp1", "fp2"])
    failures = [ssl.SSLError("bad record mac"), ConnectionResetError()]

    def flaky(key, batch):
        if failures:
            raise failures.pop(0)
        return fake_post(key, batch)

    res = acoustid.run(conn, "k", progress=io.StringIO(), post=flaky, sleep=lambda s: None)
    assert res["ok"] == 2 and res["error"] == 0
