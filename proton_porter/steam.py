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
        data = vdf.load(open(root / "config/loginusers.vdf", encoding="utf-8", errors="replace"))
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
    root = steam_root()
    out = [root]
    try:
        data = vdf.load(open(root / "config/libraryfolders.vdf", encoding="utf-8", errors="replace"))
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
    root = steam_root()
    for base in (root / "compatibilitytools.d", Path("~/.steam/root/compatibilitytools.d").expanduser(),
                 Path("/usr/share/steam/compatibilitytools.d"), Path("/usr/local/share/steam/compatibilitytools.d")):
        if not base.is_dir():
            continue
        for manifest in sorted(base.glob("*/compatibilitytool.vdf")) + sorted(base.glob("*.vdf")):
            try:
                data = vdf.load(open(manifest, encoding="utf-8", errors="replace"))
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
    dest = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "proton-porter/backups" / stamp
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
    data = vdf.binary_load(open(path, "rb")) if path.exists() else {"shortcuts": {}}
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
    data = vdf.load(open(path, encoding="utf-8", errors="replace"))
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
    entries = json.load(open(store, encoding="utf-8"))
    versions = [int(v.get("version", 0)) for _, v in entries if isinstance(v, dict) and str(v.get("version", "0")).isdigit()]
    try:
        ns = json.load(open(folder / "cloud-storage-namespaces.json"))
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
        mod = json.load(open(modified))
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
        for key, value in json.load(open(store, encoding="utf-8")):
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
