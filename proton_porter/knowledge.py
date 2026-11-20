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
