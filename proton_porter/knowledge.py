"""Known cracks and mods, and what each needs under Proton (data/known_mods.json)."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

_CACHE: list[dict] | None = None


@dataclass
class KnownMod:
    id: str
    name: str
    overrides: list[str] = field(default_factory=list)  # DLLs that need "native, builtin"
    notes: list[str] = field(default_factory=list)
    proton: str = ""  # Proton name that is known to work, if any


def load(path: Path | None = None) -> list[dict]:
    global _CACHE
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    if _CACHE is None:
        try:
            from importlib import resources  # works inside the .pyz too
            _CACHE = json.loads(resources.files("proton_porter").joinpath("data/known_mods.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, ModuleNotFoundError):
            _CACHE = []
    return _CACHE


def _matches(pattern: str, names: set[str], folders: list[Path]) -> bool:
    pattern = pattern.lower()
    if "/" not in pattern and not any(c in pattern for c in "*?["):
        return pattern in names
    for folder in folders:
        try:
            for p in folder.rglob("*") if "/" in pattern else folder.iterdir():
                rel = (p.relative_to(folder)).as_posix().lower()
                if fnmatch.fnmatch(rel, pattern):
                    return True
        except OSError:
            continue
    return False


def detect(exe_dir: Path, root: Path, entries: list[dict] | None = None) -> list[KnownMod]:
    """Which known mods/cracks are in this game folder."""
    entries = load() if entries is None else entries
    folders = [exe_dir] if exe_dir == root else [exe_dir, root]
    names: set[str] = set()
    for f in folders:
        try:
            names |= {p.name.lower() for p in f.iterdir()}
        except OSError:
            pass
    dlls_here = {n[:-4] for n in names if n.endswith(".dll")}
    found = []
    for e in entries:
        hit = any(_matches(p, names, folders) for p in e.get("detect", []))
        for group in e.get("require", []):
            hit = hit and any(_matches(alt, names, folders) for alt in group.split("|"))
        if not hit:
            continue
        found.append(KnownMod(
            e["id"], e["name"],
            [o for o in e.get("overrides", []) if o.lower() in dlls_here],
            list(e.get("notes", [])), e.get("proton", "")))
    return found


# Emulators by .exe name (lower case, no .exe). Windows builds; most also have native Linux builds.
EMULATORS = {
    "lichii": "Lichii (Eden-based Switch 2 fork)", "fritchii": "Fritchii (Eden-based Switch 2 fork)",
    "eden": "Eden (Switch)", "yuzu": "yuzu (Switch)", "suyu": "suyu (Switch)", "sudachi": "Sudachi (Switch)",
    "citron": "Citron (Switch)", "ryujinx": "Ryujinx (Switch)", "cemu": "Cemu (Wii U)",
    "rpcs3": "RPCS3 (PS3)", "dolphin": "Dolphin (GameCube/Wii)", "pcsx2-qt": "PCSX2 (PS2)", "pcsx2": "PCSX2 (PS2)",
    "duckstation-qt-x64-releaseltcg": "DuckStation (PS1)", "duckstation": "DuckStation (PS1)",
    "ppssppwindows64": "PPSSPP (PSP)", "ppsspp": "PPSSPP (PSP)", "xenia": "Xenia (Xbox 360)",
    "xenia_canary": "Xenia Canary (Xbox 360)", "retroarch": "RetroArch", "ares": "ares", "mgba": "mGBA (GBA)",
    "melonds": "melonDS (DS)", "azahar": "Azahar (3DS)", "citra": "Citra (3DS)", "citra-qt": "Citra (3DS)",
    "lime3ds": "Lime3DS (3DS)", "shadps4": "shadPS4 (PS4)", "vita3k": "Vita3K (PS Vita)",
}
ROM_SUFFIXES = (".nsp", ".xci", ".nca", ".nup", ".nro", ".iso", ".wbfs", ".rvz", ".gcm", ".chd", ".cue",
                ".pkg", ".3ds", ".cia", ".cci", ".wua", ".rpx", ".wud", ".xex", ".gba", ".nds", ".cso", ".pbp")


@dataclass
class Emulator:
    name: str  # what it emulates, for messages
    ui_exe: Path  # the emulator's own window
    roms: list[Path] = field(default_factory=list)  # game files found next to it


def detect_emulator(exe: Path, root: Path) -> Emulator | None:
    """An emulator bundle around this .exe: the emulator's main program and the game files in it."""
    found: list[tuple[int, Path]] = []
    for folder in {exe.parent, root}:
        try:
            for p in folder.iterdir():
                if p.suffix.lower() == ".exe" and p.stem.lower() in EMULATORS:
                    found.append((p.stat().st_size, p))
        except OSError:
            continue
    if not found:
        return None
    ui = max(found)[1]  # the emulator proper is the biggest of them (a launcher is small)
    roms: list[Path] = []
    try:
        for p in root.rglob("*"):
            if p.suffix.lower() in ROM_SUFFIXES and p.is_file():
                roms.append(p)
                if len(roms) >= 20:
                    break
    except OSError:
        pass
    return Emulator(EMULATORS[ui.stem.lower()], ui, sorted(roms))
