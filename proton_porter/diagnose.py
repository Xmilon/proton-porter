"""Read a Proton/Wine log (PROTON_LOG=1) and say what went wrong and what to try."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# DLL name pattern -> (what it is, what to try)
DLL_ADVICE = [
    (r"^(msvcp|vcruntime|concrt|vcomp|mfc)\d*", "Visual C++ runtime", "protontricks {appid} vcrun2022"),
    (r"^d3dx9_|^d3dx10|^d3dx11|^d3dcompiler_4", "DirectX 9/10/11 helper", "protontricks {appid} d3dx9 (or d3dcompiler_43)"),
    (r"^xaudio|^x3daudio|^xapofx|^xactengine", "DirectX audio", "protontricks {appid} xact"),
    (r"^d3dcompiler_47", "shader compiler", "protontricks {appid} d3dcompiler_47"),
    (r"^(mfplat|mf|mfreadwrite|msmpeg2|wmvcore)", "Media Foundation (video)", "use Proton-GE, which ships the codecs"),
    (r"^(mscoree|mscorlib|clr|netfx)", ".NET runtime", "protontricks {appid} dotnet48"),
    (r"^(steam_api|steamclient|galaxy)", "store client DLL", "the game's own copy is missing: re-extract the game"),
    (r"^(unityplayer|gameassembly|mono)", "Unity file", "the game folder is incomplete: re-extract it"),
    (r"^(physx|nvcuda|nvapi|nvngx)", "NVIDIA library", "set PROTON_ENABLE_NVAPI=1, or use Proton-GE"),
    (r"^(vulkan|dxvk|d3d1[01]core|dxgi)", "graphics layer", "check Vulkan works (`vulkaninfo`), or try PROTON_USE_WINED3D=1"),
]


@dataclass
class Finding:
    kind: str  # "missing-dll", "crash", "graphics", "anticheat", "info"
    message: str
    module: str = ""
    fix: str = ""


@dataclass
class Diagnosis:
    findings: list[Finding] = field(default_factory=list)
    crashed: bool = False

    @property
    def crash_module(self) -> str:
        for f in self.findings:
            if f.kind == "crash" and f.module:
                return f.module
        return ""

    @property
    def game_log_errors(self) -> bool:
        return any(f.kind == "dotnet" for f in self.findings)

    def summary(self) -> list[str]:
        if not self.findings:
            return ["No crash or missing DLL found in the log."]
        out = []
        for f in self.findings:
            out.append(f.message + (f"  ->  {f.fix}" if f.fix else ""))
        return out


_IMPORT = re.compile(r'err:module:import_dll\s+Library\s+(\S+)\s+\(which is needed by\s+"([^"]+)"\)\s+not found', re.I)
_IMPORT_FAIL = re.compile(r"err:module:LdrInitializeThunk\s+Importing dlls for '([^']+)' failed, status ([0-9a-fx]+)", re.I)
_EXC = re.compile(r"(?:wine: )?(Unhandled (?:exception|page fault)[^\n]*)", re.I)
_MOD_IN_LINE = re.compile(r"([\w.\-]+\.(?:exe|dll|drv|sys))\+0x[0-9a-f]+", re.I)
_SEH_EXC = re.compile(r"seh:dispatch_exception backtrace: --- Exception (0x[0-9a-f]+)", re.I)
_SEH_FRAME = re.compile(r'seh:virtual_unwind backtrace: [0-9a-f]+: L"([^"]+)"\s*\+\s*([0-9a-f]+)', re.I)
EXC_NAMES = {"0xc0000005": "access violation", "0xc000001d": "illegal instruction", "0xc0000094": "divide by zero",
             "0xc00000fd": "stack overflow", "0xc0000409": "stack buffer overrun", "0xc0000374": "heap corruption",
             "0xc0000135": "DLL not found", "0xc0000142": "DLL init failed"}
_MOD_PATH = re.compile(r"\([A-Za-z]:\\[^)]*?([\w.\-]+\.(?:exe|dll))\)", re.I)
_MOD_IN = re.compile(r"\bin\s+([\w.\-]+)\s+\(", re.I)
_LOAD_FAIL = re.compile(r"(?:err:module:\w+|wine): (?:could not load|Library|failed to load)\s+([^\s,;:]+)", re.I)
_VULKAN = re.compile(r"(Could not load vulkan|vkCreateInstance failed|No Vulkan|failed to create (?:d3d|vulkan) device|"
                     r"DXVK: .*Failed to create|VK_ERROR_\w+)", re.I)
_EAC = re.compile(r"(EasyAntiCheat|EOS|BEService|BattlEye)[^\n]*(fail|error|not (?:found|running))", re.I)

SYSTEM_MODULES = {"ntdll.dll", "kernel32.dll", "kernelbase.dll", "win32u.dll", "user32.dll", "ucrtbase.dll"}


def advice_for_dll(name: str, appid: int | str = "<appid>") -> tuple[str, str]:
    base = name.lower().removesuffix(".dll")
    for pattern, what, fix in DLL_ADVICE:
        if re.search(pattern, base):
            return what, fix.format(appid=appid)
    return "", "copy it next to the game's .exe if the game ships it, or re-extract the game"


def parse_log(text: str, appid: int | str = "<appid>") -> Diagnosis:
    d = Diagnosis()
    seen: set[tuple[str, str]] = set()

    def add(kind, message, module="", fix=""):
        key = (kind, module or message)
        if key not in seen:
            seen.add(key)
            d.findings.append(Finding(kind, message, module, fix))

    for m in _IMPORT.finditer(text):
        dll, needed_by = m.group(1), m.group(2)
        what, fix = advice_for_dll(dll, appid)
        add("missing-dll", f"{dll} is missing (needed by {Path(needed_by.replace(chr(92), '/')).name})"
            + (f", a {what}" if what else ""), dll, fix)
        d.crashed = True
    for m in _IMPORT_FAIL.finditer(text):
        add("crash", f"{Path(m.group(1).replace(chr(92), '/')).name} could not start: its DLL imports failed (status {m.group(2)})")
        d.crashed = True

    lines = text.splitlines()
    for i, line in enumerate(lines):
        exc = _EXC.search(line)
        if not exc:
            continue
        d.crashed = True
        mods = []
        for after in lines[i + 1:i + 40]:
            mod = _MOD_IN_LINE.search(after) or _MOD_IN.search(after) or _MOD_PATH.search(after)
            if mod and mod.group(1).lower() not in SYSTEM_MODULES and mod.group(1) not in mods:
                mods.append(mod.group(1))
        known = [m for m in mods if _known_mod(m)]
        module = (known or mods or [""])[0]
        what = re.split(r"[,(]", exc.group(1))[0].strip()
        message = f"Crash: {what}" + (f", in {module}" if module else "")
        add("crash", message, module, crash_fix(module))
        break  # the first crash is the cause; later ones are fallout

    _hard_error_popup(lines, add, d)
    if not d.crash_module:
        _last_seh_exception(lines, add)

    for m in _LOAD_FAIL.finditer(text):
        name = m.group(1).strip("\"'")
        if name.lower().endswith(".dll") and not any(f.module.lower() == name.lower() for f in d.findings):
            what, fix = advice_for_dll(name, appid)
            add("missing-dll", f"{name} could not be loaded" + (f" ({what})" if what else ""), name, fix)

    if _VULKAN.search(text):
        add("graphics", "Vulkan / graphics device problem: " + _VULKAN.search(text).group(1),
            fix="check `vulkaninfo`, update the GPU driver, or try PROTON_USE_WINED3D=1")
    if _EAC.search(text):
        add("anticheat", "Anti-cheat failed to start", fix="online play needs developer-enabled Proton support; offline/cracked builds may need the anti-cheat files removed")
    return d


_NATIVE_LOAD = re.compile(r'loaddll:build_module Loaded L"([^"]+)" at \S+: native', re.I)


def _hard_error_popup(lines: list[str], add, d: "Diagnosis") -> None:
    """NtRaiseHardError = a DLL put up a Windows error box (often while loading). The game is
    stuck behind it, so a run that 'stays up' with this is not a success."""
    recent: list[str] = []
    for line in lines:
        m = _NATIVE_LOAD.search(line)
        if m and not m.group(1).lower().startswith("c:\\windows"):
            recent = (recent + [m.group(1).replace("\\\\", "\\").split("\\")[-1]])[-3:]
        if "NtRaiseHardError" in line:
            known = [n for n in recent if _known_mod(n)]
            last_native = (known or recent[-1:] or [""])[-1]
            who = f" right after loading {last_native}" if last_native else ""
            fix = "look at the popup's text in the game's window"
            if _known_mod(last_native):
                fix = (f"{last_native} is a crack/loader DLL refusing to start; read its popup "
                       "(often: Steam not running, wrong app id, or files changed)")
            add("crash", f"An error box was shown{who}; the game is stuck behind it", last_native, fix)
            d.crashed = True
            return


def _last_seh_exception(lines: list[str], add) -> None:
    """Proton logs with +seh: the last fatal-class exception that has a stack is the likely crash.

    Not marked as `crashed`: a game can survive handled exceptions. The runner
    knows if the process really died.
    """
    last = None  # (code, [(module, offset), ...])
    cur = None
    for line in lines:
        exc = _SEH_EXC.search(line)
        if exc:
            cur = (exc.group(1).lower(), [])
            continue
        frame = _SEH_FRAME.search(line)
        if frame and cur:
            cur[1].append((frame.group(1), frame.group(2).lstrip("0") or "0"))
            if cur[0].startswith("0xc") and last is not cur:
                last = cur
    if not last:
        return
    code, frames = last
    useful = [(m, o) for m, o in frames if m.lower() not in SYSTEM_MODULES]
    if not useful:
        return
    known = [f for f in useful if _known_mod(f[0])]
    module, offset = (known or useful)[0]
    chain = ", ".join(f"{m}+0x{o}" for m, o in useful[:4])
    add("crash", f"Last fatal exception {code} ({EXC_NAMES.get(code, 'error')}) in {module}+0x{offset}; stack: {chain}",
        module, crash_fix(module))


def _known_mod(module: str) -> bool:
    from . import knowledge
    low = module.lower()
    return any(p.lower() == low for e in knowledge.load() for p in e.get("detect", []))


def crash_fix(module: str) -> str:
    from . import knowledge
    low = module.lower()
    if not low:
        return "try another Proton version (Proton-GE, Experimental), or a fresh prefix"
    for e in knowledge.load():
        for pattern in e.get("detect", []):
            if pattern.lower() == low:
                tip = f"{e['name']} is involved"
                if e.get("proton"):
                    tip += f"; it is known to need {e['proton']}"
                return tip
    if low.endswith(".exe"):
        return "the game itself crashed: try another Proton version, a fresh prefix, or PROTON_USE_WINED3D=1"
    if low.startswith(("d3d", "dxgi", "dxvk", "vulkan", "nvapi")):
        return "graphics crash: try PROTON_USE_WINED3D=1 or another Proton version"
    return f"{module} is a mod/loader DLL: check its WINEDLLOVERRIDES entry, or try another Proton version"


def parse_file(path: Path, appid: int | str = "<appid>") -> Diagnosis:
    return parse_log(Path(path).read_text(errors="replace"), appid)


def find_log(folder: Path, appid: int) -> Path | None:
    for name in (f"steam-{appid}.log", "steam-0.log"):
        p = folder / name
        if p.is_file():
            return p
    logs = sorted(folder.glob("steam-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


# Logs the game writes itself next to the .exe or in its Unity/XNA output folders.
GAME_LOG_NAMES = ("client-crashlog.txt", "crashlog.txt", "crash.log", "error.log", "output_log.txt",
                  "Player.log", "log.txt")
_DOTNET_DLL = re.compile(r"System\.DllNotFoundException:\s*([^\s]+)")
_DOTNET_EXC = re.compile(r"^((?:Unhandled Exception:\s*)?System\.[\w.]+Exception):?\s*(.*)$", re.M)


def parse_game_logs(folder: Path, since: float = 0.0) -> list[Finding]:
    """Findings from the game's own crash logs written after `since` (a timestamp)."""
    out: list[Finding] = []
    for name in GAME_LOG_NAMES:
        p = Path(folder) / name
        try:
            if not p.is_file() or p.stat().st_mtime < since:
                continue
            text = p.read_text(errors="replace")[-20000:]
        except OSError:
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
        m = _DOTNET_DLL.findall(text)
        if m:
            dll = m[-1]
            fix = ("the DLL is next to the .exe but Mono couldn't load it: a DLL it depends on is missing, or "
                   "Steam's launch differs from a plain Proton run. Try another Proton, a fresh prefix, or "
                   "protontricks <appid> vcrun2022")
            out.append(Finding("dotnet", f"{p.name} ({when}): the game couldn't load native library “{dll}”", dll, fix))
            continue
        e = _DOTNET_EXC.findall(text)
        if e:
            kind, msg = e[-1]
            out.append(Finding("dotnet", f"{p.name} ({when}): {kind} {msg[:100]}", "", "try protontricks <appid> dotnet48, or another Proton"))
    return out
