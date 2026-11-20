"""Look at a Windows game folder and work out how to run it under Proton."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pe import PEError, PEFile, PEInfo

# DLLs that Windows (and Wine) ship themselves. A copy next to the game's .exe
# is almost always a proxy/loader (mod loader, ASI loader, ReShade, BepInEx's
# doorstop...), and Wine ignores it unless told "native, then builtin".
PROXY_DLLS = {
    "winmm": "Proxy/loader DLL (common for mod and game loaders)",
    "version": "Proxy/loader DLL (common for mod and game loaders)",
    "dinput8": "Proxy/loader DLL (ASI loaders, script hooks)",
    "dinput": "Proxy/loader DLL",
    "winhttp": "Proxy/loader DLL (BepInEx / Unity Doorstop)",
    "wininet": "Proxy/loader DLL",
    "dsound": "Proxy/loader DLL (ASI loaders)",
    "dxgi": "Graphics proxy (ReShade, frame-rate or overlay mods)",
    "d3d9": "Graphics proxy (ReShade, ENB, widescreen fixes)",
    "d3d8": "Graphics proxy (d3d8to9 and similar)",
    "d3d10": "Graphics proxy",
    "d3d11": "Graphics proxy (ReShade, ENB)",
    "ddraw": "Graphics proxy (DDrawCompat, cnc-ddraw)",
    "xinput1_3": "Controller proxy (XInput plus, mods)",
    "xinput1_4": "Controller proxy",
    "xinput9_1_0": "Controller proxy",
    "iphlpapi": "Proxy/loader DLL",
    "msimg32": "Proxy/loader DLL",
    "uxtheme": "Proxy/loader DLL",
    "dwmapi": "Proxy/loader DLL",
    "hid": "Proxy/loader DLL",
    "msacm32": "Proxy/loader DLL",
    "winspool": "Proxy/loader DLL",
    "cryptbase": "Proxy/loader DLL",
    "opengl32": "Graphics proxy",
    "bink2w64": None,  # not a system DLL; listed so it is never flagged
}

# Real Microsoft DLLs games often carry; Wine's own copies are usually better,
# so these are shown but left off by default.
SOMETIMES_NATIVE = {
    "dbghelp": "Debug helper. Only turn on if the game needs its own copy",
    "d3dcompiler_43": "Shader compiler. Turn on if graphics fail to initialise",
    "d3dcompiler_47": "Shader compiler. Turn on if graphics fail to initialise",
    "d3d12": "Agility SDK. Leave off unless the game asks for it",
}

# Executables that are never the game itself.
NOT_THE_GAME = re.compile(
    r"(unitycrashhandler|crashreport|crashpad|unins\d*|uninstall|setup|vc_?redist|dxsetup|"
    r"dotnet|physx|easyanticheat|eac_?launcher|battleye|be_service|ue4prereq|uerequire|"
    r"cefprocess|webhelper|7z|zip|python|7za|cmdmenusel|depotdownloader|installer|uplay|"
    r"redistributable|crashhandler|firewall|helper)",
    re.I,
)

# Folder names that sit between the game's root and its .exe.
INNER_DIRS = {"binaries", "win64", "win32", "x64", "x86", "bin", "bin64", "bin32",
              "wingdk", "game", "shipping", "release", "_retail_", "retail"}


@dataclass
class DllChoice:
    name: str
    reason: str
    enabled: bool


@dataclass
class SaveCandidate:
    source: Path
    dest: str  # Windows-style path relative to C:\users\steamuser
    why: str


@dataclass
class Analysis:
    exe: Path
    root: Path  # the game's top folder (what gets moved)
    name: str
    engine: str = "Unknown"
    pe: PEInfo | None = None
    dlls: list[DllChoice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    saves: list[SaveCandidate] = field(default_factory=list)
    unity_ids: tuple[str, str] | None = None  # (company, product)
    bat_needs_copy: bool = False
    other_exes: list[Path] = field(default_factory=list)


def game_root(exe: Path) -> Path:
    """Walk up from the .exe past folders like Binaries/Win64."""
    root = exe.parent
    while root.parent != root and root.name.lower() in INNER_DIRS:
        root = root.parent
    # Unreal: <Game>/<Project>/Binaries/Win64/X.exe -> <Game>
    if exe.parent.name.lower() in ("win64", "wingdk", "win32") and \
            exe.parent.parent.name.lower() == "binaries":
        root = exe.parent.parent.parent
        if (root.parent / "Engine").is_dir():
            root = root.parent
    # "Game/Game/game.exe" where the outer folder holds nothing else.
    try:
        siblings = [p.name for p in root.parent.iterdir() if not p.name.startswith(".")]
        if root.parent.name == root.name and siblings == [root.name]:
            root = root.parent
    except OSError:
        pass
    return root


def tidy_name(text: str) -> str:
    text = re.sub(r"[-_]Win(64|32|GDK)[-_]Shipping$", "", text, flags=re.I)
    text = re.sub(r"[-_](Shipping|Win64|x64|DX11|DX12|Vulkan)$", "", text, flags=re.I)
    text = text.replace("_", " ").strip()
    if text.islower() and " " not in text:
        text = text.upper() if len(text) <= 6 else text.capitalize()
    return text


def unity_ids(exe: Path) -> tuple[str, str] | None:
    info = exe.with_name(exe.stem + "_Data") / "app.info"
    try:
        lines = info.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
        return lines[0].strip(), lines[1].strip()
    return None


def detect_engine(exe: Path, root: Path, pe: PEInfo | None) -> str:
    here = exe.parent
    if (here / "UnityPlayer.dll").exists() or exe.with_name(exe.stem + "_Data").is_dir():
        return "Unity"
    if "binaries" in [p.name.lower() for p in exe.parents] or (root / "Engine").is_dir():
        return "Unreal Engine"
    if pe and pe.is_dotnet:
        if any(here.glob("Microsoft.Xna.Framework*.dll")):
            return "XNA (.NET)"
        if (here / "FNA.dll").exists():
            return "FNA (.NET)"
        return ".NET"
    if any(here.glob("*.pck")) or exe.with_suffix(".pck").exists():
        return "Godot"
    if (here / "nw.dll").exists() or (here / "resources.pak").exists() and (here / "locales").is_dir():
        return "NW.js / Electron"
    if (here / "Data").is_dir() and any((here / "Data").glob("*.bsa")):
        return "Bethesda (Gamebryo/Creation)"
    if any(here.glob("*.forge")):
        return "Ubisoft AnvilNext"
    return "Unknown"


_BUILTIN_CACHE: set[str] | None = None


def builtin_dlls() -> set[str]:
    """Lower-case names of the DLLs Wine/Proton provide, from any Proton (or Wine) found."""
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is None:
        dirs = [Path("/usr/lib/wine/x86_64-windows"), Path("/usr/lib/wine/i386-windows"),
                Path("/usr/lib32/wine/i386-windows")]
        try:
            from . import steam
            for p in steam.protons():
                dirs += [p.path / "files/lib/wine/x86_64-windows", p.path / "files/lib/wine/i386-windows",
                         p.path / "files/lib64/wine/x86_64-windows", p.path / "files/lib/wine/dxvk",
                         p.path / "files/lib/wine/vkd3d-proton"]
        except Exception:  # noqa: BLE001 - no Steam is fine, fall back to system Wine
            pass
        names: set[str] = set()
        for d in dirs:
            try:
                names |= {f.name.lower() for f in d.iterdir()}
            except OSError:
                continue
        _BUILTIN_CACHE = names
    return _BUILTIN_CACHE


def missing_dlls(exe: Path, imports: list[str]) -> list[str]:
    """DLLs the .exe links against that are neither next to it nor provided by Wine.

    A non-empty result almost always means the game folder is incomplete (an
    archive still extracting, a failed copy, an antivirus quarantine).
    """
    builtin = builtin_dlls()
    if not builtin:
        return []
    try:
        local = {p.name.lower() for p in exe.parent.iterdir()}
    except OSError:
        return []
    out = []
    for name in imports:
        low = name.lower()
        if low in local or low in builtin or low.startswith(("api-ms-win-", "ext-ms-")):
            continue
        out.append(name)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_saves(a: Analysis, search_roots: list[Path]) -> list[SaveCandidate]:
    """Folders in old Windows backups / ~/Documents that look like this game's saves."""
    keys = {_norm(a.name), _norm(a.exe.stem), _norm(a.root.name)}
    if a.pe and a.pe.version.get("ProductName"):
        keys.add(_norm(a.pe.version["ProductName"]))
    keys = {k for k in keys if len(k) >= 3}
    exact = {_norm(p.stem) for p in a.other_exes[:3]} - {""}
    found: list[SaveCandidate] = []
    seen: set[Path] = set()

    generic = {"mygames", "mymusic", "mypictures", "myvideos", "savedgames", "documents", "microsoft",
               "windows", "temp", "packages", "programs", "games", "steam", "nvidia", "amd", "intel"}

    def matches(name: str) -> bool:
        n = _norm(name)
        if n in generic:
            return False
        return bool(n) and (n in exact or any(n == k or (len(n) >= 5 and (n in k or k in n)) for k in keys))

    # Relative layouts (on Windows, under C:\Users\<you>\) worth checking.
    layouts = [
        ("Documents/My Games", "Documents/My Games"),
        ("Documents", "Documents"),
        ("Saved Games", "Saved Games"),
        ("AppData/Local", "AppData/Local"),
        ("AppData/Roaming", "AppData/Roaming"),
    ]
    for base in search_roots:
        for rel, dest in layouts:
            d = base / rel
            if not d.is_dir():
                continue
            try:
                children = list(d.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir() and matches(child.name) and child.resolve() not in seen:
                    seen.add(child.resolve())
                    found.append(SaveCandidate(child, f"{dest}/{child.name}", f"Found in {d}"))
        if a.unity_ids:
            company, product = a.unity_ids
            d = base / "AppData/LocalLow" / company / product
            if d.is_dir() and d.resolve() not in seen:
                seen.add(d.resolve())
                found.append(SaveCandidate(d, f"AppData/LocalLow/{company}/{product}", "Unity save folder"))
    return found


def default_search_roots() -> list[Path]:
    home = Path.home()
    roots = [home]
    for extra in ("Old Windows", "Windows", "WindowsBackup", "Storage/Windows"):
        p = home / extra
        if p.is_dir():
            roots.append(p)
            users = p / "Users"
            if users.is_dir():
                roots += [u for u in users.iterdir() if u.is_dir() and u.name.lower() not in ("public", "default", "all users")]
    return roots


def analyze(exe: Path, search_roots: list[Path] | None = None) -> Analysis:
    exe = Path(exe).expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    root = game_root(exe) if exe.suffix.lower() == ".exe" else exe.parent
    a = Analysis(exe=exe, root=root, name=tidy_name(root.name))

    if exe.suffix.lower() in (".bat", ".cmd"):
        a.engine = "Batch launcher"
        text = exe.read_text(errors="replace").lower()
        if re.search(r"^\s*pause\b", text, re.M) or "taskkill" in text:
            a.bat_needs_copy = True
            a.warnings.append(
                "This .bat waits for a key press and/or kills the game. Under Proton there is no "
                "console to press a key in, so it would close the game at once. A Linux copy "
                "without those lines will be made and used instead.")
    else:
        try:
            a.pe = PEFile(exe).info()
        except (PEError, OSError) as error:
            a.warnings.append(f"Couldn't read the .exe ({error}). It may not be a Windows program.")
        if a.pe:
            gone = missing_dlls(exe, a.pe.imports)
            if gone:
                a.warnings.append(
                    f"The game folder looks incomplete: {', '.join(gone)} is missing next to the .exe. "
                    "If you're still extracting or copying it, wait until that finishes. Otherwise "
                    "extract it again (an antivirus may have removed files).")
        a.engine = detect_engine(exe, root, a.pe)
        a.unity_ids = unity_ids(exe)
        product = (a.pe.version.get("ProductName") if a.pe else "") or ""
        if a.unity_ids:
            a.name = a.unity_ids[1]
        elif a.name.lower() in ("", "game", "games", "bin"):
            a.name = product if product and len(product) < 60 else tidy_name(exe.stem)

    if NOT_THE_GAME.search(exe.stem):
        a.warnings.append(f"“{exe.name}” looks like a helper, not the game. Pick the game's own .exe.")

    # Other candidate executables, biggest first, to help pick the right one.
    try:
        exes = [p for p in root.rglob("*.exe") if p != exe and not NOT_THE_GAME.search(p.stem)]
        exes.sort(key=lambda p: p.stat().st_size, reverse=True)
        a.other_exes = exes[:6]
    except OSError:
        pass

    # DLLs next to the .exe that shadow system DLLs.
    here = exe.parent
    try:
        local = {p.stem.lower(): p for p in here.iterdir() if p.suffix.lower() == ".dll"}
    except OSError:
        local = {}
    for stem in sorted(local):
        if stem in PROXY_DLLS and PROXY_DLLS[stem]:
            a.dlls.append(DllChoice(stem, PROXY_DLLS[stem], True))
        elif stem in SOMETIMES_NATIVE:
            a.dlls.append(DllChoice(stem, SOMETIMES_NATIVE[stem], False))

    # Notes and known trouble.
    names = {p.name.lower() for p in here.iterdir()} if here.is_dir() else set()
    root_names = {p.name.lower() for p in root.iterdir()} if root.is_dir() else set()
    if "doorstop_config.ini" in names or "bepinex" in names:
        a.notes.append("BepInEx mod loader found" + (": its winhttp.dll override is turned on." if "winhttp.dll" in names else "."))
    if {"easyanticheat", "easyanticheat_eos"} & (names | root_names):
        a.warnings.append("EasyAntiCheat found. Online play only works if the developer enabled Proton support.")
    if "battleye" in names | root_names:
        a.warnings.append("BattlEye found. Online play only works if the developer enabled Proton support.")
    if any(n.startswith(("vcredist", "vc_redist")) for n in names | root_names) or \
            {"_commonredist", "_redist", "redist"} & root_names:
        a.notes.append("Ships Visual C++ redistributables. Proton has its own; if the game still "
                       "complains about MSVCP/VCRUNTIME, run: protontricks <appid> vcrun2022")
    if a.engine.startswith("XNA"):
        a.notes.append("XNA game: if it won't start, run: protontricks <appid> xna40")
    elif a.engine == ".NET":
        a.notes.append(".NET game: Proton's Wine Mono usually works; if not, run: protontricks <appid> dotnet48")
    if "onlinefix64.dll" in names or "onlinefix.ini" in names:
        a.notes.append("OnlineFix build: Steam has to be running and logged in while you play "
                       "(it goes through Steam's Spacewar app for online play).")
    if "galaxy.dll" in names or "galaxy64.dll" in names:
        a.notes.append("GOG build (Galaxy DLL present).")
    if "steam_api.dll" in names or "steam_api64.dll" in names:
        a.notes.append("Uses the Steam API: keep Steam running while playing.")
    if a.pe and not a.pe.is_64bit:
        a.notes.append("32-bit game.")

    a.saves = find_saves(a, search_roots if search_roots is not None else default_search_roots())
    return a


def overrides_string(dlls: list[DllChoice]) -> str:
    on = [d.name for d in dlls if d.enabled]
    return ";".join(f"{n}=n,b" for n in on)


# ------------------------------------------------------------- folder scanning

SKIP_DIRS = re.compile(r"^(common files|microsoft.*|windows.*|internet explorer|reference assemblies|"
                       r"msbuild|dotnet|nvidia.*|amd|intel|realtek|steam|epic games|gog galaxy|"
                       r"ubisoft|ea games|origin|battle\.net|riot games|\$.*|\..*)$", re.I)


def best_exe(folder: Path, max_depth: int = 4) -> Path | None:
    """The most likely game executable under a folder."""
    want = _norm(folder.name)
    best: tuple[float, Path] | None = None
    base = len(folder.parts)
    try:
        candidates = [p for p in folder.rglob("*") if p.suffix.lower() == ".exe" and len(p.parts) - base <= max_depth]
    except OSError:
        return None
    for p in candidates:
        if NOT_THE_GAME.search(p.stem) or not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        stem = _norm(p.stem)
        score = min(size / 2**20, 200)  # MB, capped
        if stem and (stem == want or (len(stem) >= 4 and (stem in want or want in stem))):
            score += 300
        if "shipping" in stem:
            score += 150
        if re.fullmatch(r"(nv|f4|sk|ob|fo)se_?loader", stem):
            score += 400  # script extender: starts the game with mods
        elif "launcher" in stem or "loader" in stem:
            score -= 50
        if (p.parent / "UnityPlayer.dll").exists() or p.with_name(p.stem + "_Data").is_dir():
            score += 200
        score -= 5 * (len(p.parts) - base)
        if best is None or score > best[0]:
            best = (score, p)
    return best[1] if best else None


def scan_folder(folder: Path, search_roots: list[Path] | None = None) -> list[tuple[Analysis, bool]]:
    """Each sub-folder that holds a Windows program, with a guess whether it is a game."""
    out = []
    try:
        children = sorted((c for c in Path(folder).iterdir() if c.is_dir() and not SKIP_DIRS.match(c.name)),
                          key=lambda c: c.name.lower())
    except OSError:
        return out
    for child in children:
        exe = best_exe(child)
        if exe is None:
            continue
        try:
            a = analyze(exe, search_roots)
        except (OSError, ValueError):
            continue
        here = {p.name.lower() for p in exe.parent.iterdir()}
        a.root = child
        if not a.unity_ids:
            a.name = tidy_name(child.name)
        likely = (a.engine not in ("Unknown", ".NET", "NW.js / Electron") or any(d.enabled for d in a.dlls)
                  or bool({"steam_api.dll", "steam_api64.dll", "galaxy.dll", "galaxy64.dll"} & here)
                  or any(n.endswith((".pak", ".forge", ".bsa", ".ba2", ".pck", ".assets")) for n in here))
        out.append((a, likely))
    return out
