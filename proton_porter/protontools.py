"""Install a custom Proton (Proton-GE, a game-specific build...) from an archive."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import archive, steam


def _find_proton_dir(root: Path) -> Path | None:
    """The folder that holds the `proton` script (root itself or up to 2 levels down)."""
    if (root / "proton").is_file():
        return root
    for depth in (1, 2):
        for p in sorted(root.glob("/".join(["*"] * depth) + "/proton")):
            if p.is_file():
                return p.parent
    return None


MANIFEST = '''"compatibilitytools"
{{
  "compat_tools"
  {{
    "{name}"
    {{
      "install_path" "."
      "display_name" "{name}"
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }}
  }}
}}
'''


def install_proton(archive_path: Path, progress: archive.Progress = lambda *_: None,
                   tools_dir: Path | None = None) -> steam.Proton:
    """Unpack, verify, and place a Proton build in Steam's compatibilitytools.d.

    Steam has to be restarted afterwards to list it.
    """
    tools_dir = tools_dir or steam.steam_root() / "compatibilitytools.d"
    tools_dir.mkdir(parents=True, exist_ok=True)
    stage = tools_dir / f".{Path(archive_path).name}.extracting"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        archive.extract(archive_path, stage, progress=progress)
        found = _find_proton_dir(stage)
        if found is None:
            raise archive.ArchiveError(f"{Path(archive_path).name} doesn't contain a Proton build (no `proton` script)")
        name = found.name if found != stage else Path(archive_path).name.split(".")[0]
        dest = tools_dir / name
        if dest.exists():
            raise archive.ArchiveError(f"{dest} already exists. Remove it first to reinstall.")
        script = found / "proton"
        script.chmod(script.stat().st_mode | 0o111)
        if not (found / "compatibilitytool.vdf").exists():
            (found / "compatibilitytool.vdf").write_text(MANIFEST.format(name=name))
        found.rename(dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    progress(f"Installed {name}", 1.0)
    tool = next((t for t in steam.protons() if t.path == dest.resolve()), None)
    return tool or steam.Proton(name, name, dest)
