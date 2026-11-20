"""Ways to run a game other than Proton: the system's Wine, or a native Linux binary.

They look like `steam.Proton` entries (internal names "system-wine" and "native")
so they can sit in the same pickers. Steam itself can't run a game with plain
Wine, so for those a small shell script becomes the Steam shortcut's target;
its first lines record the real game so Proton Porter can find it again.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from . import steam

WINE = "system-wine"
NATIVE = "native"
MARK = "# proton-porter-wine: "
LINUX_SUFFIXES = (".sh", ".x86_64", ".x86", ".appimage", ".bin", ".run")


def wine_binary() -> str | None:
    return shutil.which("wine")


def pseudo(internal: str) -> steam.Proton | None:
    if internal == WINE:
        w = wine_binary()
        return steam.Proton(WINE, "System Wine (no Proton)", Path(w).parent) if w else None
    if internal == NATIVE:
        return steam.Proton(NATIVE, "Native Linux (no Proton)", Path("/"))
    return None


def all_runners() -> list[steam.Proton]:
    """Every Proton Steam knows, then System Wine (if installed) and Native Linux."""
    return steam.protons() + [r for r in (pseudo(WINE), pseudo(NATIVE)) if r]


def is_pseudo(tool: steam.Proton | str | None) -> bool:
    name = tool.internal if isinstance(tool, steam.Proton) else tool
    return name in (WINE, NATIVE)


def is_native(path: Path) -> bool:
    """A Linux program (ELF binary or a script), not a Windows one."""
    path = Path(path)
    if not path.is_file() or read_wrapper(path):
        return False
    try:
        head = path.open("rb").read(4)
    except OSError:
        return False
    return head == b"\x7fELF" or head[:2] == b"#!" or path.suffix.lower() in LINUX_SUFFIXES and head[:2] != b"MZ"


def _data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "proton-porter"


def _key(exe: Path) -> str:
    return hashlib.sha1(str(Path(exe).resolve()).encode()).hexdigest()[:12]


def wine_prefix(exe: Path) -> Path:
    return _data() / "wine-prefixes" / _key(exe)


def wrapper_path(exe: Path) -> Path:
    return _data() / "wine-run" / f"{Path(exe).stem}-{_key(exe)}.sh"


def write_wrapper(exe: Path, start_dir: Path) -> Path:
    """The script Steam runs instead of the game: plain Wine, its own prefix."""
    exe = Path(exe).resolve()
    dest = wrapper_path(exe)
    dest.parent.mkdir(parents=True, exist_ok=True)
    q = lambda p: "'" + str(p).replace("'", "'\\''") + "'"  # noqa: E731
    dest.write_text(
        "#!/bin/sh\n"
        f"{MARK}{exe}\n"
        "# Made by Proton Porter: runs the game with the system's Wine instead of Proton.\n"
        f"export WINEPREFIX={q(wine_prefix(exe))}\n"
        f"cd {q(start_dir)} || exit 1\n"
        f"exec wine {q(exe)} \"$@\"\n")
    dest.chmod(0o755)
    return dest


def read_wrapper(path: Path) -> Path | None:
    """The real game behind one of our wrapper scripts, or None."""
    path = Path(path)
    if path.suffix != ".sh" or not path.is_file():
        return None
    try:
        with path.open(errors="replace") as fh:
            for _ in range(3):
                line = fh.readline()
                if line.startswith(MARK):
                    return Path(line[len(MARK):].strip())
    except OSError:
        pass
    return None
