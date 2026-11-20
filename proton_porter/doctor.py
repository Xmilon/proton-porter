""""Fix it": test a game, try the fixes its log points to, keep the first one that works.

Order: run it as it is; then the specific fixes the diagnosis suggests
(protontricks components, WineD3D for graphics crashes), each followed by a
retest; then other Proton versions, a fresh prefix and the system's Wine
(retry.py). Something that works is saved: protontricks installs stay in the
prefix, variables go into the game's launch options, Proton choices into Steam.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from . import retry, runner, runners, steam
from .runner import Progress, RunResult

VERB = re.compile(r"protontricks \S+ (\w+)")


@dataclass
class Step:
    what: str
    result: RunResult | None = None
    kept: bool = False


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    fixed: bool = False
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        for s in self.steps:
            mark = "✓" if s.result and s.result.survived else "✗"
            out.append(f"{mark} {s.what}" + (f": {s.result.line()}" if s.result else ""))
            if s.result and not s.result.survived:
                out += [f"    {x}" for x in s.result.diagnosis.summary()[:2]]
        return out + self.notes


@dataclass
class Fix:
    what: str
    verb: str = ""  # protontricks component
    env: dict[str, str] = field(default_factory=dict)  # launch-option variables


def fixes_for(result: RunResult) -> list[Fix]:
    """What the diagnosis says is worth trying, most specific first."""
    out: list[Fix] = []
    seen = set()
    for f in result.diagnosis.findings:
        m = VERB.search(f.fix or "")
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(Fix(f"Install {m.group(1)} with protontricks", verb=m.group(1)))
    graphics = any(f.kind == "graphics" or (f.kind == "crash" and f.module.lower().startswith(("d3d", "dxgi", "dxvk")))
                   for f in result.diagnosis.findings)
    if graphics:
        out.append(Fix("Use WineD3D instead of DXVK", env={"PROTON_USE_WINED3D": "1"}))
    return out


def _protontricks(appid: int, verb: str) -> bool:
    if not shutil.which("protontricks"):
        return False
    res = subprocess.run(["protontricks", str(appid), verb], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, timeout=1800, check=False)
    return res.returncode == 0


def _save_env(user: steam.SteamUser, entry: steam.LibraryEntry, env: dict[str, str]) -> None:
    words = " ".join(f"{k}={v}" for k, v in env.items())
    opts = entry.launch_options or ""
    if "%command%" in opts:
        opts = f"{words} {opts}"
    elif opts:
        raise RuntimeError(f"Couldn't save {words}: this game's arguments don't use %command%.")
    else:
        opts = f"{words} %command%"
    was = steam.steam_running()
    if was:
        steam.close_steam()
    try:
        steam.backup(user, time.strftime("%Y%m%d-%H%M%S"))
        steam.set_launch_options(user, entry.appid, opts)
    finally:
        if was:
            steam.open_steam()


def fix_it(entry: steam.LibraryEntry, user: steam.SteamUser, workspace: int | None, seconds: float = 60,
           progress: Progress = lambda *_: None, run=runner.run_test) -> Report:
    rep = Report()
    tools = runners.all_runners()
    spec = runner.spec_for(entry, tools)
    first = Step("As it is now")
    progress("Testing it as it is", None)
    first.result = run(spec, workspace, seconds, progress)
    rep.steps.append(first)
    if first.result.survived:
        rep.fixed = True
        rep.notes.append("It runs in a test. If it only crashes from Steam, turn on “Log Steam launches”, "
                         "play until it crashes, then look at “Latest log”.")
        return rep

    for fix in fixes_for(first.result):
        step = Step(fix.what)
        rep.steps.append(step)
        if fix.verb:
            if runners.is_pseudo(spec.proton):
                rep.notes.append(f"Skipped {fix.verb}: protontricks only works with Proton.")
                continue
            progress(f"{fix.what} (a window may open)", None)
            if not _protontricks(entry.appid, fix.verb):
                rep.notes.append(f"protontricks couldn't install {fix.verb}" + (
                    "" if shutil.which("protontricks") else " (protontricks isn't installed)") + ".")
                continue
            step.kept = True  # it's in the prefix now either way
        spec.extra_env.update(fix.env)
        progress(f"Testing with: {fix.what}", None)
        step.result = run(spec, workspace, seconds, progress)
        if step.result.survived:
            if fix.env:
                _save_env(user, entry, fix.env)
                step.kept = True
            rep.fixed = True
            rep.notes.append(f"Fixed by: {fix.what}.")
            return rep
        for k in fix.env:
            spec.extra_env.pop(k, None)

    if entry.tool == runners.NATIVE:
        rep.notes.append("It's a Linux program, so there are no Proton versions to try.")
        return rep
    progress("Trying other Proton versions and the system's Wine", None)
    r = retry.retry(entry, user, tools, workspace, seconds, 6, True, progress, runner=run)  # type: ignore[arg-type]
    for att in r.attempts:
        rep.steps.append(Step(att.proton.display + (" (fresh prefix)" if att.fresh_prefix else ""), att.result))
    rep.notes += r.notes
    rep.fixed = r.winner is not None
    if not rep.fixed:
        rep.notes.append("Nothing worked. “Latest log” shows the details; the Compatibility guide (About menu) "
                         "lists builds that can't work under Wine.")
    return rep
