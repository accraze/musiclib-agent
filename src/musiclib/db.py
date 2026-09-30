"""State DB: inventory of every file in the source dump, plus runs and audit log."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,
    args        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT
);

-- One row per audio file. Paths are relative to source_dir for the dump, absolute for
-- files outside it (inbox batches, D29): `source / path` resolves both.
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    top_dir       TEXT NOT NULL,   -- first path component ('' for loose files at the root)
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    sha256        TEXT,
    codec         TEXT,
    lossless      INTEGER,
    bitrate       INTEGER,         -- bits per second
    bitrate_mode  TEXT,            -- CBR / VBR / ABR (mp3 only)
    sample_rate   INTEGER,
    bit_depth     INTEGER,
    channels      INTEGER,
    duration      REAL,
    has_art       INTEGER,
    artist        TEXT,
    albumartist   TEXT,
    album         TEXT,
    title         TEXT,
    track         TEXT,
    disc          TEXT,
    date          TEXT,
    mb_trackid        TEXT,        -- MusicBrainz recording ID
    mb_releasetrackid TEXT,
    mb_albumid        TEXT,        -- MusicBrainz release ID
    mb_artistid       TEXT,
    mb_releasegroupid TEXT,
    acoustid_id       TEXT,
    tags_json     TEXT,            -- all text tags, normalized keys
    fp_duration   INTEGER,
    fingerprint   TEXT,            -- Chromaprint (fpcalc, compressed)
    error         TEXT,            -- scan failed; retried on the next run
    warnings      TEXT,            -- scan succeeded with problems (e.g. corrupt frames)
    scanned_at    TEXT NOT NULL,
    run_id        INTEGER REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS files_mb_trackid ON files(mb_trackid);
CREATE INDEX IF NOT EXISTS files_mb_albumid ON files(mb_albumid);
CREATE INDEX IF NOT EXISTS files_top_dir ON files(top_dir);

-- Non-audio files (art, cue, logs, playlists, junk), kept for the report and for later cleanup.
CREATE TABLE IF NOT EXISTS other_files (
    id      INTEGER PRIMARY KEY,
    path    TEXT NOT NULL UNIQUE,
    top_dir TEXT NOT NULL,
    ext     TEXT NOT NULL,
    size    INTEGER NOT NULL,
    run_id  INTEGER REFERENCES runs(id)
);

-- SPEC Safety rule #5: every change made by any command gets a row here.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(id),
    action      TEXT NOT NULL,
    source_path TEXT,
    dest_path   TEXT,
    reason      TEXT,
    decided_by  TEXT NOT NULL CHECK (decided_by IN ('auto', 'agent', 'user'))
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA + TEMP_VIEWS)
    return conn


# Per-connection views (temp schema: nothing is written to the DB file).
# dump_files: the source dump only, i.e. relative paths (D29).
TEMP_VIEWS = """
CREATE TEMP VIEW IF NOT EXISTS dump_files AS SELECT * FROM files WHERE substr(path, 1, 1) != '/';
"""


def is_dump_path(path: str) -> bool:
    return not path.startswith("/")
