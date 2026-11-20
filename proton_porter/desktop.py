"""Per-game entries in the desktop's app launcher, with the game's own icon.

Each entry starts the game through Steam (steam://rungameid/...), so Proton,
launch options and the overlay apply exactly as in Steam.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import steam


def apps_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"


def entry_path(appid: int) -> Path:
    return apps_dir() / f"proton-porter-{appid}.desktop"


def exists(appid: int) -> bool:
    return entry_path(appid).is_file()


def _icon_for(e: steam.LibraryEntry) -> str:
    """A PNG of the game's icon: the one Steam uses, else extracted from the .exe now."""
    if e.icon and Path(e.icon).suffix.lower() in (".png", ".svg", ".jpg", ".jpeg") and Path(e.icon).is_file():
        return e.icon
    from .apply import data_dir, extract_icon
    have = data_dir() / "icons" / f"{e.appid}.png"
    if have.is_file():
        return str(have)
    made = extract_icon(e.exe, e.appid) if e.exe.is_file() else ""
    return made if made.endswith(".png") else "steam"


def _clean(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def add(e: steam.LibraryEntry) -> Path:
    path = entry_path(e.appid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_clean(e.name)}\n"
        "Comment=Runs through Steam (added by Proton Porter)\n"
        f"Exec=steam steam://rungameid/{steam.rungameid(e.appid)}\n"
        f"Icon={_icon_for(e)}\n"
        "Terminal=false\n"
        "Categories=Game;\n"
        f"X-ProtonPorter-AppId={e.appid}\n")
    _refresh()
    return path


def remove(appid: int) -> bool:
    p = entry_path(appid)
    if not p.is_file():
        return False
    p.unlink()
    _refresh()
    return True


def _refresh() -> None:
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", "-q", str(apps_dir())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
