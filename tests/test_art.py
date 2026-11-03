import io
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from test_audit import imported  # noqa: F401  (fixture)
from test_importer import env  # noqa: F401  (fixture)

pytest.importorskip("beets")
pytest.importorskip("PIL")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _png(side=400, color=(200, 30, 30)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (side, side), color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(side=400, color=(30, 30, 200)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (side, side), color).save(buf, "JPEG")
    return buf.getvalue()


def _embedded(path):
    import mediafile

    return [i.data for i in (mediafile.MediaFile(path).images or [])]


def _setup(imported):  # noqa: F811
    cfg, conn, importer, audit, lib = imported
    from musiclib import art

    art.migrate(conn)
    album = lib.albums()[0]
    folder = os.path.dirname(os.fsdecode(album.items()[0].path))
    for name in os.listdir(folder):  # an album without art: no cover file, no pointer to one
        if name.startswith("cover."):
            os.remove(os.path.join(folder, name))
    album.artpath = None
    album.store()
    return cfg, conn, audit, lib, art, album


def test_caa_art_is_written_embedded_and_logged(imported):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    cover = _png()
    asked = []
    get = lambda url: asked.append(url) or (cover if "/release/rel-1/front-1200" in url else None)  # noqa: E731
    out = art.scan(conn, lib, cfg.state_dir, get=get, progress=io.StringIO())
    assert out["exact"] == 1 and asked == ["https://coverartarchive.org/release/rel-1/front-1200"]

    plan = art.apply(conn, lib, cfg, progress=io.StringIO())
    assert plan["dry_run"] and plan["would_apply"][0]["source"] == "caa_release"
    assert not art.has_folder_art(lib.get_album(album.id))           # dry run: nothing written

    res = art.apply(conn, lib, cfg, apply=True, progress=io.StringIO())
    album = lib.get_album(album.id)
    path = os.fsdecode(album.artpath)
    assert res["applied"][0]["embedded_into"] == 3 and path.endswith("/cover.png")
    assert open(path, "rb").read() == cover
    for item in album.items():
        assert _embedded(os.fsdecode(item.path)) == [cover]
    rows = conn.execute("SELECT action, decided_by FROM audit_log WHERE action LIKE 'art_%'").fetchall()
    assert sorted(tuple(r) for r in rows) == [("art_cover", "auto")] + [("art_embed", "auto")] * 3
    # beets knows it wrote the files: the audit sees no outside change, and the album has art
    rep = audit.check(conn, lib, cfg.library_dir, cfg.source_dir)
    assert audit.summary(rep)["problems"]["modified_since_import"] == 0
    assert rep["no_art"]["count"] == 0
    assert art.plan(conn, lib) == [] and art.missing(lib) == []


def test_art_embedded_in_some_tracks_wins_and_fills_the_others(imported):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    import mediafile

    cover = _jpeg()
    first = os.fsdecode(album.items()[0].path)
    mf = mediafile.MediaFile(first)
    mf.images = [mediafile.Image(data=cover, type=mediafile.ImageType.front)]
    mf.save()
    asked = []
    art.scan(conn, lib, cfg.state_dir, get=lambda url: asked.append(url), progress=io.StringIO())
    assert asked == []                                                # no network needed
    res = art.apply(conn, lib, cfg, apply=True, progress=io.StringIO())
    assert res["applied"], res
    assert res["applied"][0]["source"] == "embedded" and res["applied"][0]["embedded_into"] == 2
    assert os.fsdecode(lib.get_album(album.id).artpath).endswith("/cover.jpg")


def test_search_candidates_wait_for_the_users_pick(imported, tmp_path):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    images = {"https://img/it1.jpg": _jpeg(), "https://img/dz1.jpg": _png(color=(0, 200, 0)),
              "https://img/tiny.jpg": _jpeg(side=100)}

    def get_json(url):
        if "itunes" in url:
            return {"results": [{"artworkUrl100": "https://img/it1.jpg", "artistName": "Real Band",
                                 "collectionName": "Real Album"},
                                {"artworkUrl100": "https://img/tiny.jpg", "collectionName": "Small"}]}
        return {"data": [{"cover_xl": "https://img/dz1.jpg", "title": "Real Album (Deluxe)",
                          "artist": {"name": "Real Band"}}]}

    out = art.scan(conn, lib, cfg.state_dir, search_too=True, get=images.get, get_json=get_json,
                   progress=io.StringIO())
    assert out["search"] == 1
    assert art.plan(conn, lib) == []                                  # search alone applies nothing
    [waiting] = art.pending_search(conn, lib)
    assert [(c["source"], c["title"]) for c in waiting["candidates"]] == \
        [("itunes", "Real Band - Real Album"), ("deezer", "Real Band - Real Album (Deluxe)")]  # tiny dropped

    g = art.write_gallery(conn, lib, tmp_path / "gallery.json")
    data = json.loads((tmp_path / "gallery.json").read_text())
    assert g["albums"] == 1 and data["albums"][0]["candidates"][0]["thumb"].startswith("data:image/jpeg;base64,")

    deezer = waiting["candidates"][1]["id"]
    with pytest.raises(SystemExit):
        art.decide(conn, album.id, 9999, "user")
    art.decide(conn, album.id, deezer, "user")
    [p] = art.plan(conn, lib)
    assert p["candidate"] == deezer and p["decided_by"] == "user"
    art.apply(conn, lib, cfg, apply=True, progress=io.StringIO())
    assert conn.execute("SELECT decided_by FROM audit_log WHERE action = 'art_cover'").fetchone()[0] == "user"
    assert art.pending_search(conn, lib) == []


def test_none_fits_is_remembered(imported):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    get_json = lambda url: {"results": [{"artworkUrl100": "https://img/x.jpg"}]} if "itunes" in url else {}  # noqa: E731
    art.scan(conn, lib, cfg.state_dir, search_too=True, get={"https://img/x.jpg": _jpeg()}.get,
             get_json=get_json, progress=io.StringIO())
    art.decide(conn, album.id, None, "user")
    assert art.pending_search(conn, lib) == [] and art.plan(conn, lib) == []
    out = art.scan(conn, lib, cfg.state_dir, search_too=True, get=lambda u: None, get_json=get_json,
                   progress=io.StringIO())
    assert out["chose_none"] == 1


def test_an_existing_cover_file_is_never_overwritten(imported):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    art.scan(conn, lib, cfg.state_dir, get=lambda u: _png() if "front-1200" in u else None, progress=io.StringIO())
    folder = os.path.dirname(os.fsdecode(album.items()[0].path))
    with open(os.path.join(folder, "cover.png"), "wb") as f:
        f.write(b"theirs")
    res = art.apply(conn, lib, cfg, apply=True, progress=io.StringIO())
    assert res["applied"] == [] and "already exists" in res["skipped"][0]["why"]
    assert open(os.path.join(folder, "cover.png"), "rb").read() == b"theirs"


def test_retry_checks_each_album_about_weekly_and_applies_exact_art(imported):  # noqa: F811
    cfg, conn, audit, lib, art, album = _setup(imported)
    t0 = datetime.now(timezone.utc)
    asked = []
    nothing = lambda url: asked.append(url)  # noqa: E731
    assert art.retry(conn, lib, cfg, get=nothing, clock=lambda: t0, progress=io.StringIO())["checked"] == 1
    n = len(asked)
    assert art.retry(conn, lib, cfg, get=nothing, clock=lambda: t0 + timedelta(days=3),
                     progress=io.StringIO())["checked"] == 0 and len(asked) == n
    found = art.retry(conn, lib, cfg, get=lambda u: _png() if "front-1200" in u else None,
                      clock=lambda: t0 + timedelta(days=8), progress=io.StringIO())
    assert found == {"checked": 1, "found": 1, "applied": 1, "skipped": 0}
    assert art.has_folder_art(lib.get_album(album.id))
