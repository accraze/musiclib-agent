import json
import os
import shutil

import pytest

from test_audit import imported  # noqa: F401  (fixture)
from test_importer import env  # noqa: F401  (fixture)

pytest.importorskip("beets")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_stats_count_the_library_and_page_carries_no_paths(imported, tmp_path):  # noqa: F811
    cfg, conn, _, _, lib = imported
    from musiclib import stats

    album = lib.albums()[0]
    items = list(album.items())
    for i, item in enumerate(items):
        item.format, item.bitrate = ("FLAC", 900000) if i == 0 else ("MP3", 128000)
        item.store()
    album.year, album.label = 1977, "Island"
    album.store()

    data = stats.collect(conn, lib, cfg.library_dir)
    L = data["library"]
    assert L["totals"]["albums"] == 1 and L["totals"]["tracks"] == len(items)
    assert L["totals"]["bytes"] == sum(os.path.getsize(os.fsdecode(i.path)) for i in items)
    assert {q["tier"]: q["tracks"] for q in L["quality"]} == {
        "lossless": 1, "256k+": 0, "192k": 0, "128k": len(items) - 1, "under 128k": 0}
    assert L["years"] == [[1977, 1]] and L["labels"] == [["Island", 1]]
    assert L["records"]["oldest"]["year"] == 1977
    assert data["health"]["damaged"] == 0 and data["health"]["mbsync"]["checked"] == 0

    out = tmp_path / "almanac.html"
    stats.write_page(data, out)
    html = out.read_text()
    assert "__DATA__" not in html and str(cfg.library_dir) not in html
    blob = html.split('id="data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)["library"]["totals"]["albums"] == 1
