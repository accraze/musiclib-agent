"""Load musiclib.toml and resolve paths."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "musiclib.toml"


@dataclass(frozen=True)
class Config:
    source_dir: Path
    state_dir: Path

    @property
    def db_path(self) -> Path:
        return self.state_dir / "musiclib.db"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    base = path.resolve().parent

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (base / p)

    return Config(
        source_dir=resolve(data.get("source_dir", "/srv/data/media/music")),
        state_dir=resolve(data.get("state_dir", "state")),
    )
