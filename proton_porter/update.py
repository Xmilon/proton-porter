"""Check GitHub's main branch for a newer version and install it over the running copy.

The repo may be private, so `gh` (already logged in) is tried first; plain
HTTPS is the fallback for a public repo.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from . import __version__, archive
from .config import REPO_URL

SLUG = REPO_URL.removeprefix("https://github.com/")
VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:4])


def _gh(args: list[str]) -> bytes | None:
    if not shutil.which("gh"):
        return None
    res = subprocess.run(["gh", "api", *args], capture_output=True, timeout=120)
    return res.stdout if res.returncode == 0 else None


def _http(url: str) -> bytes:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "proton-porter"}), timeout=60) as r:
            return r.read()
    except OSError as error:
        raise UpdateError(f"Couldn't reach GitHub ({error}). If the repository is private, log in with `gh auth login`.") from error


def remote_version() -> str:
    """The version string on the main branch."""
    data = _gh([f"repos/{SLUG}/contents/proton_porter/__init__.py?ref=main", "-H", "Accept: application/vnd.github.raw"])
    if data is None:
        data = _http(f"https://raw.githubusercontent.com/{SLUG}/main/proton_porter/__init__.py")
    m = VERSION_RE.search(data.decode(errors="replace"))
    if not m:
        raise UpdateError("Couldn't read the version on main")
    return m.group(1)


def check(local: str = __version__, fetch=remote_version) -> str | None:
    """The newer version on main, or None when this copy is current."""
    remote = fetch()
    return remote if parse_version(remote) > parse_version(local) else None


def installed_package() -> Path | None:
    """The package folder if this is the per-user install (~/.local/share/proton-porter/app)."""
    pkg = Path(__file__).resolve().parent
    return pkg if pkg.parent.name == "app" and pkg.parent.parent.name == "proton-porter" else None


def download_main(dest: Path) -> Path:
    """Fetch main's tarball into dest and return the unpacked source folder."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = dest / "main.tar.gz"
    data = _gh([f"repos/{SLUG}/tarball/main"])
    tar.write_bytes(data if data else _http(f"https://github.com/{SLUG}/archive/refs/heads/main.tar.gz"))
    out = dest / "src"
    try:
        archive.extract(tar, out)  # also checks every file
    except archive.ArchiveError as error:
        raise UpdateError(f"The download is damaged: {error}") from error
    src = archive.top_folder(out)
    if not (src / "proton_porter/__init__.py").is_file():
        raise UpdateError("The download doesn't contain Proton Porter")
    return src


def install(src: Path, package: Path | None = None) -> str:
    """Replace the installed package with src's. Returns the new version. Restart the app afterwards."""
    package = package or installed_package()
    if package is None:
        raise UpdateError("This copy isn't the per-user install (./install.sh), so it can't update itself. "
                          "Update it the way you installed it (git pull, package manager).")
    m = VERSION_RE.search((src / "proton_porter/__init__.py").read_text())
    new = package.with_name(package.name + ".new")
    old = package.with_name(package.name + ".old")
    shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(src / "proton_porter", new, ignore=shutil.ignore_patterns("__pycache__"))
    package.rename(old)
    try:
        new.rename(package)
    except OSError:
        old.rename(package)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return m.group(1) if m else "?"


def update(progress=lambda *_: None) -> str:
    """Check, download everything from main, install. Returns the new version, or '' if current."""
    progress("Checking GitHub", None)
    newer = check()
    if not newer:
        return ""
    if installed_package() is None:
        raise UpdateError(f"Version {newer} is available, but this copy can't update itself. "
                          "Run ./install.sh from an updated checkout, or update it the way you installed it.")
    with tempfile.TemporaryDirectory(prefix="proton-porter-update-") as tmp:
        progress(f"Downloading {newer}", None)
        src = download_main(Path(tmp))
        progress("Installing", None)
        return install(src)
