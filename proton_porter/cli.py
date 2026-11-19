"""Command line: `proton-porter analyze GAME.exe` / `proton-porter add GAME.exe`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, steam
from .analyze import analyze, overrides_string
from .apply import Choices, apply, describe, parse_env


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
    if argv and not argv[0].startswith("-") and Path(argv[0]).is_file():
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
    add.add_argument("--proton", help="internal name, e.g. Proton-GE or proton_experimental")
    add.add_argument("--args", default="", help="arguments for the game")
    add.add_argument("--env", default="", help='extra variables, e.g. "DXVK_HUD=fps"')
    add.add_argument("--collection", default="Linux")
    add.add_argument("--dll", action="append", default=[], help="force a DLL override on (repeatable)")
    add.add_argument("--no-dll", action="append", default=[], help="turn a detected override off")
    add.add_argument("--no-saves", action="store_true", help="don't copy detected saves")
    add.add_argument("--search", action="append", type=Path, default=[],
                     help="extra folder to look for old saves in (a Windows backup or user folder)")
    add.add_argument("--no-restart", action="store_true", help="leave Steam closed afterwards")
    add.add_argument("--dry-run", action="store_true")

    sub.add_parser("protons", help="list Proton versions Steam can use")

    a = p.parse_args(argv)
    if a.cmd is None:
        from .gui import run
        return run()
    if a.cmd == "protons":
        for t in steam.protons():
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
    tools = steam.protons()
    tool = next((t for t in tools if t.internal == a.proton), None) if a.proton else steam.default_proton(tools)
    if tool is None:
        print("No Proton found. Install one from Steam (Library > Tools) first.", file=sys.stderr)
        return 1
    users = steam.users()
    if not users:
        print("No Steam user found. Log in to Steam once first.", file=sys.stderr)
        return 1
    c = Choices(name=a.name or info.name, proton=tool, user=users[0], move_to=a.move_to,
                args=a.args, extra_env=parse_env(a.env), collection=a.collection,
                saves=[] if a.no_saves else info.saves, restart_steam=not a.no_restart)
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


if __name__ == "__main__":
    sys.exit(main())


def _entry() -> None:
    """Entry point for the .pyz build."""
    sys.exit(main())
