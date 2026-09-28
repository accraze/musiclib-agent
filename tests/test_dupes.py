from musiclib import acoustid, db, dupes, verify


def add(conn, path, aid, *, sha=None, lossless=0, bitrate=320000, album=None, size=100):
    fp = f"fp-{path}"
    conn.execute(
        "INSERT INTO files (path, top_dir, ext, size, mtime, sha256, lossless, bitrate, mb_albumid, "
        "fingerprint, fp_duration, scanned_at) VALUES (?, '', 'x', ?, 0, ?, ?, ?, ?, ?, 100, 'now')",
        (path, size, sha or f"sha-{path}", lossless, bitrate, album, fp))
    conn.execute("INSERT INTO acoustid_lookups (fingerprint, fp_duration, status, acoustid_id, "
                 "recordings, looked_up_at) VALUES (?, 100, 'ok', ?, '[]', 'now')", (fp, aid))


def setup(tmp_path):
    conn = db.connect(tmp_path / "musiclib.db")
    acoustid.migrate(conn)
    return conn


def groups(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM dupe_groups ORDER BY tier, keeper")]


def run(conn):
    verify.run(conn)
    dupes.find(conn)
    return groups(conn)


def test_flac_copy_beats_mp3_copy(tmp_path):
    conn = setup(tmp_path)
    for i in range(5):
        add(conn, f"Album MP3/{i}.mp3", f"t{i}", album="R1")
        add(conn, f"Album FLAC/{i}.flac", f"t{i}", lossless=1, bitrate=900000, album="R1", size=300)
    [g] = run(conn)
    assert (g["tier"], g["action"], g["keeper"]) == (2, "auto", "Album FLAC/")
    assert g["reclaimable"] == 500


def test_partial_loser_goes_to_review(tmp_path):
    conn = setup(tmp_path)
    for i in range(10):
        add(conn, f"Full/{i}.flac", f"t{i}", lossless=1)
    for i in range(10):  # MP3 copy has one bonus track the FLAC copy lacks
        add(conn, f"Rip/{i}.mp3", f"t{i}" if i < 9 else "bonus")
    [g] = run(conn)
    assert (g["tier"], g["action"], g["keeper"]) == (2, "review", "Full/")


def test_different_releases_are_tier3(tmp_path):
    conn = setup(tmp_path)
    for i in range(10):
        add(conn, f"Standard/{i}.mp3", f"t{i}", album="R-std")
    for i in range(14):
        add(conn, f"Deluxe/{i}.mp3", f"t{i}", album="R-dlx")
    [g] = run(conn)
    assert (g["tier"], g["action"], g["keeper"]) == (3, "review", "Deluxe/")


def test_compilation_overlap_is_not_a_duplicate(tmp_path):
    conn = setup(tmp_path)
    for i in range(10):
        add(conn, f"Album/{i}.mp3", f"t{i}")
    for i in range(15):
        add(conn, f"Compilation/{i}.mp3", "t0" if i == 0 else f"c{i}")
    assert run(conn) == []


def test_identical_files_are_tier1(tmp_path):
    conn = setup(tmp_path)
    add(conn, "A/song.mp3", "x", sha="same")
    add(conn, "B/other.mp3", "y1")
    add(conn, "B/song copy.mp3", "x2", sha="same")
    add(conn, "C/z.mp3", "z")
    tiers = [(g["tier"], g["action"]) for g in run(conn)]
    assert (1, "auto") in tiers


def test_quality_beats_completeness(tmp_path):
    conn = setup(tmp_path)
    for i in range(10):  # complete but 128k
        add(conn, f"Low/{i}.mp3", f"t{i}", bitrate=128000)
    for i in range(9):   # missing one track but 320k
        add(conn, f"High/{i}.mp3", f"t{i}", bitrate=320000)
    [g] = run(conn)
    assert g["keeper"] == "High/" and g["action"] == "review"  # loser has a track keeper lacks


def test_identical_file_keeps_clean_name(tmp_path):
    conn = setup(tmp_path)
    add(conn, "A/05 - Song (2023_05_20 16_20_17 UTC).flac", "x", sha="same", lossless=1)
    add(conn, "A/05 - Song.flac", "x2", sha="same", lossless=1)
    add(conn, "A/06 - Other.flac", "y", lossless=1)
    [g] = run(conn)
    assert g["keeper"] == "A/05 - Song.flac"


def test_disc_folders_both_kept_over_worse_combined_copy(tmp_path):
    conn = setup(tmp_path)
    for i in range(18):
        add(conn, f"Box CD1/{i}.mp3", f"a{i}")
    for i in range(16):
        add(conn, f"Box CD2/{i}.mp3", f"b{i}")
    for i in range(34):
        add(conn, f"Box/{i}.mp3", f"a{i}" if i < 18 else f"b{i - 18}", bitrate=150000)
    [g] = run(conn)
    roles = {m["path"]: m["role"] for m in conn.execute("SELECT path, role FROM dupe_members")}
    assert roles == {"Box CD1/": "keep", "Box CD2/": "keep", "Box/": "drop"}
    assert g["action"] == "auto"


def test_not_imported_report_names_keeper_for_dropped_copies(tmp_path):
    from musiclib import report

    conn = setup(tmp_path)
    for i in range(3):
        add(conn, f"FLAC/{i}.flac", f"t{i}", lossless=1)
        add(conn, f"MP3/{i}.mp3", f"t{i}")
    add(conn, "loose.mp3", "solo")
    run(conn)
    summary, rows = report.not_imported(conn)
    assert summary["by_status"] == {"duplicate": 3, "unmatched": 4}
    dup = next(r for r in rows if r["path"] == "MP3/0.mp3")
    assert "kept FLAC/" in dup["detail"]


def _title(conn, path, title, duration=200):
    conn.execute("UPDATE files SET title = ?, duration = ? WHERE path = ?", (title, duration, path))


def test_second_copy_in_same_folder_is_dropped(tmp_path):
    conn = setup(tmp_path)
    for i in range(3):
        add(conn, f"Album/0{i} Song {i}.mp3", f"t{i}")
        _title(conn, f"Album/0{i} Song {i}.mp3", f"Song {i}")
    add(conn, "Album/01 Song 1 2.mp3", "t1")        # same audio, same title, copy name
    _title(conn, "Album/01 Song 1 2.mp3", "song 1")
    groups = run(conn)
    [g] = [g for g in groups if g["scope"] == "file"]
    assert (g["tier"], g["action"], g["keeper"]) == (2, "auto", "Album/01 Song 1.mp3")


def test_alternate_mix_sharing_an_acoustid_is_kept(tmp_path):
    conn = setup(tmp_path)
    add(conn, "Box/07 Dust.mp3", "dust")
    _title(conn, "Box/07 Dust.mp3", "Dust")
    add(conn, "Box/12 Dust (Alternate Mix).mp3", "dust")
    _title(conn, "Box/12 Dust (Alternate Mix).mp3", "Dust (Alternate Mix)")
    assert run(conn) == []
