"""Command line: `proton-porter analyze GAME.exe` / `proton-porter add GAME.exe`."""

from __future__ import annotations

import argparse
import time
import sys
from pathlib import Path

from . import __version__, steam
from .analyze import analyze, overrides_string
from .apply import Choices, apply, describe, parse_env
from .config import Settings


def _print_analysis(a) -> None:
    print(f"Game:    {a.name}")
    print(f"Engine:  {a.engine}")
    print(f"Folder:  {a.root}")
    print(f"Exe:     {a.exe}")
    for d in a.dlls:
        print(f"DLL:     {d.name}.dll  [{'on' if d.enabled else 'off'}]  {d.reason}")
    for s in a.saves:
        print(f"Saves:   {s.source}  ->  {s.dest}")
    for n in a.notes:
        print(f"Note:    {n}")
    for w in a.warnings:
        print(f"Warning: {w}")
    if a.other_exes:
        print("Other .exe files:", ", ".join(str(p.relative_to(a.root)) for p in a.other_exes))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and not argv[0].startswith("-") and Path(argv[0]).exists():
        from .gui import run  # `proton-porter game.exe` opens the window with it
        return run(["proton-porter", str(Path(argv[0]).resolve())])
    p = argparse.ArgumentParser(prog="proton-porter", description="Add Windows games to Steam with Proton, fixed up.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    an = sub.add_parser("analyze", help="show what would be detected")
    an.add_argument("exe", type=Path)
    an.add_argument("--search", action="append", type=Path, default=[])

    add = sub.add_parser("add", help="add a game to Steam")
    add.add_argument("exe", type=Path)
    add.add_argument("--name")
    add.add_argument("--move-to", type=Path, help="move the game's folder into this folder first")
    add.add_argument("--proton", help="internal name, e.g. Proton-GE, proton_experimental, system-wine or native")
    add.add_argument("--args", default="", help="arguments for the game")
    add.add_argument("--env", default="", help='extra variables, e.g. "DXVK_HUD=fps"')
    add.add_argument("--collection", default="Linux")
    add.add_argument("--dll", action="append", default=[], help="force a DLL override on (repeatable)")
    add.add_argument("--no-dll", action="append", default=[], help="turn a detected override off")
    add.add_argument("--no-saves", action="store_true", help="don't copy detected saves")
    add.add_argument("--search", action="append", type=Path, default=[],
                     help="extra folder to look for old saves in (a Windows backup or user folder)")
    add.add_argument("--no-restart", action="store_true", help="leave Steam closed afterwards")
    add.add_argument("--mangohud", action="store_true")
    add.add_argument("--gamemode", action="store_true")
    add.add_argument("--log", action="store_true", help="PROTON_LOG=1")
    add.add_argument("--artwork", action="store_true", help="fetch SteamGridDB artwork (key from Settings)")
    add.add_argument("--dry-run", action="store_true")

    sc = sub.add_parser("scan", help="list the games in a folder's sub-folders")
    sc.add_argument("folder", type=Path)
    sub.add_parser("list", help="list non-Steam games in the library")

    sub.add_parser("protons", help="list Proton versions Steam can use")
    sub.add_parser("mods", help="list the known cracks/mods and what each needs")

    dg = sub.add_parser("diagnose", help="read a Proton log: crash module, missing DLL, likely fix")
    dg.add_argument("target", help="a log file, or a game (name or appid) to read its latest log")

    ts = sub.add_parser("test", help="start a library game with PROTON_LOG=1, then diagnose the log")
    ts.add_argument("game", help="name or appid")
    ts.add_argument("--workspace", type=int, help="Hyprland workspace to start it on (default: settings, 7)")
    ts.add_argument("--seconds", type=int, help="how long it has to stay up (default: settings, 60)")

    rt = sub.add_parser("retry", help="try other Proton versions and a fresh prefix; keep what survives")
    rt.add_argument("game", help="name or appid")
    rt.add_argument("--workspace", type=int)
    rt.add_argument("--seconds", type=int)
    rt.add_argument("--limit", type=int, default=6, help="most attempts")
    rt.add_argument("--no-keep", action="store_true", help="only report, change nothing in Steam")

    ia = sub.add_parser("install-archive", help="extract a .rar/.7z/.zip/.tar.* game, verify it, optionally add it")
    ia.add_argument("archive", type=Path)
    ia.add_argument("--to", type=Path, help="games folder (default: settings)")
    ia.add_argument("--password", default="")
    ia.add_argument("--add", action="store_true", help="then add it to Steam")

    up = sub.add_parser("update", help="check GitHub main for a newer version and install it")
    up.add_argument("--check", action="store_true", help="only report")

    lg = sub.add_parser("logs", help="show the newest log for a game and what it says")
    lg.add_argument("game", help="name or appid")

    pl = sub.add_parser("play", help="run a library game with a Proton log; the log is kept if it crashes")
    pl.add_argument("game", help="name or appid")

    rp = sub.add_parser("install-repack", help="install a FitGirl/DODI repack folder (checks its MD5 list, runs its setup)")
    rp.add_argument("folder", type=Path)
    rp.add_argument("--to", type=Path, help="games folder (default: settings)")
    rp.add_argument("--no-check", action="store_true", help="skip the MD5 check")
    rp.add_argument("--add", action="store_true", help="then add it to Steam")

    sr = sub.add_parser("set-runner", help="switch a library game to another Proton, system-wine or native")
    sr.add_argument("game", help="name or appid")
    sr.add_argument("runner", help="internal name (see: proton-porter protons)")

    un = sub.add_parser("uninstall", help="uninstall a library game (shows the plan; --yes to do it)")
    un.add_argument("game", help="name or appid")
    un.add_argument("--remove-prefix", action="store_true", help="also move its Proton folder to the Trash")
    un.add_argument("--yes", action="store_true", help="really do it")

    ip = sub.add_parser("install-proton", help="install a custom Proton from an archive")
    ip.add_argument("archive", type=Path)

    a = p.parse_args(argv)
    if a.cmd is None:
        from .gui import run
        return run()
    if a.cmd == "scan":
        from .analyze import scan_folder
        for info, likely in scan_folder(a.folder, []):
            print(f"[{'x' if likely else ' '}] {info.name:30} {info.engine:24} {info.exe}")
        return 0
    if a.cmd == "list":
        for e in steam.library(steam.users()[0]):
            print(f"{e.appid:<11} {e.name:30} {e.tool or '-':20} {e.exe}")
        return 0
    if a.cmd in ("mods", "diagnose", "test", "retry", "install-archive", "install-proton", "update", "play", "logs", "install-repack", "uninstall", "set-runner"):
        return _extra(a)
    if a.cmd == "protons":
        from . import runners
        for t in runners.all_runners():
            print(f"{t.internal:24} {t.display:24} {t.path}")
        return 0

    from .analyze import default_search_roots
    info = analyze(a.exe, default_search_roots() + a.search)
    _print_analysis(info)
    if a.cmd == "analyze":
        return 0

    for d in info.dlls:
        if d.name in a.no_dll:
            d.enabled = False
    from .analyze import DllChoice
    for name in a.dll:
        name = name.lower().removesuffix(".dll")
        if not any(d.name == name for d in info.dlls):
            info.dlls.append(DllChoice(name, "forced", True))
        for d in info.dlls:
            if d.name == name:
                d.enabled = True
    from . import runners
    tools = runners.all_runners()
    if a.proton:
        tool = next((t for t in tools if t.internal == a.proton), None)
    elif info.native:
        tool = runners.pseudo(runners.NATIVE)
    else:
        tool = steam.default_proton(steam.protons())
    if tool is None:
        print("No Proton found. Install one from Steam (Library > Tools) first.", file=sys.stderr)
        return 1
    users = steam.users()
    if not users:
        print("No Steam user found. Log in to Steam once first.", file=sys.stderr)
        return 1
    c = Choices(name=a.name or info.name, proton=tool, user=users[0], move_to=a.move_to,
                args=a.args, extra_env=parse_env(a.env), collection=a.collection,
                saves=[] if a.no_saves else info.saves, restart_steam=not a.no_restart,
                mangohud=a.mangohud, gamemode=a.gamemode, proton_log=a.log,
                artwork_key=Settings.load().steamgriddb_key if a.artwork else "")
    print()
    print(describe(info, overrides_string(info.dlls), c))
    if a.dry_run:
        return 0
    r = apply(info, overrides_string(info.dlls), c, lambda msg, frac: print(f"  {msg}" + (f" {frac:.0%}" if frac else "")))
    print(f"\nDone: “{r.name}” (appid {r.appid}) {'updated' if r.updated else 'added'}. Backup: {r.backup}")
    for n in r.notes:
        print(f"  {n}")
    if not r.collection_ok:
        print("  Couldn't add it to the collection (Steam's collection file wasn't found).")
    return 0


def _progress(msg: str, frac: float | None) -> None:
    print(f"  {msg}" + (f" {frac:.0%}" if frac else ""), flush=True)


def _extra(a) -> int:
    from . import archive, diagnose, knowledge, protontools, retry, runner, update
    s = Settings.load()
    try:
        if a.cmd == "update":
            if a.check:
                newer = update.check()
                print(f"Update available: {newer} (you have {__version__})" if newer else f"Up to date ({__version__})")
                return 0
            new = update.update(_progress)
            print(f"Updated to {new}. Restart Proton Porter." if new else f"Up to date ({__version__})")
            return 0
        if a.cmd == "mods":
            for e in knowledge.load():
                print(f"{e['name']}\n  finds: {', '.join(e.get('detect', []))}")
                if e.get("overrides"):
                    print(f"  DLL overrides: {', '.join(e['overrides'])}")
                if e.get("proton"):
                    print(f"  Proton: {e['proton']}")
                for n in e.get("notes", []):
                    print(f"  {n}")
            return 0
        if a.cmd == "install-proton":
            t = protontools.install_proton(a.archive, _progress)
            print(f"Installed {t.display} ({t.internal}) in {t.path.parent}. Restart Steam to list it.")
            return 0
        if a.cmd == "install-repack":
            from . import repack
            r = repack.detect(a.folder)
            if r is None:
                print("That doesn't look like a repack (no setup.exe with .bin files).", file=sys.stderr)
                return 1
            tool = next((t for t in steam.protons() if t.internal == s.proton), None) or steam.default_proton(steam.protons())
            if tool is None:
                print("No Proton found.", file=sys.stderr)
                return 1
            print(f"{r.kind} repack: {r.name}")
            folder = repack.install(r, a.to or Path(s.games_dir).expanduser(), tool, _progress,
                                    s.repack_check_md5 and not a.no_check)
            print(f"Installed in {folder}")
            if a.add:
                from .analyze import best_exe
                exe = best_exe(folder)
                return main(["add", str(exe)]) if exe else 1
            return 0
        if a.cmd == "install-archive":
            folder = archive.install_game_archive(a.archive, a.to or Path(s.games_dir).expanduser(),
                                                  _progress, a.password)
            print(f"Extracted and verified: {folder}")
            if a.add:
                from .analyze import best_exe
                exe = best_exe(folder)
                if exe is None:
                    print("No game .exe found to add.", file=sys.stderr)
                    return 1
                return main(["add", str(exe)])
            return 0
        users = steam.users()
        if not users:
            print("No Steam user found. Log in to Steam once first.", file=sys.stderr)
            return 1
        if a.cmd == "diagnose":
            target = Path(a.target)
            appid: int | str = "<appid>"
            if not target.is_file():
                e = runner.find_entry(users[0], a.target)
                appid, target = e.appid, runner.latest_log(e.appid)
                if target is None:
                    print(f"No log for {e.name}. Run: proton-porter test {a.target}", file=sys.stderr)
                    return 1
            print(f"Log: {target}")
            for line in diagnose.parse_file(target, appid).summary():
                print(f"  {line}")
            return 0
        entry = runner.find_entry(users[0], a.game)
        from . import runners
        tools = runners.all_runners()
        workspace = getattr(a, "workspace", None)
        workspace = workspace if workspace is not None else s.test_workspace
        seconds = getattr(a, "seconds", None) or s.test_seconds
        if a.cmd == "test":
            r = runner.run_test(runner.spec_for(entry, tools), workspace, seconds, _progress)
            print(r.line())
            print(f"Log: {r.log}")
            for line in r.diagnosis.summary():
                print(f"  {line}")
            return 0 if r.survived else 2
        if a.cmd == "set-runner":
            tool = next((t for t in tools if t.internal == a.runner), None)
            if tool is None:
                print(f"Unknown runner “{a.runner}”. Have: {', '.join(t.internal for t in tools)}", file=sys.stderr)
                return 1
            from .apply import switch_runner
            was = steam.steam_running()
            if was:
                steam.close_steam()
            try:
                steam.backup(users[0], time.strftime("%Y%m%d-%H%M%S"))
                switch_runner(users[0], entry, tool)
            finally:
                if was:
                    steam.open_steam()
            print(f"{entry.name} now runs with {tool.display}.")
            return 0
        if a.cmd == "uninstall":
            from . import uninstall
            plan = uninstall.make_plan(entry, Path(s.games_dir).expanduser(), steam.library(users[0]))
            print(f"Uninstall {entry.name}:")
            print(plan.describe(a.remove_prefix))
            if not a.yes:
                print("\nNothing done. Add --yes to do it.")
                return 0
            tool = next((t for t in tools if t.internal == entry.tool), None) or steam.default_proton(tools)
            for n in uninstall.execute(plan, tool, a.remove_prefix, _progress):
                print(n)
            was = steam.steam_running()
            if was:
                steam.close_steam()
            try:
                steam.backup(users[0], time.strftime("%Y%m%d-%H%M%S"))
                steam.remove_shortcut(users[0], entry.appid)
            finally:
                if was:
                    steam.open_steam()
            print("Removed from Steam.")
            return 0
        if a.cmd == "logs":
            text, _folder = runner.latest_report(entry)
            print(text)
            return 0
        if a.cmd == "play":
            r = runner.play(runner.spec_for(entry, tools))
            if r.survived:
                print("Closed normally.")
                return 0
            print(f"{entry.name} crashed or failed (exit code {r.exit_code}). Log saved: {r.log}")
            for line in r.diagnosis.summary():
                print(f"  {line}")
            return 2
        if a.cmd == "retry":
            rep = retry.retry(entry, users[0], tools, workspace, seconds, a.limit, not a.no_keep, _progress)
            for att in rep.attempts:
                r = att.result
                print(f"{'OK  ' if r.survived else 'FAIL'} {r.line()}" + (" (fresh prefix)" if att.fresh_prefix else ""))
                if not r.survived:
                    for line in r.diagnosis.summary()[:2]:
                        print(f"       {line}")
            for n in rep.notes:
                print(n)
            if rep.winner:
                print(f"Winner: {rep.winner.proton.display}" + (" (saved to Steam)" if rep.kept else " (not saved)"))
            return 0 if rep.winner else 2
    except (archive.ArchiveError, LookupError, RuntimeError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _entry() -> None:
    """Entry point for the .pyz build."""
    sys.exit(main())
