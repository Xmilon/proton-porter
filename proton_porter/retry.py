"""Try a game with other Proton versions and a fresh prefix; keep what survives."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import knowledge, steam
from .runner import Progress, RunResult, RunSpec, run_test


@dataclass
class Attempt:
    proton: steam.Proton
    fresh_prefix: bool
    result: RunResult | None = None


@dataclass
class RetryReport:
    attempts: list[Attempt] = field(default_factory=list)
    winner: Attempt | None = None
    kept: bool = False  # the winning setup was written to Steam
    notes: list[str] = field(default_factory=list)


def plan(current: steam.Proton, tools: list[steam.Proton], hint: str = "", limit: int = 6) -> list[Attempt]:
    """Current setup first, then the same Proton on a fresh prefix, then the others on fresh prefixes."""
    def rank(t: steam.Proton) -> int:
        n = t.internal.lower()
        if hint and (t.internal == hint or hint.lower() in n):
            return 0
        if n.startswith(("proton-ge", "ge-proton")):
            return 1
        if n == "proton_experimental":
            return 2
        if n == "proton_hotfix":
            return 3
        return 4
    others = sorted((t for t in tools if t.internal != current.internal), key=lambda t: (rank(t), t.internal))
    out = [Attempt(current, False), Attempt(current, True)]
    out += [Attempt(t, True) for t in others]
    if hint:  # a known-good Proton goes first
        out.sort(key=lambda a: 0 if rank(a.proton) == 0 and a.proton.internal != current.internal else 1)
    return out[:limit]


def fresh_prefix_path(appid: int) -> Path:
    return steam.prefix_dir(appid).with_name(f"{appid}.try")


def swap_in_prefix(appid: int, trial: Path, stamp: str) -> Path | None:
    """Make a surviving trial prefix the real one; the old one is kept, not deleted."""
    real = steam.prefix_dir(appid)
    old = None
    if real.exists():
        old = real.with_name(f"{appid}.pre-retry-{stamp}")
        real.rename(old)
    trial.rename(real)
    return old


def retry(entry: steam.LibraryEntry, user: steam.SteamUser, tools: list[steam.Proton], workspace: int,
          seconds: float = 60, limit: int = 6, keep: bool = True, progress: Progress = lambda *_: None,
          runner=run_test) -> RetryReport:
    """Run attempts until one survives `seconds`. With keep=True the winner is saved:
    Steam is closed, the Proton choice (and prefix) is switched, Steam is reopened.
    """
    report = RetryReport()
    if steam.game_running():
        raise RuntimeError("A Steam game is running. Close it first.")
    if not tools:
        raise RuntimeError("No Proton found")
    from . import runners
    if entry.tool == runners.NATIVE:
        raise RuntimeError(f"{entry.name} is a Linux program; there are no Proton versions to try.")
    tools = [t for t in tools if not runners.is_pseudo(t)]
    current = next((t for t in tools if t.internal == entry.tool), None) or steam.default_proton(tools)
    hint = ""
    try:
        mods = knowledge.detect(entry.exe.parent, entry.start_dir)
        hint = next((m.proton for m in mods if m.proton), "")
    except OSError:
        pass
    attempts = plan(current, tools, hint, limit)
    wine = runners.pseudo(runners.WINE)
    if wine and entry.tool != runners.WINE:  # last resort: the system's Wine, not Proton
        attempts = attempts[:max(limit - 1, 1)] + [Attempt(wine, False)]
    for i, att in enumerate(attempts, 1):
        label = f"Attempt {i}: {att.proton.display}" + (
            "" if runners.is_pseudo(att.proton) else ", fresh prefix" if att.fresh_prefix else ", current prefix")
        progress(label, None)
        trial = fresh_prefix_path(entry.appid)
        if att.fresh_prefix:
            shutil.rmtree(trial, ignore_errors=True)
        spec = RunSpec(entry.appid, entry.exe, entry.start_dir, att.proton, entry.launch_options,
                       trial if att.fresh_prefix else None)
        try:
            att.result = runner(spec, workspace, seconds, lambda msg, frac, l=label: progress(f"{l}: {msg}", frac))
        except RuntimeError as error:  # this attempt couldn't start; try the next
            from .diagnose import Diagnosis, Finding
            from .runner import RunResult
            att.result = RunResult(False, 0, None, None, Diagnosis([Finding("crash", str(error))]),
                                   att.proton.display, spec.prefix)
        report.attempts.append(att)
        if att.result.survived:
            report.winner = att
            break
        if att.fresh_prefix:
            shutil.rmtree(trial, ignore_errors=True)  # a failed trial prefix is worthless
    if report.winner is None:
        report.notes.append("No Proton version survived. The logs are in ~/.local/state/proton-porter/logs.")
        return report
    if keep:
        _keep(entry, user, report, progress)
    return report


def _keep(entry: steam.LibraryEntry, user: steam.SteamUser, report: RetryReport, progress: Progress) -> None:
    win = report.winner
    assert win is not None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    was_running = steam.steam_running()
    if was_running:
        progress("Closing Steam", None)
        steam.close_steam()
    try:
        steam.backup(user, stamp)
        if win.proton.internal != entry.tool:
            from .apply import switch_runner
            switch_runner(user, entry, win.proton)
            report.notes.append(f"Now runs with {win.proton.display}.")
        if win.fresh_prefix:
            old = swap_in_prefix(entry.appid, fresh_prefix_path(entry.appid), stamp)
            report.notes.append("Using a fresh prefix." + (f" The old one is kept at {old}." if old else ""))
        report.kept = True
    finally:
        if was_running:
            progress("Reopening Steam", None)
            steam.open_steam()
