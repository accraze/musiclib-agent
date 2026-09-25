"""Read-only scan of a single audio file: tags, stream info, SHA-256, Chromaprint.

Nothing here opens a file for writing. Keep it that way (SPEC: Safety rules #1).
"""

import hashlib
import json
import subprocess
from pathlib import Path

import mutagen

AUDIO_EXTS = {
    "mp3", "flac", "ogg", "opus", "m4a", "mp4", "aac", "wma",
    "wav", "aif", "aiff", "ape", "wv", "mpc",
}
LOSSLESS_CODECS = {"flac", "wav", "aiff", "ape", "wavpack", "alac"}

# Normalized tag key -> lowercase raw keys seen across ID3 (easy), Vorbis, MP4 (easy), ASF, APE.
TAG_ALIASES = {
    "artist": ["artist", "author", "wm/artist"],
    "albumartist": ["albumartist", "album artist", "wm/albumartist", "album_artist"],
    "album": ["album", "wm/albumtitle"],
    "title": ["title"],
    "track": ["tracknumber", "track", "wm/tracknumber"],
    "disc": ["discnumber", "disc", "wm/partofset"],
    "date": ["date", "year", "originaldate", "wm/year"],
    "mb_trackid": ["musicbrainz_trackid", "musicbrainz/track id", "musicbrainz track id"],
    "mb_releasetrackid": ["musicbrainz_releasetrackid", "musicbrainz/release track id",
                          "musicbrainz release track id"],
    "mb_albumid": ["musicbrainz_albumid", "musicbrainz/album id", "musicbrainz album id"],
    "mb_artistid": ["musicbrainz_artistid", "musicbrainz/artist id", "musicbrainz artist id"],
    "mb_releasegroupid": ["musicbrainz_releasegroupid", "musicbrainz/release group id",
                          "musicbrainz release group id"],
    "acoustid_id": ["acoustid_id", "acoustid/id", "acoustid id"],
}

_ART_KEYS = ("apic", "covr", "metadata_block_picture", "wm/picture", "cover art")
_HASH_CHUNK = 1 << 20


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path: Path, timeout: int = 120) -> tuple[int, str, str | None]:
    """Chromaprint of the first 120 s, as used by AcoustID.

    Decodes past corrupt frames; returns (duration, fingerprint, decoder warning or None).
    """
    out = subprocess.run(
        ["fpcalc", "-json", "-length", "120", "-ignore-errors", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"fpcalc: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout)
    warning = out.stderr.strip()[:200] or None
    return round(data["duration"]), data["fingerprint"], warning


def _text(value) -> str | None:
    """Flatten a mutagen tag value to text; None for binary (art, blobs)."""
    if isinstance(value, (bytes, bytearray)):
        return None
    if isinstance(value, list):
        parts = [t for t in (_text(v) for v in value) if t]
        return "; ".join(parts) if parts else None
    if hasattr(value, "text"):  # ID3 frames
        return _text(list(value.text))
    if hasattr(value, "data") and not isinstance(value, str):  # UFID, ASF byte values
        data = value.data
        if isinstance(data, bytes):
            try:
                return data.decode("ascii")
            except UnicodeDecodeError:
                return None
        return str(data)
    s = str(value).strip()
    return s or None


def _raw_tags(path: Path):
    """Return (mutagen file, {lowercase key: text}). Uses easy mode where it exists."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("unrecognized audio format")
    tags: dict[str, str] = {}
    if audio.tags is not None:
        for key, value in audio.tags.items():
            k = str(key).lower()
            if k.startswith(_ART_KEYS):
                continue
            if k.startswith("txxx:"):  # non-easy ID3 fallback
                k = k[5:]
            t = _text(value)
            if t:
                tags[k] = t[:2000]
    return audio, tags


def _has_art(path: Path, audio) -> bool:
    if getattr(audio, "pictures", None):  # FLAC
        return True
    full = mutagen.File(path)  # non-easy view exposes APIC / covr / pictures
    if full is None or full.tags is None:
        return False
    return any(str(k).lower().startswith(_ART_KEYS) for k in full.tags.keys())


def _codec(audio, ext: str) -> str:
    name = type(audio).__name__.lower()
    if name.startswith("easy"):
        name = name[4:]
    if name == "mp4":
        codec = getattr(audio.info, "codec", "") or ""
        return "alac" if codec.startswith("alac") else "aac"
    return {"mp3": "mp3", "oggvorbis": "vorbis", "oggopus": "opus", "asf": "wma",
            "wave": "wav", "monkeysaudio": "ape", "musepack": "mpc"}.get(name, name or ext)


def scan_file(path: Path, *, do_fingerprint: bool = True) -> dict:
    """Scan one file. Always returns a dict; failures go in 'error'."""
    rec: dict = {}
    errors, warnings = [], []
    try:
        rec["sha256"] = sha256(path)
    except OSError as e:
        return {"error": f"read: {e}"}

    try:
        audio, tags = _raw_tags(path)
        info = audio.info
        codec = _codec(audio, path.suffix[1:].lower())
        rec.update(
            codec=codec,
            lossless=int(codec in LOSSLESS_CODECS),
            bitrate=getattr(info, "bitrate", None) or None,
            sample_rate=getattr(info, "sample_rate", None),
            bit_depth=getattr(info, "bits_per_sample", None),
            channels=getattr(info, "channels", None),
            duration=round(getattr(info, "length", 0) or 0, 3) or None,
        )
        mode = getattr(info, "bitrate_mode", None)
        if mode is not None and codec == "mp3":
            rec["bitrate_mode"] = {1: "CBR", 2: "VBR", 3: "ABR"}.get(int(mode))
        for field, aliases in TAG_ALIASES.items():
            rec[field] = next((tags[a] for a in aliases if a in tags), None)
        rec["tags_json"] = json.dumps(tags, ensure_ascii=False, sort_keys=True)
        rec["has_art"] = int(_has_art(path, audio))
    except Exception as e:  # corrupt or odd files are expected in a dump this size
        errors.append(f"tags: {type(e).__name__}: {e}"[:300])

    if do_fingerprint:
        try:
            rec["fp_duration"], rec["fingerprint"], warning = fingerprint(path)
            if warning:
                warnings.append(f"decode: {warning}")
        except Exception as e:
            errors.append(f"fingerprint: {e}"[:300])

    if errors:
        rec["error"] = " | ".join(errors)
    if warnings:
        rec["warnings"] = " | ".join(warnings)
    return rec
