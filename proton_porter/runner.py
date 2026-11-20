"""Start a game through Proton outside Steam's UI, with a Proton log, and watch it.

Games are only ever started on a workspace the caller names (`hypruse launch
--workspace N`), so a test never lands on the screen the user is working on.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import steam
from .diagnose import Diagnosis, find_log, parse_file, parse_game_logs

Progress = Callable[[str, float | None], None]
TOKEN_VAR = "PROTON_PORTER_TEST"
WRAPPERS = ("gamemoderun", "mangohud")


@dataclass
class RunSpec:
    appid: int
    exe: Path
    start_dir: Path
    proton: steam.Proton
    launch_options: str = ""
    prefix: Path | None = None  # STEAM_COMPAT_DATA_PATH; None = the game's usual compatdata folder
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class RunResult:
    survived: bool
    seconds: float
    exit_code: int | None  # None = still running when the test ended
    log: Path | None
    diagnosis: Diagnosis
    proton: str = ""
    prefix: Path | None = None

    def line(self) -> str:
        if self.survived:
            return f"{self.proton}: still running after {self.seconds:.0f}s"
        how = f"exit code {self.exit_code}" if self.exit_code is not None else "hung or crashed"
        return f"{self.proton}: stopped after {self.seconds:.0f}s ({how})"


def parse_launch_options(options: str) -> tuple[dict[str, str], list[str], list[str]]:
    """'A=1 gamemoderun %command% -x' -> ({'A': '1'}, ['gamemoderun'], ['-x'])."""
    try:
        parts = shlex.split(options or "")
    except ValueError:
        parts = (options or "").split()
    if "%command%" in parts:
        i = parts.index("%command%")
        before, after = parts[:i], parts[i + 1:]
    else:
        before, after = [], parts
    env: dict[str, str] = {}
    wrappers: list[str] = []
    for p in before:
        if "=" in p and p.split("=", 1)[0].replace("_", "").isalnum():
            k, v = p.split("=", 1)
            env[k] = v
        elif Path(p).name in WRAPPERS:
            wrappers.append(p)
    return env, wrappers, after


def build_command(spec: RunSpec, log_dir: Path, token: str) -> tuple[list[str], dict[str, str]]:
    """The command and the extra environment (a dict, so it can go through `env`)."""
    opt_env, wrappers, args = parse_launch_options(spec.launch_options)
    prefix = spec.prefix or steam.prefix_dir(spec.appid)
    env = dict(opt_env)
    env.update({
        "STEAM_COMPAT_DATA_PATH": str(prefix),
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam.steam_root()),
        "SteamGameId": str(spec.appid),
        "SteamAppId": str(spec.appid),
        "PROTON_LOG": "1",
        "PROTON_LOG_DIR": str(log_dir),
        TOKEN_VAR: token,
    })
    env.update(spec.extra_env)
    cmd = [w for w in wrappers if shutil.which(w)]
    from . import runners
    if runners.is_pseudo(spec.proton):
        # No Proton: plain Wine (own prefix) or the Linux program itself. Its stderr is the log.
        for k in ("STEAM_COMPAT_DATA_PATH", "PROTON_LOG", "PROTON_LOG_DIR"):
            env.pop(k, None)
        game = [str(spec.exe), *args]
        if spec.proton.internal == runners.WINE:
            env["WINEPREFIX"] = str(spec.prefix or runners.wine_prefix(spec.exe))
            env.setdefault("WINEDEBUG", "+loaddll,+seh")
            game = ["wine", *game]
        log = log_dir / f"steam-{spec.appid}.log"
        return ["sh", "-c", f'exec "$@" 2>>"{log}"', "sh", *cmd, *game], env
    cmd += [str(spec.proton.path / "proton"), "run", str(spec.exe), *args]
    return cmd, env


# Wine/Proton helper processes: they carry the token too but aren't the game.
INFRA = {"wineserver", "services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe", "explorer.exe",
         "rpcss.exe", "tabtip.exe", "xalia.exe", "conhost.exe", "steam.exe", "start.exe", "cmd.exe",
         "rundll32.exe", "wineboot.exe", "winemenubuilder.exe", "mscorsvw.exe", "lsass.exe"}


def _game_pids(token: str) -> list[int]:
    """Windows programs of this run that are still alive, other than Wine's own helpers.
    Loaders (nvse_loader, launchers) exit after starting the game; the game shows up here."""
    needle = f"{TOKEN_VAR}={token}".encode()
    out = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if needle not in (proc / "environ").read_bytes().split(b"\0"):
                continue
            argv0 = (proc / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
        except OSError:
            continue
        name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name.endswith(".exe") and name not in INFRA:
            out.append(int(proc.name))
    return out


def _kill_token(token: str, grace: float = 3.0) -> None:
    """End every process that carries this test's token (the game, wine, wineserver)."""
    needle = f"{TOKEN_VAR}={token}".encode()

    def mine() -> list[int]:
        out = []
        for proc in Path("/proc").iterdir():
            if proc.name.isdigit() and int(proc.name) != os.getpid():
                try:
                    if needle in (proc / "environ").read_bytes().split(b"\0"):
                        out.append(int(proc.name))
                except OSError:
                    continue
        return out

    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = mine()
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.time() + grace
        while time.time() < end and mine():
            time.sleep(0.2)
        if not mine():
            return


def _launch(cmd: list[str], env: dict[str, str], cwd: Path, workspace: int | None,
            pidfile: Path, rcfile: Path) -> None:
    # Wrapper records the exit code, since `hypruse launch` doesn't hand the process back.
    script = f'echo $$ > "{pidfile}"; "$@"; echo $? > "{rcfile}"'
    inner = ["env", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", script, "sh", *cmd]
    if workspace is not None and workspace_support():
        res = subprocess.run(["hypruse", "launch", "--workspace", str(workspace), "--", *inner],
                             cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        # 4 = started, but no window within 8 s (first start, prefix being built): fine,
        # herd_windows() moves late windows to the workspace while the run lasts.
        if res.returncode not in (0, 4):
            raise RuntimeError(f"hypruse couldn't start the game: {res.stderr.strip() or res.returncode}")
    else:
        subprocess.Popen(inner, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def herd_windows(appid: int, workspace: int, exe_name: str = "") -> None:
    """Move this game's windows onto the workspace: class steam_app_<appid> under Proton,
    the .exe's name (e.g. "game.exe") under plain Wine."""
    try:
        import json
        out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5).stdout
        for c in json.loads(out or "[]"):
            mine = c.get("class") == f"steam_app_{appid}" or (exe_name and c.get("class", "").lower() == exe_name)
            if mine and c.get("workspace", {}).get("id") != workspace:
                subprocess.run(["hypruse", "hypr", "move_window", c["address"], str(workspace)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def workspace_support() -> bool:
    """Games can be started on a chosen workspace (Hyprland with hypruse). Elsewhere they open
    on the current screen."""
    return bool(shutil.which("hypruse")) and bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def where(workspace: int | None) -> str:
    """Where a test run will show up, for messages."""
    if workspace is not None and workspace_support():
        return f"on workspace {workspace}"
    return "on this screen"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "proton-porter"


def run_test(spec: RunSpec, workspace: int | None, seconds: float = 60, progress: Progress = lambda *_: None,
             poll: float = 0.5) -> RunResult:
    """Start the game, wait up to `seconds`, then stop it and read the log.

    survived = still running at the end and the log shows no crash.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = uuid.uuid4().hex
    log_dir = state_dir() / "logs" / f"{spec.appid}-{stamp}-{spec.proton.internal}"
    log_dir.mkdir(parents=True, exist_ok=True)
    from . import runners
    if spec.proton.internal == runners.WINE:
        spec_prefix = spec.prefix or runners.wine_prefix(spec.exe)
    elif spec.proton.internal == runners.NATIVE:
        spec_prefix = None
    else:
        spec_prefix = spec.prefix or steam.prefix_dir(spec.appid)
    if spec_prefix:
        spec_prefix.mkdir(parents=True, exist_ok=True)
    cmd, env = build_command(spec, log_dir, token)
    pidfile, rcfile = log_dir / "pid", log_dir / "exit-code"
    cwd = spec.start_dir if spec.start_dir.is_dir() else spec.exe.parent
    progress(f"Starting with {spec.proton.display} {where(workspace)}", None)
    _launch(cmd, env, cwd, workspace, pidfile, rcfile)
    t0 = time.time()
    code: int | None = None
    exited_at: float | None = None  # when the launched program itself ended
    try:
        while time.time() - t0 < seconds:
            if exited_at is None and rcfile.exists():
                try:
                    code = int(rcfile.read_text().strip() or 0)
                except ValueError:
                    code = -1
                exited_at = time.time()
            if exited_at is not None and not _game_pids(token) and time.time() - exited_at > 3:
                break  # nothing of the game left (3 s grace for a loader's child to appear)
            progress(f"Running with {spec.proton.display}", min((time.time() - t0) / seconds, 1.0))
            if workspace is not None and workspace_support():
                herd_windows(spec.appid, workspace, spec.exe.name.lower())
            time.sleep(poll)
    finally:
        _kill_token(token)
    took = time.time() - t0
    log = find_log(log_dir, spec.appid)
    diagnosis = parse_file(log, spec.appid) if log else Diagnosis()
    diagnosis.findings += parse_game_logs(cwd, t0)
    alive_at_end = took >= seconds  # the loop only runs out of time while something of the game lives
    if alive_at_end and code is not None:
        code = None  # a loader exited, but the game it started was still running
    survived = alive_at_end and not diagnosis.crashed and not diagnosis.game_log_errors
    return RunResult(survived, took, code, log, diagnosis, spec.proton.display, spec_prefix)


def spec_for(entry: steam.LibraryEntry, tools: list[steam.Proton]) -> RunSpec:
    from . import runners
    tool = next((t for t in tools if t.internal == entry.tool), None) or runners.pseudo(entry.tool)
    if tool is None:
        tool = steam.default_proton(tools)
    if tool is None:
        raise RuntimeError("No Proton found")
    return RunSpec(entry.appid, entry.exe, entry.start_dir, tool, entry.launch_options)



def _candidate_logs(appid: int, game_dir: Path | None = None) -> list[Path]:
    """Proton logs that may belong to this game: test/play runs, and logs Steam wrote to ~
    (Steam names them after the id it passes, which can be the game's steam_appid.txt)."""
    out = [log for run in (state_dir() / "logs").glob(f"{appid}-*") if (log := find_log(run, appid))]
    ids = {str(appid)}
    if game_dir:
        try:
            ids.add((game_dir / "steam_appid.txt").read_text().strip())
        except OSError:
            pass
    out += [p for i in ids if i and (p := Path.home() / f"steam-{i}.log").is_file()]
    return out


def latest_log(appid: int, game_dir: Path | None = None) -> Path | None:
    """The newest Proton log for this game, from any source."""
    logs = _candidate_logs(appid, game_dir)
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def latest_report(entry: steam.LibraryEntry) -> tuple[str, Path | None]:
    """Readable summary of the newest Proton log and the game's own crash logs, plus a folder to open."""
    from .diagnose import parse_game_logs
    stamp = lambda p: time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))  # noqa: E731
    lines: list[str] = []
    log = latest_log(entry.appid, entry.start_dir)
    if log:
        lines.append(f"Proton log ({stamp(log)}):\n{log}")
        lines += [f"  • {x}" for x in parse_file(log, entry.appid).summary()]
    else:
        lines.append("No Proton log yet. Use “Test & diagnose”, or Play (it keeps the log if the game crashes).")
    own = parse_game_logs(entry.start_dir)
    if own:
        lines.append("\nThe game's own crash log:")
        for f in own:
            lines.append(f"  • {f.message}" + (f"\n    -> {f.fix}" if f.fix else ""))
    return "\n".join(lines), (log.parent if log else entry.start_dir)


def find_entry(user: steam.SteamUser, query: str) -> steam.LibraryEntry:
    """A library game by appid or (part of) its name."""
    entries = steam.library(user)
    if query.isdigit():
        for e in entries:
            if e.appid == int(query):
                return e
    exact = [e for e in entries if e.name.lower() == query.lower()]
    part = [e for e in entries if query.lower() in e.name.lower()]
    hits = exact or part
    if len(hits) != 1:
        names = ", ".join(e.name for e in (hits or entries))
        raise LookupError(f"{'No game' if not hits else 'Several games'} match “{query}”. Have: {names}")
    return hits[0]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def prune_logs(keep: int = 20) -> None:
    runs = sorted((p for p in (state_dir() / "logs").glob("*") if not p.name.endswith("-steam")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in runs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def play(spec: RunSpec, poll: float = 1.0, keep_logs: int = 20) -> RunResult:
    """Run the game until it ends, with a Proton log. The log is kept only if it crashed
    (non-zero exit, or a crash in the log); a clean exit deletes it. Blocks."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_dir = state_dir() / "logs" / f"{spec.appid}-{stamp}-play"
    log_dir.mkdir(parents=True, exist_ok=True)
    from . import runners
    prefix = None if runners.is_pseudo(spec.proton) else (spec.prefix or steam.prefix_dir(spec.appid))
    if prefix:
        prefix.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    cmd, env = build_command(spec, log_dir, token)
    pidfile, rcfile = log_dir / "pid", log_dir / "exit-code"
    cwd = spec.start_dir if spec.start_dir.is_dir() else spec.exe.parent
    _launch(cmd, env, cwd, None, pidfile, rcfile)
    t0 = time.time()
    code: int | None = None
    exited_at: float | None = None
    game_seen = False
    while True:
        if exited_at is None:
            if rcfile.exists():
                try:
                    code = int(rcfile.read_text().strip() or 0)
                except ValueError:
                    code = -1
                exited_at = time.time()
            elif pidfile.exists() and not _alive(int(pidfile.read_text().strip() or 0)):
                code, exited_at = -1, time.time()  # killed before it could record an exit code
        if exited_at is not None:
            if _game_pids(token):
                game_seen = True  # a loader ended, the game it started is running
            elif time.time() - exited_at > 3:
                break
        time.sleep(poll)
    if game_seen:
        code = None  # the loader's exit code says nothing about the game: judge by the logs
    log = find_log(log_dir, spec.appid)
    diagnosis = parse_file(log, spec.appid) if log else Diagnosis()
    diagnosis.findings += parse_game_logs(cwd, t0)
    crashed = (code is not None and code != 0) or diagnosis.crashed or diagnosis.game_log_errors
    result = RunResult(not crashed, time.time() - t0, code, log if crashed else None, diagnosis,
                       spec.proton.display, prefix)
    if crashed:
        (log_dir / "diagnosis.txt").write_text("\n".join(diagnosis.summary()) + f"\nexit code: {code}\n")
        prune_logs(keep_logs)
    else:
        shutil.rmtree(log_dir, ignore_errors=True)
    return result


def steam_log_dir(appid: int) -> Path:
    return state_dir() / "logs" / f"{appid}-steam"


def steam_logging(options: str) -> bool:
    return "PROTON_LOG=1" in (options or "").split()


def with_steam_logging(options: str, appid: int, on: bool) -> str:
    """Launch options with PROTON_LOG on or off, so launches from Steam leave a log Proton Porter reads."""
    words = [w for w in shlex.split(options or "") if not w.startswith(("PROTON_LOG=", "PROTON_LOG_DIR="))]
    if "%command%" not in words:
        if words:  # plain arguments: Steam can't combine them with variables for non-Steam games
            raise RuntimeError("This game passes arguments without %command%, so a log variable can't be added. "
                               "Use “Test & diagnose” instead.")
        words = ["%command%"]
    if on:
        words = ["PROTON_LOG=1", f"PROTON_LOG_DIR={steam_log_dir(appid)}"] + words
    if on:
        steam_log_dir(appid).mkdir(parents=True, exist_ok=True)

    def q(w: str) -> str:  # quote only the value of VAR=value, or the shell stops seeing an assignment
        if w == "%command%":
            return w
        k, eq, v = w.partition("=")
        if eq and k.replace("_", "").isalnum() and words.index(w) < words.index("%command%"):
            return f"{k}={shlex.quote(v)}" if shlex.quote(v) == v else f'{k}="{v}"'
        return shlex.quote(w)
    out = " ".join(q(w) for w in words)
    return "" if out == "%command%" else out
