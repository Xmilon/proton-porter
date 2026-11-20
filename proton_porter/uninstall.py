"""Uninstall a game: run its own uninstaller if it has one, else move its folder to the Trash.

Nothing is deleted outright: leftovers and the Proton folder go to the Trash
(`gio trash`), so they can be restored. Folders that could hold more than this
one game (home, the games folder itself, a folder with other library games in
it) are refused.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import steam
from .analyze import game_root

Progress = Callable[[str, float | None], None]
UNINSTALLER = re.compile(r"^(unins\d*|uninstall(er)?|uninst)\.exe$", re.I)


@dataclass
class Plan:
    appid: int
    name: str
    folder: Path | None  # game folder to clear out
    uninstaller: Path | None  # unins000.exe and friends
    prefix: Path | None  # Proton folder (compatdata/<appid>)
    blocked: list[str] = field(default_factory=list)  # reasons the folder must not be touched

    def describe(self, remove_prefix: bool) -> str:
        lines = []
        if self.uninstaller:
            lines.append(f"Run its uninstaller: {self.uninstaller.name}")
        if self.folder and not self.blocked:
            lines.append(f"Move what's left of {self.folder} to the Trash")
        for b in self.blocked:
            lines.append(f"Leave the files alone: {b}")
        if remove_prefix and self.prefix:
            lines.append(f"Move its Proton folder (settings, maybe saves) to the Trash: {self.prefix}")
        lines.append("Remove it from Steam")
        return "\n".join(f"• {x}" for x in lines)


def find_uninstaller(folder: Path) -> Path | None:
    """unins000.exe (Inno Setup: FitGirl, DODI, GOG) or uninstall.exe, at most two levels down."""
    for depth in ("*", "*/*", "*/*/*"):
        hits = sorted(p for p in folder.glob(depth) if p.is_file() and UNINSTALLER.match(p.name))
        if hits:
            return hits[0]
    return None


def game_folder(entry: steam.LibraryEntry) -> Path:
    exe = entry.exe
    if exe.suffix.lower() in (".bat", ".cmd"):
        return entry.start_dir if entry.start_dir.is_dir() else exe.parent
    return game_root(exe)


def why_unsafe(folder: Path, games_dir: Path, others: list[Path]) -> str | None:
    """Reason this folder must not be trashed, or None."""
    f = folder.resolve()
    home = Path.home().resolve()
    protected = {Path("/"), home, games_dir.resolve(), home / "Downloads", home / "Documents", home / "Desktop",
                 home / "Storage", home / ".local", home / ".local/share", steam.steam_root().resolve()}
    if f in protected:
        return f"{f} is a shared folder"
    if f == home or home not in f.parents:
        return f"{f} is outside your home folder"
    if len(f.relative_to(home).parts) < 2:
        return f"{f} is too close to your home folder"
    inside = [o for o in others if f in o.resolve().parents]
    if inside:
        return f"{f} also holds another game in your library ({inside[0].name})"
    return None


def make_plan(entry: steam.LibraryEntry, games_dir: Path, library: list[steam.LibraryEntry]) -> Plan:
    folder = game_folder(entry)
    # "Game-Site.com/Game/": take the wrapper too when it holds nothing else.
    try:
        parent = folder.parent
        if parent.resolve() != games_dir.resolve() and [p.name for p in parent.iterdir()] == [folder.name]:
            folder = parent
    except OSError:
        pass
    others = [e.exe for e in library if e.appid != entry.appid]
    from . import runners
    prefix = runners.wine_prefix(entry.exe) if entry.tool == runners.WINE else \
        None if entry.tool == runners.NATIVE else steam.prefix_dir(entry.appid)
    plan = Plan(entry.appid, entry.name, folder if folder.is_dir() else None,
                find_uninstaller(folder) if folder.is_dir() else None,
                prefix if prefix and prefix.is_dir() else None)
    if plan.folder:
        reason = why_unsafe(plan.folder, games_dir, others)
        if reason:
            plan.blocked.append(reason)
            plan.uninstaller = None  # an uninstaller in a shared folder isn't trusted either
    return plan


def trash(path: Path) -> None:
    """Move to the Trash (recoverable). Raises if that isn't possible."""
    if not shutil.which("gio"):
        raise RuntimeError("`gio` isn't available, so nothing can be moved to the Trash")
    res = subprocess.run(["gio", "trash", str(path)], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Couldn't move {path} to the Trash: {res.stderr.strip()}")


def run_uninstaller(path: Path, proton: steam.Proton) -> None:
    """Run it under Proton in the installer prefix (where repacks were installed)."""
    from .repack import installer_prefix
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(installer_prefix()),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam.steam_root()), SteamGameId="0", SteamAppId="0")
    env.pop("PROTON_LOG", None)
    subprocess.run([str(proton.path / "proton"), "waitforexitandrun", str(path)], env=env, cwd=path.parent,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def execute(plan: Plan, proton: steam.Proton | None, remove_prefix: bool,
            progress: Progress = lambda *_: None) -> list[str]:
    """Do the file part (uninstaller, Trash). Removing the Steam entry is separate: Steam must be closed."""
    notes = []
    if plan.uninstaller and proton:
        progress(f"Running {plan.uninstaller.name}: click through it", None)
        run_uninstaller(plan.uninstaller, proton)
        notes.append(f"Ran {plan.uninstaller.name}.")
    if plan.folder and not plan.blocked and plan.folder.exists():
        progress(f"Moving {plan.folder.name} to the Trash", None)
        trash(plan.folder)
        notes.append(f"Moved {plan.folder} to the Trash.")
    if remove_prefix and plan.prefix and plan.prefix.exists():
        progress("Moving the Proton folder to the Trash", None)
        trash(plan.prefix)
        notes.append(f"Moved {plan.prefix} to the Trash.")
    notes += [f"Didn't touch the files: {b}." for b in plan.blocked]
    return notes
