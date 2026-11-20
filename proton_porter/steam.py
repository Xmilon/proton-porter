"""Steam on Linux: users, libraries, Proton versions, shortcuts, collections.

Steam rewrites its config files from memory, so every write here must happen
while Steam is closed. `SteamSession` takes care of closing and reopening it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import vdf

STEAMID64_BASE = 76561197960265728


def steam_root() -> Path:
    env = os.environ.get("PROTON_PORTER_STEAM_ROOT")
    if env:
        return Path(env).expanduser()
    for p in ("~/.local/share/Steam", "~/.steam/steam", "~/.steam/root",
              "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"):
        q = Path(p).expanduser()
        if (q / "config").is_dir():
            return q.resolve()
    raise FileNotFoundError("Steam isn't installed (no ~/.local/share/Steam)")


def _vload(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return vdf.load(fh)


def _bload(path):
    with open(path, "rb") as fh:
        return vdf.binary_load(fh)


def _jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _lower_keys(d):
    return {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}


# ---------------------------------------------------------------------- users

@dataclass
class SteamUser:
    account_id: str
    name: str
    most_recent: bool

    @property
    def config(self) -> Path:
        return steam_root() / "userdata" / self.account_id / "config"


def users() -> list[SteamUser]:
    root = steam_root()
    out = []
    try:
        data = _vload(root / "config/loginusers.vdf")
        for sid, info in _lower_keys(data).get("users", {}).items():
            info = _lower_keys(info)
            acc = str(int(sid) - STEAMID64_BASE)
            if (root / "userdata" / acc).is_dir():
                out.append(SteamUser(acc, info.get("personaname") or info.get("accountname") or acc,
                                     info.get("mostrecent") == "1"))
    except (OSError, SyntaxError, ValueError):
        pass
    known = {u.account_id for u in out}
    for d in sorted((root / "userdata").glob("[0-9]*")):
        if d.name not in known and d.name != "0" and (d / "config").is_dir():
            out.append(SteamUser(d.name, d.name, False))
    out.sort(key=lambda u: not u.most_recent)
    return out


# ------------------------------------------------------------------ libraries

def libraries() -> list[Path]:
    try:
        root = steam_root()
    except FileNotFoundError:  # no Steam: no libraries
        return []
    out = [root]
    try:
        data = _vload(root / "config/libraryfolders.vdf")
        for v in _lower_keys(data).get("libraryfolders", {}).values():
            if isinstance(v, dict) and v.get("path"):
                p = Path(v["path"])
                if p.resolve() != root.resolve():
                    out.append(p)
    except (OSError, SyntaxError):
        pass
    return out


# --------------------------------------------------------------------- Proton

@dataclass
class Proton:
    internal: str  # what Steam stores in CompatToolMapping
    display: str
    path: Path  # folder that holds the `proton` script


def _valve_internal(dirname: str) -> str | None:
    n = dirname.lower()
    if n == "proton - experimental":
        return "proton_experimental"
    if n == "proton hotfix":
        return "proton_hotfix"
    m = re.fullmatch(r"proton (\d+)\.(\d+)( beta)?", n)
    if m:
        major, minor, beta = m.groups()
        name = f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"
        return name + ("_beta" if beta else "")
    return None


def protons() -> list[Proton]:
    out: list[Proton] = []
    try:
        own = [steam_root() / "compatibilitytools.d"]
    except FileNotFoundError:  # no Steam yet: only system-wide tools
        own = []
    for base in (*own, Path("~/.steam/root/compatibilitytools.d").expanduser(),
                 Path("/usr/share/steam/compatibilitytools.d"), Path("/usr/local/share/steam/compatibilitytools.d")):
        if not base.is_dir():
            continue
        for manifest in sorted(base.glob("*/compatibilitytool.vdf")) + sorted(base.glob("*.vdf")):
            try:
                data = _vload(manifest)
                tools = _lower_keys(_lower_keys(data).get("compatibilitytools", {})).get("compat_tools", {})
                for internal, info in tools.items():
                    info = _lower_keys(info)
                    path = (manifest.parent / info.get("install_path", ".")).resolve()
                    if (path / "proton").exists() and not any(p.internal == internal for p in out):
                        out.append(Proton(internal, info.get("display_name", internal), path))
            except (OSError, SyntaxError):
                continue
    for lib in libraries():
        for d in sorted((lib / "steamapps/common").glob("Proton*")):
            internal = _valve_internal(d.name)
            if internal and (d / "proton").exists() and not any(p.internal == internal for p in out):
                out.append(Proton(internal, d.name, d))
    return out


def default_proton(tools: list[Proton]) -> Proton | None:
    for want in ("Proton-GE", "GE-Proton", "proton_experimental"):
        for t in tools:
            if t.internal == want or t.internal.startswith(want):
                return t
    return tools[0] if tools else None


# ------------------------------------------------------------------ processes

def _procs() -> list[tuple[int, str]]:
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                out.append((int(d.name), (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")))
            except OSError:
                pass
    return out


def _sandboxed() -> bool:
    """Tests point PROTON_PORTER_STEAM_ROOT at a copy; never touch the real Steam then."""
    return bool(os.environ.get("PROTON_PORTER_STEAM_ROOT"))


def steam_running() -> bool:
    if _sandboxed():
        return False
    for _, cmd in _procs():
        if "steamwebhelper" in cmd or re.search(r"(^|/)steam(\s|$)", cmd.split(" -")[0]):
            return True
    return False


def game_running() -> bool:
    if _sandboxed():
        return False
    return any("SteamLaunch AppId=" in cmd for _, cmd in _procs())


def close_steam(timeout: float = 60) -> None:
    if not steam_running():
        return
    subprocess.run(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    end = time.time() + timeout
    while time.time() < end:
        if not steam_running():
            time.sleep(1.5)
            return
        time.sleep(1)
    raise TimeoutError("Steam didn't close within a minute")


def open_steam() -> None:
    subprocess.Popen(["steam", "-silent"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ------------------------------------------------------------------ shortcuts

def shortcut_appid(exe_field: str, name: str) -> int:
    """The unsigned 32-bit id Steam gives a non-Steam shortcut."""
    return (zlib.crc32((exe_field + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def rungameid(appid: int) -> int:
    return (appid << 32) | 0x02000000


@dataclass
class Shortcut:
    name: str
    exe: Path
    start_dir: Path
    launch_options: str = ""
    icon: str = ""
    allow_overlay: bool = True

    @property
    def exe_field(self) -> str:
        return f'"{self.exe}"'


def backup(user: SteamUser, stamp: str) -> Path:
    dest = _backups_root() / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for src in (user.config / "shortcuts.vdf", steam_root() / "config/config.vdf",
                user.config / "cloudstorage/cloud-storage-namespace-1.json",
                user.config / "cloudstorage/cloud-storage-namespace-1.modified.json"):
        if src.exists():
            shutil.copy2(src, dest / src.name)
    return dest


def write_shortcut(user: SteamUser, sc: Shortcut) -> tuple[int, bool]:
    """Add or update the shortcut. Returns (appid, updated_existing)."""
    path = user.config / "shortcuts.vdf"
    data = _bload(path) if path.exists() else {"shortcuts": {}}
    entries = data.setdefault("shortcuts", {})
    appid = shortcut_appid(sc.exe_field, sc.name)
    existing = None
    for entry in entries.values():
        same_exe = entry.get("Exe", entry.get("exe")) == sc.exe_field
        if same_exe or entry.get("AppName", entry.get("appname")) == sc.name:
            existing = entry
            break
    fields = {
        "AppName": sc.name,
        "Exe": sc.exe_field,
        "StartDir": f'"{sc.start_dir}/"',
        "icon": sc.icon,
        "LaunchOptions": sc.launch_options,
        "AllowOverlay": 1 if sc.allow_overlay else 0,
    }
    if existing is not None:
        appid = int(existing.get("appid", appid - 2**32)) & 0xFFFFFFFF
        existing.update(fields)
    else:
        entry = {
            "appid": appid - 2**32,
            **fields,
            "ShortcutPath": "", "IsHidden": 0, "AllowDesktopConfig": 1,
            "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0,
            "LastPlayTime": 0, "FlatpakAppID": "", "sortas": "", "tags": {},
        }
        entries[str(len(entries))] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        vdf.binary_dump(data, fh)
    return appid, existing is not None


def set_compat_tool(appid: int, tool: str) -> None:
    path = steam_root() / "config/config.vdf"
    data = _vload(path)
    node = data
    for key in ("InstallConfigStore", "Software", "Valve", "Steam"):
        match = next((k for k in node if k.lower() == key.lower()), key)
        node = node.setdefault(match, {})
    match = next((k for k in node if k.lower() == "compattoolmapping"), "CompatToolMapping")
    mapping = node.setdefault(match, {})
    mapping[str(appid)] = {"name": tool, "config": "", "priority": "250"}
    with open(path, "w", encoding="utf-8") as fh:
        vdf.dump(data, fh, pretty=True)


def add_to_collection(user: SteamUser, collection: str, appid: int) -> bool:
    """Add the shortcut to a library collection, creating it if needed.

    Collections live in Steam's cloud-synced key/value store; the key is also
    listed in the ".modified" file so Steam uploads it instead of replacing it.
    """
    folder = user.config / "cloudstorage"
    store = folder / "cloud-storage-namespace-1.json"
    if not store.exists():
        return False
    entries = _jload(store)
    versions = [int(v.get("version", 0)) for _, v in entries if isinstance(v, dict) and str(v.get("version", "0")).isdigit()]
    try:
        ns = _jload(folder / "cloud-storage-namespaces.json")
        versions += [int(v) for k, v in ns if int(k) == 1]
    except (OSError, ValueError):
        pass
    version = str(max(versions, default=0) + 1)

    target = None
    for key, value in entries:
        if not key.startswith("user-collections.") or value.get("is_deleted"):
            continue
        try:
            body = json.loads(value.get("value", "{}"))
        except ValueError:
            continue
        if body.get("name", "").strip().lower() == collection.strip().lower():
            target = (key, value, body)
            break
    if target is None:
        slug = re.sub(r"[^a-z0-9]", "", collection.lower())[:12] or "games"
        cid = f"uc-{slug}"
        key = f"user-collections.{cid}"
        body = {"id": cid, "name": collection, "added": [], "removed": []}
        value = {"key": key}
        entries.append([key, value])
    else:
        key, value, body = target
    if appid not in body.setdefault("added", []):
        body["added"].append(appid)
    body["removed"] = [x for x in body.get("removed", []) if x != appid]
    value.update(timestamp=int(time.time()), value=json.dumps(body, separators=(",", ":")), version=version)
    value.pop("conflictResolutionMethod", None)
    with open(store, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, separators=(",", ":"))
    modified = folder / "cloud-storage-namespace-1.modified.json"
    try:
        mod = _jload(modified)
    except (OSError, ValueError):
        mod = []
    if key not in mod:
        mod.append(key)
    with open(modified, "w") as fh:
        json.dump(mod, fh)
    return True


def collections(user: SteamUser) -> list[str]:
    store = user.config / "cloudstorage/cloud-storage-namespace-1.json"
    out = []
    try:
        for key, value in _jload(store):
            if key.startswith("user-collections.uc-") and not value.get("is_deleted"):
                name = json.loads(value.get("value", "{}")).get("name")
                if name:
                    out.append(name)
    except (OSError, ValueError):
        pass
    return out


# ------------------------------------------------------------------- prefixes

def prefix_dir(appid: int) -> Path:
    return steam_root() / "steamapps/compatdata" / str(appid)


def create_prefix(proton: Proton, appid: int, timeout: int = 300) -> Path:
    """Build the Wine prefix the game will use, without starting the game."""
    compat = prefix_dir(appid)
    compat.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(compat),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam_root()), SteamGameId=str(appid), SteamAppId="0")
    env.pop("PROTON_LOG", None)
    subprocess.run([str(proton.path / "proton"), "run", "wineboot", "-u"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    user_dir = compat / "pfx/drive_c/users/steamuser"
    if not user_dir.is_dir():
        raise RuntimeError("Proton didn't create the prefix")
    return user_dir


# --------------------------------------------------------------- management

@dataclass
class LibraryEntry:
    appid: int
    name: str
    exe: Path
    start_dir: Path
    launch_options: str
    tool: str
    icon: str = ""


def _backups_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "proton-porter/backups"


def compat_mapping() -> dict:
    try:
        data = _vload(steam_root() / "config/config.vdf")
    except (OSError, SyntaxError):
        return {}
    node = data
    for key in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
        node = next((v for k, v in node.items() if k.lower() == key.lower()), {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def library(user: SteamUser) -> list[LibraryEntry]:
    """Every non-Steam game in this account's library."""
    path = user.config / "shortcuts.vdf"
    if not path.exists():
        return []
    mapping = compat_mapping()
    out = []
    for e in _bload(path).get("shortcuts", {}).values():
        get = lambda k: e.get(k, e.get(k.lower(), ""))  # noqa: E731
        appid = int(get("appid") or 0) & 0xFFFFFFFF
        tool = mapping.get(str(appid), {}).get("name", "") if isinstance(mapping.get(str(appid)), dict) else ""
        exe = Path(str(get("Exe")).strip('"'))
        start = Path(str(get("StartDir")).strip('"'))
        from . import runners  # local: runners imports this module
        real = runners.read_wrapper(exe)
        if real is not None:  # our System Wine wrapper: show the real game
            exe, start, tool = real, real.parent, runners.WINE
        elif not tool and runners.is_native(exe):
            tool = runners.NATIVE
        out.append(LibraryEntry(appid, get("AppName"), exe, start, get("LaunchOptions"), tool,
                                str(get("icon")).strip('"')))
    out.sort(key=lambda x: x.name.lower())
    return out


def set_launch_options(user: SteamUser, appid: int, options: str) -> bool:
    """Change one shortcut's launch options, leaving everything else as it is. Steam must be closed."""
    path = user.config / "shortcuts.vdf"
    data = _bload(path)
    for e in data.get("shortcuts", {}).values():
        if (int(e.get("appid", 0)) & 0xFFFFFFFF) == appid:
            key = next((k for k in e if k.lower() == "launchoptions"), "LaunchOptions")
            e[key] = options
            with open(path, "wb") as fh:
                vdf.binary_dump(data, fh)
            return True
    return False


def clear_compat_tool(appid: int) -> None:
    """Forget the game's Proton choice (it then runs as a Linux program)."""
    cfg_path = steam_root() / "config/config.vdf"
    cfg = _vload(cfg_path)
    node = cfg
    for key in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
        node = next((v for k, v in node.items() if k.lower() == key.lower()), None)
        if not isinstance(node, dict):
            return
    if node.pop(str(appid), None) is not None:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            vdf.dump(cfg, fh, pretty=True)


def remove_shortcut(user: SteamUser, appid: int) -> bool:
    path = user.config / "shortcuts.vdf"
    data = _bload(path)
    old = data.get("shortcuts", {})
    keep = [e for e in old.values() if (int(e.get("appid", 0)) & 0xFFFFFFFF) != appid]
    if len(keep) == len(old):
        return False
    data["shortcuts"] = {str(i): e for i, e in enumerate(keep)}
    with open(path, "wb") as fh:
        vdf.binary_dump(data, fh)
    clear_compat_tool(appid)  # forget its Proton choice
    remove_from_collections(user, appid)
    for f in grid_dir(user).glob(f"{appid}*"):
        f.unlink()
    return True


def remove_from_collections(user: SteamUser, appid: int) -> None:
    folder = user.config / "cloudstorage"
    store = folder / "cloud-storage-namespace-1.json"
    if not store.exists():
        return
    entries = _jload(store)
    versions = [int(v.get("version", 0)) for _, v in entries if str(v.get("version", "0")).isdigit()]
    changed = []
    for key, value in entries:
        if not key.startswith("user-collections."):
            continue
        try:
            body = json.loads(value.get("value", "{}"))
        except ValueError:
            continue
        if appid in body.get("added", []):
            body["added"] = [x for x in body["added"] if x != appid]
            value.update(timestamp=int(time.time()), value=json.dumps(body, separators=(",", ":")),
                         version=str(max(versions, default=0) + 1 + len(changed)))
            changed.append(key)
    if not changed:
        return
    with open(store, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, separators=(",", ":"))
    modified = folder / "cloud-storage-namespace-1.modified.json"
    try:
        mod = _jload(modified)
    except (OSError, ValueError):
        mod = []
    with open(modified, "w") as fh:
        json.dump(mod + [k for k in changed if k not in mod], fh)


def grid_dir(user: SteamUser) -> Path:
    return user.config / "grid"


def backups() -> list[Path]:
    root = _backups_root()
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.is_dir() else []


def prune_backups(keep: int) -> None:
    for old in backups()[max(keep, 1):]:
        shutil.rmtree(old, ignore_errors=True)


def restore_backup(user: SteamUser, folder: Path) -> list[str]:
    """Put a backup's files back (Steam must be closed)."""
    targets = {
        "shortcuts.vdf": user.config / "shortcuts.vdf",
        "config.vdf": steam_root() / "config/config.vdf",
        "cloud-storage-namespace-1.json": user.config / "cloudstorage/cloud-storage-namespace-1.json",
        "cloud-storage-namespace-1.modified.json": user.config / "cloudstorage/cloud-storage-namespace-1.modified.json",
    }
    done = []
    for name, dest in targets.items():
        src = folder / name
        if src.exists():
            shutil.copy2(src, dest)
            done.append(name)
    return done
