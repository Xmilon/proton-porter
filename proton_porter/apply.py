"""Turn an Analysis plus the user's choices into a working Steam shortcut."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import steam
from .analyze import Analysis, SaveCandidate
from .pe import PEError, PEFile

Progress = Callable[[str, float | None], None]


@dataclass
class Choices:
    name: str
    proton: steam.Proton
    user: steam.SteamUser
    move_to: Path | None = None  # parent folder to move the game into
    args: str = ""  # arguments for the game itself
    extra_env: str = ""  # e.g. "DXVK_HUD=fps"
    collection: str = "Linux"
    saves: list[SaveCandidate] = field(default_factory=list)
    overlay: bool = True
    restart_steam: bool = True
    mangohud: bool = False
    gamemode: bool = False
    proton_log: bool = False
    artwork_key: str = ""  # SteamGridDB API key; "" = no artwork


@dataclass
class Result:
    appid: int
    name: str
    exe: Path
    launch_options: str
    updated: bool
    backup: Path | None
    moved_to: Path | None = None
    saves_copied: list[str] = field(default_factory=list)
    collection_ok: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class Job:
    analysis: Analysis
    dll_overrides: str
    choices: Choices


def data_dir() -> Path:
    d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "proton-porter"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- moving files

def _tree_size(path: Path) -> tuple[int, int]:
    files = size = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            files += 1
            size += p.stat().st_size
    return files, size


# A folder whose files changed this recently is probably still being extracted.
# Uses ctime too: extractors restore the archive's old mtimes.
MOVE_QUIET_SECONDS = 20.0


def folder_busy(src: Path, quiet_seconds: float | None = None) -> str | None:
    """Why the folder shouldn't be moved yet (still being extracted or copied), or None."""
    if quiet_seconds is None:
        quiet_seconds = MOVE_QUIET_SECONDS
    src = src.resolve()
    prefix = str(src) + os.sep
    # Another program has a file in it open for writing.
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith(prefix):
                    flags = (proc / "fdinfo" / fd.name).read_text().split("flags:")[1].split()[0]
                    if int(flags, 8) & (os.O_WRONLY | os.O_RDWR):
                        comm = (proc / "comm").read_text().strip()
                        return f"{comm} is still writing to it"
        except (OSError, IndexError, ValueError):
            continue
    # Something changed in it a moment ago.
    newest = 0.0
    for p in [src, *src.rglob("*")]:
        try:
            newest = max(newest, p.lstat().st_mtime, p.lstat().st_ctime)
        except OSError:
            continue
    if time.time() - newest < quiet_seconds:
        return "files in it changed a few seconds ago"
    return None


def move_game(src: Path, parent: Path, progress: Progress) -> Path:
    """Move a game folder, verifying the copy before deleting the original."""
    busy = folder_busy(src)
    if busy:
        raise RuntimeError(f"Not moving {src.name}: {busy}. If it's still being extracted or "
                           "copied, wait for that to finish and try again.")
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / src.name
    if dest.resolve() == src.resolve():
        return dest
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} already exists and isn't empty")
    if os.stat(src).st_dev == os.stat(parent).st_dev:
        progress(f"Moving {src.name}", None)
        if dest.exists():
            dest.rmdir()
        os.replace(src, dest)
        return dest
    files, total = _tree_size(src)
    done = 0

    def copy(s, d):
        nonlocal done
        shutil.copy2(s, d)
        done += os.path.getsize(s)
        progress(f"Copying {src.name}: {Path(s).name}", done / total if total else None)

    shutil.copytree(src, dest, symlinks=True, copy_function=copy)
    progress("Checking the copy", None)
    if _tree_size(dest) != (files, total):
        raise RuntimeError("The copy doesn't match the original; nothing was deleted")
    if _tree_size(src) != (files, total):
        shutil.rmtree(dest)
        raise RuntimeError(f"{src.name} changed while it was being copied (still extracting?). "
                           "Nothing was deleted; try again once it's finished.")
    progress("Removing the original", None)
    shutil.rmtree(src)
    return dest


# --------------------------------------------------------------- launch files

def linux_bat(bat: Path) -> Path:
    """Copy a .bat without `pause`/`taskkill`, and make `start` wait for the game."""
    out = []
    for line in bat.read_text(errors="replace").splitlines():
        low = line.strip().lower()
        if re.match(r"pause\b", low) or low.startswith("taskkill"):
            continue
        line = re.sub(r'(?i)\bstart\s+""\s+(?!/wait)', 'start "" /wait ', line)
        out.append(line)
    dest = bat.with_name(bat.stem + "-linux.bat")
    dest.write_text("\r\n".join(out) + "\r\n")
    return dest


def wrapper_bat(exe: Path, args: str) -> Path:
    """Steam mangles `%command% args` for non-Steam games, so arguments go in a .bat."""
    dest = exe.with_name(exe.stem + "-linux.bat")
    dest.write_text(f'@echo off\r\ncd /d "%~dp0"\r\nstart "" /wait "{exe.name}" {args}\r\n')
    return dest


def command_prefix(dll_overrides: str, c: Choices) -> str:
    """Everything that goes before %command%: variables, then wrappers."""
    parts = []
    if dll_overrides:
        parts.append(f'WINEDLLOVERRIDES="{dll_overrides}"')
    if c.proton_log:
        parts.append("PROTON_LOG=1")
    if c.extra_env.strip():
        parts.append(c.extra_env.strip())
    if c.gamemode:
        parts.append("gamemoderun")
    if c.mangohud:
        parts.append("mangohud")
    return " ".join(parts)


def launch_options(prefix: str, args: str) -> str:
    if prefix and not args:
        return f"{prefix} %command%"
    return args  # no prefix: Steam appends plain options to the command


def game_icon_file(exe: Path, root: Path) -> Path | None:
    """An .ico the game ships for itself (repacks often add icon.ico). Used for emulator bundles,
    where the .exe's own icon is the emulator's, not the game's."""
    for folder in dict.fromkeys((root, exe.parent)):
        for name in ("icon.ico", "game.ico", f"{root.name}.ico", f"{exe.stem}.ico"):
            p = folder / name
            if p.is_file():
                return p
    return None


def extract_icon(exe: Path, appid: int, ico_file: Path | None = None) -> str:
    """PNG (else .ico) of the game's icon, saved under the data folder. ico_file wins over the .exe's."""
    ico = b""
    if ico_file is not None:
        try:
            ico = ico_file.read_bytes()
        except OSError:
            ico = b""
    if not ico:
        try:
            ico = PEFile(exe).icon_ico()
        except (PEError, OSError):
            return ""
    if not ico:
        return ""
    folder = data_dir() / "icons"
    folder.mkdir(exist_ok=True)
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        loader = GdkPixbuf.PixbufLoader()
        loader.write(ico)
        loader.close()
        dest = folder / f"{appid}.png"
        loader.get_pixbuf().savev(str(dest), "png", [], [])
        return str(dest)
    except Exception:  # noqa: BLE001 - any loader failure falls back to the .ico
        dest = folder / f"{appid}.ico"
        dest.write_bytes(ico)
        return str(dest)


# ------------------------------------------------------------------- one game

def _add_one(job: Job, progress: Progress) -> Result:
    a, c = job.analysis, job.choices
    exe, root = a.exe, a.root
    moved = None
    notes: list[str] = []

    if c.move_to and c.move_to.resolve() != root.parent.resolve():
        new_root = move_game(root, c.move_to, progress)
        exe = new_root / exe.relative_to(root)
        root = moved = new_root

    from . import runners
    target = exe
    if a.bat_needs_copy:
        target = linux_bat(exe)
        notes.append(f"Made {target.name} (no pause/taskkill) and pointed Steam at it.")
    prefix = command_prefix(job.dll_overrides, c)
    args = c.args.strip()
    pseudo = runners.is_pseudo(c.proton)
    if c.proton.internal == runners.WINE:
        target = runners.write_wrapper(target, target.parent)
        notes.append(f"Runs with the system's Wine through {target.name} (own prefix: {runners.wine_prefix(exe)}).")
    if args and prefix and target.suffix.lower() == ".exe" and not pseudo:
        target = wrapper_bat(target, args)
        args = ""
        notes.append(f"Arguments go through {target.name}, since Steam can't combine them with variables.")
    # Linux programs (and our Wine script) take "VARS %command% args" fine; only Proton shortcuts can't.
    options = f"{prefix} %command% {args}".strip() if pseudo and prefix and args else launch_options(prefix, args)

    shortcut = steam.Shortcut(c.name.strip(), target, target.parent, options, allow_overlay=c.overlay)
    appid = steam.shortcut_appid(shortcut.exe_field, shortcut.name)
    progress(f"{c.name}: icon", None)
    shortcut.icon = extract_icon(exe, appid, game_icon_file(exe, root) if a.emulator else None)

    progress(f"{c.name}: adding to Steam", None)
    appid, updated = steam.write_shortcut(c.user, shortcut)
    if pseudo:
        steam.clear_compat_tool(appid)
    else:
        steam.set_compat_tool(appid, c.proton.internal)
    collection_ok = True
    if c.collection.strip():
        collection_ok = steam.add_to_collection(c.user, c.collection.strip(), appid)

    if c.artwork_key:
        progress(f"{c.name}: artwork", None)
        from .artwork import ArtworkError, download
        try:
            matched, files = download(c.name, appid, steam.grid_dir(c.user), c.artwork_key)
            if files:
                notes.append(f"Artwork from SteamGridDB (“{matched}”): {len(files)} image(s).")
        except ArtworkError as error:
            notes.append(f"No artwork: {error}.")

    copied = []
    if c.saves and pseudo:
        notes.append("Saves weren't copied: that only works with Proton prefixes.")
    elif c.saves:
        progress(f"{c.name}: creating the Proton prefix (the game is not started)", None)
        user_dir = steam.create_prefix(c.proton, appid)
        for s in c.saves:
            progress(f"{c.name}: copying saves to {s.dest}", None)
            dest = user_dir / s.dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(s.source, dest, dirs_exist_ok=True)
            copied.append(s.dest)
    return Result(appid, shortcut.name, target, options, updated, None, moved, copied, collection_ok, notes)


# ------------------------------------------------------------------------ main

def apply_many(jobs: list[Job], progress: Progress = lambda *_: None, keep_backups: int = 20,
               reopen_steam: bool = True) -> list[Result | Exception]:
    """Add several games with one Steam restart. Failures don't stop the rest."""
    if not jobs:
        return []
    if steam.game_running():
        raise RuntimeError("A Steam game is running. Close it first, Steam has to restart.")
    user = jobs[0].choices.user
    was_running = steam.steam_running()
    if was_running:
        progress("Closing Steam", None)
        steam.close_steam()
    progress("Backing up Steam's settings", None)
    saved = steam.backup(user, time.strftime("%Y%m%d-%H%M%S"))
    steam.prune_backups(keep_backups)
    results: list[Result | Exception] = []
    try:
        for job in jobs:
            try:
                r = _add_one(job, progress)
                r.backup = saved
                results.append(r)
            except Exception as error:  # noqa: BLE001 - report per game
                results.append(error)
    finally:
        if was_running and reopen_steam:
            progress("Reopening Steam", None)
            steam.open_steam()
    return results


def apply(a: Analysis, dll_overrides: str, c: Choices, progress: Progress = lambda *_: None,
          keep_backups: int = 20) -> Result:
    r = apply_many([Job(a, dll_overrides, c)], progress, keep_backups, c.restart_steam)[0]
    if isinstance(r, Exception):
        raise r
    return r


def describe(a: Analysis, dll_overrides: str, c: Choices) -> str:
    """A readable summary of what apply() would do."""
    prefix = command_prefix(dll_overrides, c)
    args = c.args.strip()
    lines = [f"Name:        {c.name}", f"Runs:        {a.exe}"]
    if c.move_to and c.move_to.resolve() != a.root.parent.resolve():
        lines.append(f"Moves:       {a.root} -> {c.move_to / a.root.name}")
    lines.append(f"Proton:      {c.proton.display} ({c.proton.internal})")
    if prefix and args:
        lines.append(f"Options:     {prefix} %command%   (arguments “{args}” via a -linux.bat)")
    else:
        lines.append(f"Options:     {launch_options(prefix, args) or '(none)'}")
    lines.append(f"Overlay:     {'on' if c.overlay else 'off'}")
    if c.collection:
        lines.append(f"Collection:  {c.collection}")
    if c.artwork_key:
        lines.append("Artwork:     from SteamGridDB")
    for s in c.saves:
        lines.append(f"Saves:       {s.source} -> C:\\users\\steamuser\\{s.dest.replace('/', chr(92))}")
    return "\n".join(lines)


def parse_env(text: str) -> str:
    """Validate 'A=1 B="x y"' style input; returns it normalised."""
    parts = shlex.split(text)
    for p in parts:
        if "=" not in p or not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", p):
            raise ValueError(f"“{p}” isn't NAME=value")
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in (p.split("=", 1) for p in parts))


def switch_runner(user: steam.SteamUser, entry: steam.LibraryEntry, tool: steam.Proton) -> None:
    """Make an existing library game use another Proton, System Wine, or no Proton. Steam must be closed."""
    from . import runners
    if tool.internal == runners.WINE:
        target = runners.write_wrapper(entry.exe, entry.start_dir)
        steam.write_shortcut(user, steam.Shortcut(entry.name, target, target.parent, entry.launch_options, entry.icon))
        steam.clear_compat_tool(entry.appid)
        return
    if entry.tool == runners.WINE:  # back from the Wine script to the game itself
        steam.write_shortcut(user, steam.Shortcut(entry.name, entry.exe, entry.start_dir, entry.launch_options,
                                                  entry.icon))
    if tool.internal == runners.NATIVE:
        steam.clear_compat_tool(entry.appid)
    else:
        steam.set_compat_tool(entry.appid, tool.internal)
