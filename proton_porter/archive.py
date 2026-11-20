"""Extract .rar/.7z/.zip/.tar.* archives and prove every file came out intact."""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Progress = Callable[[str, float | None], None]

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst", ".tzst")
ARCHIVE_SUFFIXES = TAR_SUFFIXES + (".zip", ".7z", ".rar", ".001")


class ArchiveError(RuntimeError):
    pass


@dataclass
class Entry:
    path: str  # as stored, with forward slashes
    size: int
    crc: int | None = None  # None when the format doesn't record one


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def is_tar(path: Path) -> bool:
    return path.name.lower().endswith(TAR_SUFFIXES)


def _seven_zip() -> str:
    for name in ("7z", "7zz", "7za", "7zr"):
        if shutil.which(name):
            return name
    raise ArchiveError("7-Zip isn't installed (pacman -S 7zip), so .rar/.7z/.zip can't be unpacked")


def _tar_open(path: Path):
    """(tarfile, process-or-None). .zst goes through the zstd tool."""
    if path.name.lower().endswith((".tar.zst", ".tzst")):
        if not shutil.which("zstd"):
            raise ArchiveError("zstd isn't installed, so .tar.zst can't be unpacked")
        proc = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return tarfile.open(fileobj=proc.stdout, mode="r|"), proc
    return tarfile.open(path, mode="r:*"), None


# ---------------------------------------------------------------------- listing

def _safe(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", name))


def list_entries(path: Path) -> list[Entry]:
    """Every file in the archive with its size (and CRC when recorded)."""
    path = Path(path)
    if is_tar(path):
        tf, proc = _tar_open(path)
        try:
            out = [Entry(m.name.rstrip("/"), m.size) for m in tf if m.isreg()]
        except (tarfile.TarError, OSError, EOFError) as error:
            raise ArchiveError(f"Can't read {path.name}: {error}") from error
        finally:
            tf.close()
            if proc:
                proc.kill()
                proc.wait()
        return out
    res = subprocess.run([_seven_zip(), "l", "-slt", "-ba", "-sccUTF-8", str(path)],
                         capture_output=True, text=True, errors="replace")
    if res.returncode not in (0, 1) and not res.stdout.strip():
        raise ArchiveError(f"Can't read {path.name}: {res.stderr.strip() or res.stdout.strip()}")
    out: list[Entry] = []
    block: dict[str, str] = {}
    for line in (res.stdout + "\n\n").splitlines():
        if not line.strip():
            if block.get("Path") and block.get("Folder", "-") != "+" and not block.get("Attributes", "").startswith("D"):
                crc = block.get("CRC", "").strip()
                try:
                    size = int(block.get("Size", "0") or 0)
                except ValueError:
                    size = 0
                out.append(Entry(block["Path"].replace("\\", "/"), size, int(crc, 16) if crc else None))
            block = {}
        elif " = " in line:
            k, v = line.split(" = ", 1)
            block[k.strip()] = v
    if not out and res.returncode:
        raise ArchiveError(f"Can't read {path.name}: {res.stderr.strip() or 'unknown format'}")
    return out


# ------------------------------------------------------------------- extracting

def _crc_of(path: Path) -> int:
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def verify(dest: Path, entries: list[Entry], progress: Progress = lambda *_: None) -> list[str]:
    """Problems found (empty list = every file present, right size, right CRC)."""
    problems = []
    total = len(entries) or 1
    for i, e in enumerate(entries, 1):
        p = dest / e.path
        if i % 25 == 0 or i == total:
            progress(f"Verifying {i}/{total}", i / total)
        if not p.is_file():
            problems.append(f"missing: {e.path}")
        elif p.stat().st_size != e.size:
            problems.append(f"wrong size: {e.path} ({p.stat().st_size} instead of {e.size})")
        elif e.crc is not None and _crc_of(p) != e.crc:
            problems.append(f"damaged (CRC): {e.path}")
    return problems


def _extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Safe tar extraction. Python 3.12+ (and patched 3.8-3.11) have filter="data"; older ones get
    the same rules by hand: only plain files and folders, nothing outside dest."""
    if hasattr(tarfile, "data_filter"):
        tf.extractall(dest, filter="data")
        return
    root = dest.resolve()
    for m in tf:
        target = (dest / m.name).resolve()
        if not _safe(m.name) or (target != root and root not in target.parents):
            raise ArchiveError(f"unsafe path in archive: {m.name}")
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif m.isreg():
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if m.mode & 0o111 else 0o644)
        # links, devices and fifos are skipped, as filter="data" would refuse them


def extract(archive: Path, dest: Path, password: str = "", progress: Progress = lambda *_: None) -> list[Entry]:
    """Unpack into `dest` (created, must be empty or new) and verify every file.

    Raises ArchiveError listing the problems; the partly extracted folder is left
    so it can be looked at, but it is never reported as done.
    """
    archive, dest = Path(archive), Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ArchiveError(f"{dest} already exists and isn't empty")
    progress(f"Reading {archive.name}", None)
    entries = list_entries(archive)
    if not entries:
        raise ArchiveError(f"{archive.name} has no files in it")
    unsafe = [e.path for e in entries if not _safe(e.path)]
    if unsafe:
        raise ArchiveError(f"{archive.name} has unsafe paths ({unsafe[0]}); not extracting")
    dest.mkdir(parents=True, exist_ok=True)
    progress(f"Extracting {archive.name} ({len(entries)} files)", None)
    if is_tar(archive):
        tf, proc = _tar_open(archive)
        try:
            _extract_tar(tf, dest)
        except (tarfile.TarError, OSError, EOFError) as error:
            raise ArchiveError(f"Extracting failed: {error}") from error
        finally:
            tf.close()
            if proc:
                proc.kill()
                proc.wait()
    else:
        cmd = [_seven_zip(), "x", "-y", "-bso0", "-bsp0", f"-o{dest}", f"-p{password}", str(archive)]
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if res.returncode != 0:
            raise ArchiveError(f"7-Zip failed (code {res.returncode}): "
                               f"{(res.stderr or res.stdout).strip().splitlines()[-1:] or ['no message']}")
    problems = verify(dest, entries, progress)
    if problems:
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise ArchiveError("Extracted files don't match the archive: " + "; ".join(problems[:5]) + more)
    return entries


def top_folder(dest: Path) -> Path:
    """The game folder inside an extracted archive: the single top-level folder if there is one."""
    items = [p for p in dest.iterdir()]
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return dest


def install_game_archive(archive: Path, games_dir: Path, progress: Progress = lambda *_: None,
                         password: str = "") -> Path:
    """Extract an archive into games_dir/<name>, verified. Returns the game's folder."""
    archive = Path(archive)
    name = re.sub(r"(\.part0*1)?(\.tar)?\.[A-Za-z0-9]+$", "", archive.name)
    games_dir.mkdir(parents=True, exist_ok=True)
    dest = games_dir / name
    if dest.exists() and any(dest.iterdir()):
        raise ArchiveError(f"{dest} already exists")
    stage = games_dir / f".{name}.extracting"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        extract(archive, stage, password, progress)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)  # nothing usable in a failed extraction
        raise
    inner = top_folder(stage)
    if inner != stage:
        dest = games_dir / inner.name
    if dest.exists() and any(dest.iterdir()):
        shutil.rmtree(stage, ignore_errors=True)
        raise ArchiveError(f"{dest} already exists")
    if dest.exists():
        dest.rmdir()
    inner.rename(dest)
    if stage.exists() and inner != stage:
        shutil.rmtree(stage, ignore_errors=True)
    return dest
