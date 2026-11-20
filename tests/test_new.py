"""Tests for diagnose, runner, retry, archive, protontools and the known-mods list."""

import json
import os
import shutil
import subprocess
import tarfile
import time
import unittest
import zipfile
from pathlib import Path

import test_core as core

FAKE_PROTON = """#!/bin/sh
# fake `proton run EXE`: behaviour chosen by FAKE=...
mkdir -p "$PROTON_LOG_DIR"
log="$PROTON_LOG_DIR/steam-$SteamGameId.log"
case "$FAKE" in
  crash)
    printf 'wine: Unhandled page fault on read access to 0x0 at address 0x1 (thread 1)\\nBacktrace:\\n=>0 0x1 in heatedmetal.dll+0x7ff320 (0x0)\\n' > "$log"
    exit 1 ;;
  missing)
    printf 'err:module:import_dll Library MSVCP140.dll (which is needed by "Z:\\\\g\\\\a.exe") not found\\n' > "$log"
    exit 1 ;;
  quick) echo "fixme: fine" > "$log"; exit 0 ;;
  loader)
    echo "fixme: loader" > "$log"
    python3 -c "import os; os.execvp('sleep', ['Z:/g/game.exe', '30'])" &
    exit 1 ;;
  *)
    echo "fixme: all good" > "$log"
    exec sleep 30 ;;
esac
"""


def fake_proton(root: Path, name: str) -> "steam.Proton":
    from proton_porter import steam
    d = root / name
    d.mkdir(parents=True)
    (d / "proton").write_text(FAKE_PROTON)
    (d / "proton").chmod(0o755)
    return steam.Proton(name, name, d)


class Diagnose(unittest.TestCase):
    def test_crash_module_prefers_known_mod(self):
        from proton_porter.diagnose import parse_log
        d = parse_log("wine: Unhandled page fault on read access to 0x0 at address 0x1 (thread 1)\n"
                      "Backtrace:\n=>0 0x1 in rainbowsix.exe+0x5f35b64 (0x0)\n 1 0x2 in heatedmetal.dll+0x7ff320 (0x0)\n")
        self.assertTrue(d.crashed)
        self.assertEqual(d.crash_module, "heatedmetal.dll")
        self.assertIn("Proton-R6HM", d.findings[0].fix)

    def test_missing_dll(self):
        from proton_porter.diagnose import parse_log
        d = parse_log('err:module:import_dll Library MSVCP140.dll (which is needed by "Z:\\g\\Game.exe") not found\n', 42)
        self.assertEqual(d.findings[0].kind, "missing-dll")
        self.assertIn("protontricks 42 vcrun2022", d.findings[0].fix)

    def test_proton_seh_log(self):
        from proton_porter.diagnose import parse_log
        d = parse_log('1:warn:seh:dispatch_exception backtrace: --- Exception 0xc0000005.\n'
                      '2:warn:seh:virtual_unwind backtrace: 00006FFFE9C9F320: L"HeatedMetal.dll" + 00000000007FF320.\n'
                      '3:warn:seh:virtual_unwind backtrace: 00006FFFFFF3A8BD: L"ntdll.dll" + 000000000004A8BD.\n'
                      '4:warn:seh:virtual_unwind backtrace: 00006FFFF35B5B64: L"RainbowSix.exe" + 0000000005F35B64.\n')
        self.assertEqual(d.crash_module, "HeatedMetal.dll")
        self.assertIn("RainbowSix.exe+0x5F35B64", d.findings[0].message)
        self.assertFalse(d.crashed)  # a handled exception alone doesn't fail a run

    def test_game_own_crash_log(self):
        import tempfile
        from pathlib import Path
        from proton_porter.diagnose import Diagnosis, parse_game_logs
        d = Path(tempfile.mkdtemp())
        (d / "client-crashlog.txt").write_text("System.DllNotFoundException: steam_api assembly:<unknown>\n  at X\n")
        found = parse_game_logs(d)
        self.assertEqual(found[0].module, "steam_api")
        self.assertTrue(Diagnosis(found).game_log_errors)
        self.assertEqual(parse_game_logs(d, since=4e9), [])  # older than the run: ignored

    def test_error_popup_counts_as_failure(self):
        from proton_porter.diagnose import parse_log
        d = parse_log('1:trace:loaddll:build_module Loaded L"Z:\\g\\OnlineFix.dll" at 784D0000: native\n'
                      '2:trace:loaddll:build_module Loaded L"Z:\\g\\steam_api.dll" at 79400000: native\n'
                      '3:fixme:ntdll:NtRaiseHardError 0x50000018 4 0x3 0x206e664 0 0x206e674: stub\n')
        self.assertTrue(d.crashed)
        self.assertEqual(d.crash_module, "OnlineFix.dll")

    def test_clean_log(self):
        from proton_porter.diagnose import parse_log
        d = parse_log("fixme:ntdll:something\n")
        self.assertFalse(d.crashed)
        self.assertEqual(d.findings, [])


class Mods(core.Sandbox):
    def test_detect_and_analyze(self):
        from proton_porter import knowledge
        from proton_porter.analyze import analyze, overrides_string
        here = self.exe.parent
        (here / "OnlineFix64.dll").write_bytes(b"MZ")
        (here / "doorstop_config.ini").write_text("")
        (here / "winhttp.dll").write_bytes(b"MZ")
        ids = {m.id for m in knowledge.detect(here, here.parents[2])}
        self.assertEqual(ids, {"onlinefix", "bepinex"})
        a = analyze(self.exe, [])
        self.assertEqual(overrides_string(a.dlls), "winhttp=n,b;winmm=n,b")
        self.assertTrue(any("OnlineFix build" in n for n in a.notes))

    def test_json_is_valid(self):
        from proton_porter import knowledge
        for e in knowledge.load():
            self.assertTrue({"id", "name", "detect"} <= set(e), e)


class Archives(core.Sandbox):
    def make_tree(self):
        src = self.tmp / "src/Game"
        (src / "bin").mkdir(parents=True)
        (src / "bin/game.exe").write_bytes(b"MZ" * 100)
        (src / "data.pak").write_bytes(b"x" * 5000)
        return src

    def test_zip_roundtrip(self):
        from proton_porter import archive
        src = self.make_tree()
        z = self.tmp / "Game.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for p in src.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src.parent))
        folder = archive.install_game_archive(z, self.tmp / "Games")
        self.assertEqual(folder, self.tmp / "Games/Game")
        self.assertEqual((folder / "data.pak").stat().st_size, 5000)

    def test_flat_zip_gets_its_own_folder(self):
        from proton_porter import archive
        z = self.tmp / "My.Game.v1.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("game.exe", "MZ")
            zf.writestr("data/a.pak", "x")
        folder = archive.install_game_archive(z, self.tmp / "Games")
        self.assertEqual(folder, self.tmp / "Games/My.Game.v1")
        self.assertTrue((folder / "data/a.pak").is_file())
        self.assertEqual([p.name for p in (self.tmp / "Games").iterdir()], ["My.Game.v1"])

    @unittest.skipUnless(shutil.which("7z"), "7z not installed")
    def test_7z_crc_verified(self):
        from proton_porter import archive
        src = self.make_tree()
        z = self.tmp / "Game.7z"
        subprocess.run(["7z", "a", "-bso0", "-bsp0", str(z), str(src)], check=True)
        entries = archive.list_entries(z)
        self.assertTrue(all(e.crc is not None for e in entries))
        dest = self.tmp / "out"
        archive.extract(z, dest)
        self.assertEqual(archive.verify(dest, entries), [])
        (dest / "Game/data.pak").write_bytes(b"y" * 5000)  # same size, different content
        self.assertIn("damaged (CRC): Game/data.pak", archive.verify(dest, entries))
        (dest / "Game/bin/game.exe").unlink()
        self.assertIn("missing: Game/bin/game.exe", archive.verify(dest, entries))

    def test_tar_and_verify_failure_cleans_up(self):
        from proton_porter import archive
        src = self.make_tree()
        t = self.tmp / "Game.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            tf.add(src, arcname="Game")
        self.assertEqual({e.path for e in archive.list_entries(t)}, {"Game/bin/game.exe", "Game/data.pak"})
        orig = archive.verify
        archive.verify = lambda *a, **k: ["missing: x"]
        try:
            with self.assertRaises(archive.ArchiveError):
                archive.install_game_archive(t, self.tmp / "Games")
        finally:
            archive.verify = orig
        self.assertEqual(list((self.tmp / "Games").iterdir()), [])

    def test_tar_without_data_filter(self):
        import tarfile as tf_mod
        from proton_porter import archive
        src = self.make_tree()
        (src / "bin/game.exe").chmod(0o755)
        t = self.tmp / "Game.tar.gz"
        with tf_mod.open(t, "w:gz") as tf:
            tf.add(src, arcname="Game")
        saved = tf_mod.data_filter
        del tf_mod.data_filter  # pretend to be an old Python
        try:
            archive.extract(t, self.tmp / "out")
        finally:
            tf_mod.data_filter = saved
        self.assertEqual((self.tmp / "out/Game/data.pak").stat().st_size, 5000)
        self.assertTrue(os.access(self.tmp / "out/Game/bin/game.exe", os.X_OK))

    def test_unsafe_path_refused(self):
        from proton_porter import archive
        t = self.tmp / "evil.tar"
        with tarfile.open(t, "w") as tf:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            import io
            tf.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(archive.ArchiveError):
            archive.extract(t, self.tmp / "dest")
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_install_proton(self):
        from proton_porter import protontools
        build = self.tmp / "src/Proton-Test-1"
        build.mkdir(parents=True)
        (build / "proton").write_text("#!/bin/sh\n")
        (build / "version").write_text("1")
        t = self.tmp / "Proton-Test-1.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            tf.add(build, arcname="Proton-Test-1")
        tool = protontools.install_proton(t)
        self.assertEqual(tool.path, (self.steam / "compatibilitytools.d/Proton-Test-1").resolve())
        self.assertTrue(os.access(tool.path / "proton", os.X_OK))
        self.assertEqual(tool.internal, "Proton-Test-1")
        with self.assertRaises(Exception):
            protontools.install_proton(t)  # already installed

    def test_not_a_proton_archive(self):
        from proton_porter import archive, protontools
        z = self.tmp / "nope.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hi")
        with self.assertRaises(archive.ArchiveError):
            protontools.install_proton(z)
        self.assertEqual(list((self.steam / "compatibilitytools.d").iterdir()), [])


class Running(core.Sandbox):
    def setUp(self):
        super().setUp()
        from proton_porter import runners
        old = runners.wine_binary
        runners.wine_binary = lambda: None  # never start the real Wine in tests
        self.addCleanup(setattr, runners, "wine_binary", old)

    def spec(self, proton, fake, prefix=None):
        from proton_porter.runner import RunSpec
        return RunSpec(777, self.exe, self.exe.parent, proton, f"FAKE={fake} %command%", prefix)

    def test_crash_is_diagnosed(self):
        from proton_porter.runner import run_test
        r = run_test(self.spec(fake_proton(self.tmp, "P1"), "crash"), None, seconds=10, poll=0.1)
        self.assertFalse(r.survived)
        self.assertEqual(r.exit_code, 1)
        self.assertEqual(r.diagnosis.crash_module, "heatedmetal.dll")

    def test_loader_that_exits_counts_its_game(self):
        from proton_porter.runner import run_test
        r = run_test(self.spec(fake_proton(self.tmp, "P1"), "loader"), None, seconds=5, poll=0.1)
        self.assertTrue(r.survived)  # nvse_loader-style: the loader quit, the game ran on
        self.assertIsNone(r.exit_code)

    def test_survivor_is_stopped(self):
        from proton_porter.runner import TOKEN_VAR, run_test
        r = run_test(self.spec(fake_proton(self.tmp, "P1"), "ok"), None, seconds=1.5, poll=0.1)
        self.assertTrue(r.survived)
        time.sleep(0.3)
        for proc in Path("/proc").iterdir():  # nothing of ours may be left running
            try:
                self.assertNotIn(b"PROTON_PORTER_TEST=", (proc / "environ").read_bytes()
                                 if proc.name.isdigit() and b"sleep" in (proc / "cmdline").read_bytes() else b"")
            except OSError:
                pass

    def test_no_hyprland_runs_on_current_screen(self):
        from proton_porter import runner
        old = runner.shutil.which
        runner.shutil.which = lambda name: None if name == "hypruse" else old(name)
        try:
            self.assertEqual(runner.where(7), "on this screen")
            r = runner.run_test(self.spec(fake_proton(self.tmp, "P1"), "crash"), 7, seconds=10, poll=0.1)
            self.assertEqual(r.exit_code, 1)  # it ran, directly
        finally:
            runner.shutil.which = old

    def test_launch_option_parsing(self):
        from proton_porter.runner import parse_launch_options
        env, wrappers, args = parse_launch_options('WINEDLLOVERRIDES="winmm=n,b" PROTON_LOG=1 mangohud %command% -x')
        self.assertEqual(env, {"WINEDLLOVERRIDES": "winmm=n,b", "PROTON_LOG": "1"})
        self.assertEqual((wrappers, args), (["mangohud"], ["-x"]))
        self.assertEqual(parse_launch_options("-windowed"), ({}, [], ["-windowed"]))

    def test_retry_keeps_winner(self):
        from proton_porter import retry, steam
        user = steam.users()[0]
        steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent))
        appid = steam.library(user)[0].appid
        steam.set_compat_tool(appid, "P1")
        bad, good = fake_proton(self.tmp, "P1"), fake_proton(self.tmp, "P2")
        real = steam.prefix_dir(appid)
        (real / "pfx").mkdir(parents=True)
        (real / "pfx/marker").write_text("old")
        entry = steam.library(user)[0]
        entry.launch_options = ""

        def runner(spec, workspace, seconds, progress):
            from proton_porter.runner import run_test
            spec.launch_options = f"FAKE={'ok' if spec.proton.internal == 'P2' else 'crash'} %command%"
            return run_test(spec, None, 1.5, poll=0.1)

        rep = retry.retry(entry, user, [bad, good], 7, 1.5, keep=True, runner=runner)
        self.assertEqual(rep.winner.proton.internal, "P2")
        self.assertEqual([a.proton.internal for a in rep.attempts], ["P1", "P1", "P2"])
        self.assertTrue(rep.kept)
        self.assertEqual(steam.library(user)[0].tool, "P2")
        self.assertTrue(any(p.name.startswith(f"{appid}.pre-retry-") for p in real.parent.iterdir()))
        self.assertFalse(retry.fresh_prefix_path(appid).exists())

    def test_retry_nothing_survives(self):
        from proton_porter import retry, steam
        user = steam.users()[0]
        steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent))
        entry = steam.library(user)[0]
        p = fake_proton(self.tmp, "P1")

        def runner(spec, workspace, seconds, progress):
            from proton_porter.runner import run_test
            spec.launch_options = "FAKE=missing %command%"
            return run_test(spec, None, 1.5, poll=0.1)

        rep = retry.retry(entry, user, [p], 7, 1.5, runner=runner)
        self.assertIsNone(rep.winner)
        self.assertFalse(rep.kept)
        self.assertEqual(rep.attempts[0].result.diagnosis.findings[0].kind, "missing-dll")


if __name__ == "__main__":
    unittest.main()


class Updating(core.Sandbox):
    def test_check_compares_versions(self):
        from proton_porter import update
        self.assertEqual(update.check("1.1.1", lambda: "1.2.0"), "1.2.0")
        self.assertIsNone(update.check("1.1.1", lambda: "1.1.1"))
        self.assertIsNone(update.check("1.2.0", lambda: "1.1.9"))  # never downgrade
        self.assertEqual(update.check("1.9.0", lambda: "1.10.0"), "1.10.0")

    def test_install_swaps_package(self):
        from proton_porter import update
        pkg = self.tmp / "proton-porter/app/proton_porter"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "1.0.0"\n')
        (pkg / "gone.py").write_text("")
        src = self.tmp / "src"
        (src / "proton_porter").mkdir(parents=True)
        (src / "proton_porter/__init__.py").write_text('__version__ = "2.0.0"\n')
        self.assertEqual(update.install(src, pkg), "2.0.0")
        self.assertIn("2.0.0", (pkg / "__init__.py").read_text())
        self.assertFalse((pkg / "gone.py").exists())
        self.assertEqual(sorted(p.name for p in pkg.parent.iterdir()), ["proton_porter"])

    def test_download_unpacks_tarball(self):
        from proton_porter import update
        src = self.tmp / "Xmilon-proton-porter-abc/proton_porter"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text('__version__ = "3.0.0"\n')
        t = self.tmp / "tb.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            tf.add(self.tmp / "Xmilon-proton-porter-abc", arcname="Xmilon-proton-porter-abc")
        old = update._gh
        update._gh = lambda args: t.read_bytes()
        try:
            out = update.download_main(self.tmp / "dl")
        finally:
            update._gh = old
        self.assertTrue((out / "proton_porter/__init__.py").is_file())

    def test_play_keeps_log_only_on_crash(self):
        from proton_porter.runner import RunSpec, play, state_dir
        proton = fake_proton(self.tmp, "P1")
        ok = play(RunSpec(5, self.exe, self.exe.parent, proton, "FAKE=quick %command%"), poll=0.1)
        self.assertTrue(ok.survived)
        self.assertEqual(list((state_dir() / "logs").glob("5-*")), [])
        bad = play(RunSpec(5, self.exe, self.exe.parent, proton, "FAKE=crash %command%"), poll=0.1)
        self.assertFalse(bad.survived)
        self.assertEqual(bad.diagnosis.crash_module, "heatedmetal.dll")
        self.assertTrue(bad.log.is_file())
        self.assertIn("heatedmetal", (bad.log.parent / "diagnosis.txt").read_text())


class Repacks(core.Sandbox):
    def make(self, name="Cool Game [FitGirl Repack]"):
        import hashlib
        d = self.tmp / name
        (d / "MD5").mkdir(parents=True)
        tiny_pe = core.tiny_pe
        tiny_pe(d / "setup.exe")
        (d / "fg-01.bin").write_bytes(b"abc")
        h = hashlib.md5(b"abc").hexdigest()
        (d / "MD5/fitgirl-bins.md5").write_text(f"; comment\r\n{h} *..\\fg-01.bin\r\n")
        return d

    def test_detect_and_name(self):
        from proton_porter import repack
        r = repack.detect(self.make())
        self.assertEqual((r.kind, r.name), ("FitGirl", "Cool Game"))
        self.assertEqual(repack.detect(self.make("Other Game-DODI Repack") / "setup.exe").kind, "DODI")
        self.assertIsNone(repack.detect(self.exe.parent))  # a normal game folder

    def test_verify(self):
        from proton_porter import repack
        d = self.make()
        r = repack.detect(d)
        self.assertEqual(repack.verify(r), [])
        (d / "fg-01.bin").write_bytes(b"abd")
        self.assertIn("damaged: fg-01.bin (re-download it)", repack.verify(r))
        (d / "fg-01.bin").unlink()
        self.assertIn("missing: fg-01.bin", repack.verify(r))

    def test_install_runs_setup_with_dir(self):
        from proton_porter import repack, steam
        d = self.make()
        proton = self.tmp / "P"
        proton.mkdir()
        # fake proton: "install" by creating an exe inside the /DIR= folder (given as Z:\...)
        (proton / "proton").write_text('#!/bin/sh\nd=$(echo "$3" | sed "s#^/DIR=Z:##; s#\\\\\\\\#/#g")\n'
                                       'mkdir -p "$d" && cp "$2" "$d/game.exe"\n')
        (proton / "proton").chmod(0o755)
        out = repack.install(repack.detect(d), self.tmp / "Games", steam.Proton("P", "P", proton))
        self.assertEqual(out, self.tmp / "Games/Cool Game")
        self.assertTrue((out / "game.exe").is_file())


class Uninstalling(core.Sandbox):
    def _game(self):
        from proton_porter import steam
        self.games = self.tmp / "home/Games"
        game = self.games / "Cool Game"
        game.mkdir(parents=True)
        core.tiny_pe(game / "Cool.exe")
        old_home = os.environ.get("HOME")
        self.addCleanup(os.environ.__setitem__, "HOME", old_home)
        os.environ["HOME"] = str(self.tmp / "home")
        self.user = steam.users()[0]
        steam.write_shortcut(self.user, steam.Shortcut("Cool Game", game / "Cool.exe", game))
        self.entry = steam.library(self.user)[0]

    def test_plan_and_trash(self):
        self._game()
        from proton_porter import uninstall
        (self.games / "Cool Game/unins000.exe").write_bytes(b"MZ")
        plan = uninstall.make_plan(self.entry, self.games, [self.entry])
        self.assertEqual(plan.folder, self.games / "Cool Game")
        self.assertEqual(plan.uninstaller.name, "unins000.exe")
        trashed = []
        old = uninstall.trash
        uninstall.trash = trashed.append
        try:
            uninstall.execute(plan, None, False)  # no Proton: the uninstaller is skipped
        finally:
            uninstall.trash = old
        self.assertEqual(trashed, [self.games / "Cool Game"])

    def test_refuses_shared_folders(self):
        self._game()
        from proton_porter import steam, uninstall
        # exe straight in the games folder: the folder is shared, must not be trashed
        core.tiny_pe(self.games / "loose.exe")
        loose = steam.LibraryEntry(1, "Loose", self.games / "loose.exe", self.games, "", "")
        plan = uninstall.make_plan(loose, self.games, [loose, self.entry])
        self.assertTrue(plan.blocked)
        self.assertIsNone(plan.uninstaller)
        # a folder holding another library game
        bundle = self.games / "Bundle"
        (bundle / "Other").mkdir(parents=True)
        core.tiny_pe(bundle / "Other/o.exe")
        self.assertIn("another game", uninstall.why_unsafe(bundle, self.games, [bundle / "Other/o.exe"]) or "")

    def test_wrapper_folder_included(self):
        self._game()
        from proton_porter import steam, uninstall
        w = self.games / "Game-Site.com/Game"
        w.mkdir(parents=True)
        core.tiny_pe(w / "g.exe")
        e = steam.LibraryEntry(3, "Game", w / "g.exe", w, "", "")
        self.assertEqual(uninstall.make_plan(e, self.games, [e]).folder, self.games / "Game-Site.com")


class OtherRunners(core.Sandbox):
    def test_wrapper_roundtrip_and_library(self):
        from proton_porter import runners, steam
        user = steam.users()[0]
        script = runners.write_wrapper(self.exe, self.exe.parent)
        self.assertEqual(runners.read_wrapper(script), self.exe.resolve())
        self.assertIn("exec wine", script.read_text())
        steam.write_shortcut(user, steam.Shortcut("My Game", script, script.parent))
        e = steam.library(user)[0]
        self.assertEqual((e.exe, e.tool), (self.exe.resolve(), runners.WINE))

    def test_switch_runner(self):
        from proton_porter import runners, steam
        from proton_porter.apply import switch_runner
        user = steam.users()[0]
        appid, _ = steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent, "-x"))
        steam.set_compat_tool(appid, "Proton-GE")
        wine = steam.Proton(runners.WINE, "System Wine", Path("/usr/bin"))
        switch_runner(user, steam.library(user)[0], wine)
        e = steam.library(user)[0]
        self.assertEqual((e.appid, e.tool, e.exe, e.launch_options), (appid, runners.WINE, self.exe.resolve(), "-x"))
        self.assertNotIn(str(appid), steam.compat_mapping())
        switch_runner(user, e, steam.Proton("Proton-GE", "GE", self.tmp))
        e = steam.library(user)[0]
        self.assertEqual((e.tool, e.exe), ("Proton-GE", self.exe.resolve()))

    def test_native_game_added_without_proton(self):
        from proton_porter import runners, steam
        from proton_porter.analyze import analyze
        from proton_porter.apply import Choices, apply
        game = self.tmp / "Linux Game"
        game.mkdir()
        binary = game / "game.x86_64"
        binary.write_bytes(b"\x7fELF" + b"\0" * 60)
        binary.chmod(0o755)
        a = analyze(binary, [])
        self.assertTrue(a.native)
        self.assertEqual((a.name, a.dlls, a.warnings), ("Linux Game", [], []))
        user = steam.users()[0]
        r = apply(a, "", Choices(name="Linux Game", proton=runners.pseudo(runners.NATIVE), user=user,
                                 extra_env="FOO=1", args="--fullscreen"))
        self.assertEqual(r.launch_options, "FOO=1 %command% --fullscreen")
        self.assertNotIn(str(r.appid), steam.compat_mapping())
        self.assertEqual(steam.library(user)[0].tool, runners.NATIVE)

    def test_wine_command(self):
        from proton_porter import runners, steam
        from proton_porter.runner import RunSpec, build_command
        spec = RunSpec(5, self.exe, self.exe.parent, steam.Proton(runners.WINE, "W", Path("/usr/bin")),
                       'WINEDLLOVERRIDES="winmm=n,b" %command% -x')
        cmd, env = build_command(spec, self.tmp / "log", "tok")
        self.assertEqual(cmd[-3:], ["wine", str(self.exe), "-x"])
        self.assertEqual(env["WINEPREFIX"], str(runners.wine_prefix(self.exe)))
        self.assertEqual(env["WINEDLLOVERRIDES"], "winmm=n,b")
        self.assertNotIn("STEAM_COMPAT_DATA_PATH", env)

    def test_repack_with_wine(self):
        from proton_porter import repack, runners, steam
        d = self.tmp / "G [FitGirl Repack]"
        d.mkdir()
        core.tiny_pe(d / "setup.exe")
        (d / "fg-01.bin").write_bytes(b"x")
        cmd, env = repack.install_command(repack.detect(d), self.tmp / "Games/G",
                                          steam.Proton(runners.WINE, "W", Path("/usr/bin")))
        self.assertEqual(cmd[0], "wine")
        self.assertTrue(cmd[2].startswith("/DIR=Z:"))
        self.assertTrue(env["WINEPREFIX"].endswith("installer-prefix/wine"))


class Helpers(core.Sandbox):
    def test_launcher_entry(self):
        from proton_porter import desktop, steam
        user = steam.users()[0]
        steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent))
        e = steam.library(user)[0]
        p = desktop.add(e)
        text = p.read_text()
        self.assertIn(f"Exec=steam steam://rungameid/{steam.rungameid(e.appid)}", text)
        self.assertIn("Name=My Game", text)
        self.assertTrue(desktop.exists(e.appid))
        self.assertTrue(desktop.remove(e.appid))
        self.assertFalse(desktop.exists(e.appid))

    def test_steam_logging_options(self):
        from proton_porter import runner, steam
        on = runner.with_steam_logging('WINEDLLOVERRIDES="a=n;b=n,b" %command%', 7, True)
        self.assertTrue(runner.steam_logging(on))
        self.assertIn('WINEDLLOVERRIDES="a=n;b=n,b" %command%', on)
        self.assertEqual(runner.with_steam_logging(on, 7, False), 'WINEDLLOVERRIDES="a=n;b=n,b" %command%')
        with self.assertRaises(RuntimeError):
            runner.with_steam_logging("-windowed", 7, True)
        user = steam.users()[0]
        appid, _ = steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent, "", "icon.png"))
        self.assertTrue(steam.set_launch_options(user, appid, on))
        e = steam.library(user)[0]
        self.assertEqual((e.launch_options, e.icon), (on, "icon.png"))

    def test_fix_it_installs_suggested_component(self):
        from proton_porter import doctor, runner, steam
        from proton_porter.diagnose import Diagnosis, Finding
        user = steam.users()[0]
        appid, _ = steam.write_shortcut(user, steam.Shortcut("My Game", self.exe, self.exe.parent))
        steam.set_compat_tool(appid, "Proton-GE")
        e = steam.library(user)[0]
        calls = []
        old = doctor._protontricks
        doctor._protontricks = lambda a, v: calls.append(v) or True
        try:
            def run(spec, ws, secs, progress):
                ok = bool(calls)
                d = Diagnosis() if ok else Diagnosis([Finding("missing-dll", "MSVCP140.dll is missing", "MSVCP140.dll",
                                                               "protontricks 1 vcrun2022")], crashed=True)
                return runner.RunResult(ok, 1, None if ok else 1, None, d, "P")
            rep = doctor.fix_it(e, user, None, 1, run=run)
        finally:
            doctor._protontricks = old
        self.assertEqual(calls, ["vcrun2022"])
        self.assertTrue(rep.fixed)
        self.assertIn("Fixed by: Install vcrun2022 with protontricks.", rep.notes)

    def test_fixes_for_graphics(self):
        from proton_porter import doctor, runner
        from proton_porter.diagnose import Diagnosis, Finding
        r = runner.RunResult(False, 1, 1, None, Diagnosis([Finding("crash", "x", "d3d11.dll")], True), "P")
        self.assertEqual(doctor.fixes_for(r)[0].env, {"PROTON_USE_WINED3D": "1"})


class Emulators(core.Sandbox):
    def test_bundle_detected_with_roms(self):
        from proton_porter.analyze import analyze
        d = self.tmp / "Some Game"
        (d / "games").mkdir(parents=True)
        core.tiny_pe(d / "lichii.exe")
        big = bytearray((d / "lichii.exe").read_bytes()) + b"\0" * 5000
        (d / "fritchii.exe").write_bytes(bytes(big))
        (d / "games/title.nup").write_bytes(b"x")
        a = analyze(d / "lichii.exe", [])
        self.assertEqual(a.emulator.ui_exe.name, "fritchii.exe")
        self.assertEqual([r.name for r in a.emulator.roms], ["title.nup"])
        self.assertTrue(any("second Steam entry" in n for n in a.notes))
        self.assertIsNone(analyze(self.exe, []).emulator)
