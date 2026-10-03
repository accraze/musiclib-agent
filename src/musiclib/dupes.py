"""M2: find duplicates and propose keepers. Read-only on files; writes proposals to the state DB.

Track identity: AcoustID id (same audio across encodes), else SHA-256.
Folders are compared by the identities they contain:

  tier 1  byte-identical files (any folders)                         -> auto
  tier 2  folders sharing >= 90% of the smaller folder's tracks,
          same release or no release tags                            -> auto if the loser is
                                                                        fully covered, else review
  tier 3  same overlap, but tagged as different releases (editions)  -> review
  tier 2  (file scope) two copies inside one folder: same AcoustID, same full title
          (case-insensitive, parentheticals included), lengths within 2 s  -> auto, unless
          the filenames carry different track numbers (a release may repeat a track on
          purpose, or a bonus version may share tags)                    -> review

Folders that share only a few tracks (album vs. compilation) are not duplicates.
Keepers follow SPEC "Keeper ranking" (D6/D10).
"""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

CONTAINMENT = 0.9

SCHEMA = """
DROP TABLE IF EXISTS dupe_members;
DROP TABLE IF EXISTS dupe_groups;
CREATE TABLE dupe_groups (
    id              INTEGER PRIMARY KEY,
    tier            INTEGER NOT NULL,
    scope           TEXT NOT NULL,          -- file | folder
    action          TEXT NOT NULL,          -- auto | review
    reason          TEXT NOT NULL,
    keeper          TEXT NOT NULL,          -- path (file or folder)
    carry_tags_from TEXT,                   -- D10: loser whose verified tags beat the keeper's
    reclaimable     INTEGER NOT NULL
);
CREATE TABLE dupe_members (
    group_id  INTEGER NOT NULL REFERENCES dupe_groups(id) ON DELETE CASCADE,
    path      TEXT NOT NULL,
    role      TEXT NOT NULL,                -- keep | drop
    coverage  REAL,                         -- drop: share covered by kept copies; keep: share overlapping earlier keepers
    stats     TEXT                          -- JSON used for ranking
);
"""


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] + "/" if "/" in path else ""


def quality_tier(lossless: bool, bitrate: float) -> int:
    """Coarse audio quality: lossless > ~320k > V0/256k > 192k > 128k > worse. Bitrate is
    ignored among lossless files, and small differences (245k VBR vs 256k) don't count."""
    if lossless:
        return 6
    kbps = (bitrate or 0) / 1000
    return 5 if kbps >= 300 else 4 if kbps >= 230 else 3 if kbps >= 180 else 2 if kbps >= 120 else 1


def _tiebreak(path: str) -> tuple:
    """Prefer shorter, then alphabetically first paths: 'x.flac' over 'x (2023_05_20 ...).flac'."""
    return (-len(path), [-ord(c) for c in path])


def _filename_number(path: str) -> int | None:
    """First number in the file name: '09 Transposition.mp3' -> 9."""
    m = re.search(r"\d+", path.rsplit("/", 1)[-1])
    return int(m.group()) if m else None


def _majority(values) -> str | None:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None


@dataclass
class Folder:
    path: str
    keys: set = field(default_factory=set)
    files: int = 0
    size: int = 0
    lossless: int = 0
    bitrate_sum: int = 0
    verified: int = 0
    art: int = 0
    albums: list = field(default_factory=list)

    @property
    def release(self) -> str | None:
        return _majority(self.albums)

    def rank(self) -> tuple:
        """Higher is better. SPEC keeper ranking: quality, completeness, verified tags, art."""
        n = self.files or 1
        quality = quality_tier(self.lossless / n > 0.5, self.bitrate_sum / n)
        return (quality, len(self.keys), round(self.verified / n, 1), round(self.art / n, 1),
                self.size, *_tiebreak(self.path))

    def stats(self) -> dict:
        n = self.files or 1
        return {"files": self.files, "tracks": len(self.keys), "size": self.size,
                "lossless": round(self.lossless / n, 2), "kbps": round(self.bitrate_sum / n / 1000),
                "verified": round(self.verified / n, 2), "art": round(self.art / n, 2),
                "release": self.release}


# The per-file columns duplicate grouping needs; {where} narrows the files.
FILE_ROWS = """
    SELECT f.id, f.path, f.size, f.sha256, f.lossless, COALESCE(f.bitrate, 0) AS bitrate,
           f.has_art, NULLIF(f.mb_albumid, '') AS album, a.acoustid_id, v.verdict,
           lower(trim(f.title)) AS title, f.duration, f.fingerprint,
           COALESCE(NULLIF(f.albumartist, ''), f.artist) AS artist_name, f.album AS album_name
    FROM files f
    LEFT JOIN acoustid_lookups a ON a.fingerprint = f.fingerprint AND a.fp_duration = f.fp_duration
    LEFT JOIN verify v ON v.file_id = f.id
    WHERE {where}
"""


def _load(conn: sqlite3.Connection):
    return folders_of(conn.execute(FILE_ROWS.format(where="substr(f.path, 1, 1) != '/'")).fetchall())


def track_key(r) -> str:
    """A file's track identity: its AcoustID, else its bytes."""
    return f"aid:{r['acoustid_id']}" if r["acoustid_id"] else f"sha:{r['sha256']}"


def folders_of(rows) -> tuple[list, dict[str, Folder]]:
    """Group file rows (columns as in FILE_ROWS) into Folders."""
    folders: dict[str, Folder] = {}
    for r in rows:
        d = folders.setdefault(_folder(r["path"]), Folder(_folder(r["path"])))
        d.keys.add(track_key(r))
        d.files += 1
        d.size += r["size"]
        d.lossless += bool(r["lossless"])
        d.bitrate_sum += r["bitrate"] or 0
        d.verified += r["verdict"] == "confirmed"
        d.art += bool(r["has_art"])
        d.albums.append(r["album"])
    return rows, folders


def _file_rank(r) -> tuple:
    return (quality_tier(r["lossless"], r["bitrate"]), r["verdict"] == "confirmed",
            bool(r["has_art"]), r["size"], *_tiebreak(r["path"]))


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def find(conn: sqlite3.Connection) -> dict:
    """Rebuild the dump's duplicate groups (dupe_groups/dupe_members) from scratch."""
    rows, folders = _load(conn)
    groups = group(rows, folders)
    conn.executescript(SCHEMA)
    for tier, scope, action, reason, keeper, carry, reclaim, mem in groups:
        gid = conn.execute(
            "INSERT INTO dupe_groups (tier, scope, action, reason, keeper, carry_tags_from, reclaimable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (tier, scope, action, reason, keeper, carry, reclaim)).lastrowid
        conn.executemany("INSERT INTO dupe_members VALUES (?, ?, ?, ?, ?)",
                         [(gid, p, role, cov, json.dumps(st) if st else None) for p, role, cov, st in mem])
    conn.commit()
    return summary(conn)


def group(rows, folders: dict[str, Folder]) -> list[tuple]:
    """Duplicate groups over the given files; no DB access.
    Each: (tier, scope, action, reason, keeper, carry_tags_from, reclaimable, members),
    members = [(path, 'keep' | 'drop', coverage, stats or None)]."""
    groups = []  # (tier, scope, action, reason, keeper, carry, reclaimable, members)

    # Tier 2/3: folder pairs with high overlap, via an inverted index on track keys.
    index = defaultdict(set)
    for f in folders.values():
        for k in f.keys:
            index[k].add(f.path)
    shared = Counter()
    for paths in index.values():
        if 1 < len(paths) <= 50:  # a key in > 50 folders is noise (silence, intro tracks)
            ps = sorted(paths)
            for i, a in enumerate(ps):
                for b in ps[i + 1:]:
                    shared[(a, b)] += 1

    uf, editions = _UnionFind(), []
    for (a, b), n in shared.items():
        fa, fb = folders[a], folders[b]
        if n / min(len(fa.keys), len(fb.keys)) < CONTAINMENT:
            continue
        ra, rb = fa.release, fb.release
        if ra and rb and ra != rb:
            editions.append((a, b))
        else:
            uf.union(a, b)

    clusters = defaultdict(list)
    for p in list(uf.parent):
        clusters[uf.find(p)].append(p)
    in_tier2 = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        in_tier2.update(members)
        # Greedy cover, best first: keep a folder only if it adds tracks not already kept.
        # Disc folders (CD1, CD2) both stay; a worse copy of both is dropped.
        ranked = sorted((folders[m] for m in members), key=Folder.rank, reverse=True)
        keeper = ranked[0]
        covered, mem, partial, reclaim, carry = set(), [], False, 0, None
        for f in ranked:
            cov = len(f.keys & covered) / len(f.keys)
            if f is keeper or cov < 1.0:
                partial |= cov > 0  # a kept folder that overlaps: someone should look
                covered |= f.keys
                mem.append((f.path, "keep", round(cov, 3), f.stats()))
            else:
                reclaim += f.size
                if carry is None and f.verified / f.files > keeper.verified / keeper.files + 0.2:
                    carry = f.path
                mem.append((f.path, "drop", 1.0, f.stats()))
        reason = ("kept copies overlap; lower-quality copy has tracks the best copy lacks"
                  if partial else "same tracks")
        groups.append((2, "folder", "review" if partial else "auto", reason, keeper.path, carry,
                       reclaim, mem))

    for a, b in editions:
        fa, fb = folders[a], folders[b]
        keeper, other = sorted((fa, fb), key=Folder.rank, reverse=True)
        cov = len(other.keys & keeper.keys) / len(other.keys)
        groups.append((3, "folder", "review", "different releases (editions?)", keeper.path, None,
                       other.size, [(keeper.path, "keep", 1.0, keeper.stats()),
                                    (other.path, "drop", round(cov, 3), other.stats())]))

    dropped_folders = {m[0] for g in groups if g[1] == "folder" for m in g[7] if m[1] == "drop"}
    tier1_dropped = set()

    # Tier 1: identical bytes, skipping pairs already handled as whole folders above.
    by_hash = defaultdict(list)
    for r in rows:
        by_hash[r["sha256"]].append(r)
    for same in by_hash.values():
        if len(same) < 2:
            continue
        if all(_folder(r["path"]) in in_tier2 for r in same):
            continue
        ranked = sorted(same, key=_file_rank, reverse=True)
        mem = [(ranked[0]["path"], "keep", 1.0, None)] + [(r["path"], "drop", 1.0, None) for r in ranked[1:]]
        tier1_dropped.update(r["path"] for r in ranked[1:])
        groups.append((1, "file", "auto", "identical bytes", ranked[0]["path"], None,
                       sum(r["size"] for r in ranked[1:]), mem))

    # Tier 2, file scope: a second copy of a track inside the same folder. Alternate mixes
    # can share an AcoustID ("Dust" vs "Dust (Alternate Mix)"), so titles must match fully.
    in_folder = defaultdict(list)
    for r in rows:
        d = _folder(r["path"])
        if r["acoustid_id"] and r["title"] and d not in dropped_folders and r["path"] not in tier1_dropped:
            in_folder[(d, r["acoustid_id"], r["title"])].append(r)
    for same in in_folder.values():
        if len(same) < 2:
            continue
        ranked = sorted(same, key=_file_rank, reverse=True)
        keeper = ranked[0]
        losers = [r for r in ranked[1:] if abs((r["duration"] or 0) - (keeper["duration"] or 0)) < 2]
        if not losers:
            continue
        mem = [(keeper["path"], "keep", 1.0, None)] + [(r["path"], "drop", 1.0, None) for r in losers]
        numbers = {_filename_number(r["path"]) for r in [keeper, *losers]} - {None}
        if len(numbers) > 1:
            action, reason = "review", "same audio and title but different track numbers: may be intentional"
        else:
            action, reason = "auto", "second copy in the same folder"
        groups.append((2, "file", action, reason, keeper["path"], None,
                       sum(r["size"] for r in losers), mem))
    return groups


def summary(conn: sqlite3.Connection, top: int = 5) -> dict:
    by = [dict(r) for r in conn.execute("""
        SELECT tier, action, COUNT(*) AS groups, SUM(reclaimable) AS reclaimable_bytes,
               SUM(carry_tags_from IS NOT NULL) AS carry_tags
        FROM dupe_groups GROUP BY tier, action ORDER BY tier, action""")]
    examples = {}
    for tier in (1, 2, 3):
        examples[f"tier{tier}"] = []
        for g in conn.execute("SELECT * FROM dupe_groups WHERE tier = ? ORDER BY reclaimable DESC LIMIT ?",
                              (tier, top)):
            members = [dict(m) for m in conn.execute(
                "SELECT path, role, coverage, stats FROM dupe_members WHERE group_id = ?", (g["id"],))]
            for m in members:
                m["stats"] = json.loads(m["stats"]) if m["stats"] else None
            examples[f"tier{tier}"].append({**dict(g), "members": members})
    return {"by_tier": by, "examples": examples}
