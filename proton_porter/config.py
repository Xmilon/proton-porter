"""User settings, kept in ~/.config/proton-porter/settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MAKER = "Xmilon"
MAKER_URL = "https://github.com/Xmilon"
REPO_URL = "https://github.com/Xmilon/proton-porter"


def _path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "proton-porter/settings.json"


def _default_games_dir() -> str:
    for p in ("~/Storage/Games", "~/Games", "~/games"):
        q = Path(p).expanduser()
        if q.is_dir():
            return str(q)
    return str(Path("~/Games").expanduser())


@dataclass
class Settings:
    games_dir: str = field(default_factory=_default_games_dir)
    move_by_default: bool = True
    proton: str = ""  # internal name; "" = best available
    collection: str = "Linux"
    reopen_steam: bool = True
    overlay: bool = True
    mangohud: bool = False
    gamemode: bool = False
    proton_log: bool = False
    extra_env: str = ""
    search_folders: list[str] = field(default_factory=list)
    import_saves: bool = True
    steamgriddb_key: str = ""
    fetch_artwork: bool = True
    keep_backups: int = 20
    test_workspace: int = 7  # games for tests only start on this workspace
    test_seconds: int = 60
    check_updates: bool = True
    repack_check_md5: bool = True  # check FitGirl/DODI .bin files against their MD5 list first
    repack_use_wine: bool = False  # run repack installers with the system's Wine instead of Proton

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads(_path().read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in raw.items() if k in known})
        return s

    def save(self) -> None:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.chmod(tmp, 0o600)  # may hold an API key
        tmp.replace(p)
