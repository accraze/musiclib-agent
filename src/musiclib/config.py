"""Load musiclib.toml (+ gitignored musiclib.local.toml for secrets) and resolve paths."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "musiclib.toml"


@dataclass(frozen=True)
class Config:
    source_dir: Path
    state_dir: Path
    library_dir: Path | None = None
    inbox_dir: Path | None = None
    acoustid_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.state_dir / "musiclib.db"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def processed_dir(self) -> Path:
        """Claimed inbox folders live here, read-only from then on (D28)."""
        if self.inbox_dir is None:
            raise SystemExit("inbox_dir is not set in musiclib.toml")
        return self.inbox_dir / ".processed"


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    local = path.with_name(path.stem + ".local.toml")
    if local.exists():
        data.update(tomllib.loads(local.read_text()))
    base = path.resolve().parent

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return (p if p.is_absolute() else (base / p)).resolve()

    cfg = Config(
        source_dir=resolve(data.get("source_dir", "/srv/data/media/music")),
        state_dir=resolve(data.get("state_dir", "state")),
        library_dir=resolve(data["library_dir"]) if data.get("library_dir") else None,
        inbox_dir=resolve(data["inbox_dir"]) if data.get("inbox_dir") else None,
        acoustid_key=os.environ.get("ACOUSTID_KEY") or data.get("acoustid_key"),
    )
    for name in ("state_dir", "library_dir", "inbox_dir"):
        p = getattr(cfg, name)
        if p is not None and p.is_relative_to(cfg.source_dir):
            raise SystemExit(f"{name} {p} is inside source_dir; the dump is read-only")
    if cfg.inbox_dir is not None and cfg.library_dir is not None and (
            cfg.inbox_dir.is_relative_to(cfg.library_dir) or cfg.library_dir.is_relative_to(cfg.inbox_dir)):
        raise SystemExit("inbox_dir and library_dir must not contain each other (D27)")
    return cfg
