"""Repack installers (FitGirl, DODI and similar): a setup.exe plus .bin archives.

These can't be unpacked directly (they use their own compressors), so the
installer itself runs under Proton in a separate prefix, with the install
folder preset to the games folder. The repack's MD5 list is checked first.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import steam

Progress = Callable[[str, float | None], None]


@dataclass
class Repack:
    kind: str  # "FitGirl", "DODI" or "Repack"
    folder: Path
    setup: Path
    bins: list[Path] = field(default_factory=list)
    md5_file: Path | None = None

    @property
    def name(self) -> str:
        n = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", self.folder.name)
        n = re.sub(r"(?i)\b(fitgirl|dodi)\b.*$|[-_.]?repack.*$", "", n)
        return n.strip(" -_.") or self.folder.name


def detect(path: Path) -> Repack | None:
    """A repack folder (or its setup.exe), or None."""
    path = Path(path)
    folder = path.parent if path.is_file() else path
    if not folder.is_dir():
        return None
    try:
        names = {p.name.lower(): p for p in folder.iterdir()}
    except OSError:
        return None
    setup = names.get("setup.exe")
    if setup is None:
        return None
    bins = sorted(p for n, p in names.items() if n.endswith(".bin") or re.match(r".*\.(bin|dat)\d*$", n))
    low = folder.name.lower()
    if "dodi" in low:  # the folder name is the strongest hint
        kind = "DODI"
    elif "fitgirl" in low or any(n.startswith("fg-") for n in names):
        kind = "FitGirl"
    elif any("dodi" in n for n in names):
        kind = "DODI"
    elif bins:
        kind = "Repack"
    else:
        return None
    md5 = next((p for p in folder.rglob("*.md5")), None)
    return Repack(kind, folder, setup, bins, md5)


def _md5(path: Path, progress: Progress, done: int, total: int) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
            done += len(chunk)
            progress(f"Checking {path.name}", done / total if total else None)
    return h.hexdigest()


def md5_entries(r: Repack) -> list[tuple[str, Path]]:
    """(hash, file) pairs from the repack's .md5 list. Paths are relative to the list's folder."""
    if not r.md5_file:
        return []
    out = []
    for line in r.md5_file.read_text(errors="replace").splitlines():
        line = line.strip()
        m = re.match(r"^([0-9a-fA-F]{32})\s+\*?(.+)$", line)
        if not m or line.startswith(";"):
            continue
        rel = m.group(2).strip().replace("\\", "/")
        out.append((m.group(1).lower(), (r.md5_file.parent / rel).resolve()))
    return out


def verify(r: Repack, progress: Progress = lambda *_: None) -> list[str]:
    """Problems found; [] = every listed file is present with the right MD5."""
    entries = md5_entries(r)
    if not entries:
        return []
    total = sum(p.stat().st_size for _, p in entries if p.is_file())
    done, problems = 0, []
    for want, p in entries:
        if not p.is_file():
            problems.append(f"missing: {p.name}")
            continue
        if _md5(p, progress, done, total) != want:
            problems.append(f"damaged: {p.name} (re-download it)")
        done += p.stat().st_size
    return problems


def installer_prefix() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "proton-porter/installer-prefix"
    base.mkdir(parents=True, exist_ok=True)
    return base


def windows_path(path: Path) -> str:
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def install_command(r: Repack, dest: Path, proton: steam.Proton) -> tuple[list[str], dict[str, str]]:
    """Proton (or the system's Wine) runs the installer; /DIR presets the install folder (Inno Setup)."""
    from . import runners
    if proton.internal == runners.WINE:
        env = dict(os.environ, WINEPREFIX=str(installer_prefix() / "wine"))
        return ["wine", str(r.setup), f"/DIR={windows_path(dest)}"], env
    env = dict(os.environ,
               STEAM_COMPAT_DATA_PATH=str(installer_prefix()),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam.steam_root()),
               SteamGameId="0", SteamAppId="0")
    env.pop("PROTON_LOG", None)
    return [str(proton.path / "proton"), "waitforexitandrun", str(r.setup), f"/DIR={windows_path(dest)}"], env


def install(r: Repack, games_dir: Path, proton: steam.Proton, progress: Progress = lambda *_: None,
            check: bool = True) -> Path:
    """Check the MD5 list, run the installer (the user clicks through it), return the game folder."""
    if check:
        problems = verify(r, progress)
        if problems:
            raise RuntimeError("The repack's files don't match its MD5 list: " + "; ".join(problems[:5]))
    dest = Path(games_dir) / r.name
    dest.mkdir(parents=True, exist_ok=True)
    cmd, env = install_command(r, dest, proton)
    progress(f"Running the {r.kind} installer: click through it. The folder is already set to {dest}", None)
    subprocess.run(cmd, env=env, cwd=r.folder, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if not any(dest.rglob("*.exe")):
        raise RuntimeError(f"The installer finished but nothing was installed in {dest}. "
                           "If you changed the folder in the installer, add the game from there.")
    return dest
