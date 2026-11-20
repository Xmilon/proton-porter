"""Run with: python3 -m unittest discover tests"""

import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import vdf


def _w(path, fn):
    with open(path, "w") as fh:
        fn(fh)


def tiny_pe(path: Path, dotnet: bool = False) -> None:
    """A minimal 64-bit PE with no imports or resources."""
    d = bytearray(0x400)
    d[0:2] = b"MZ"
    struct.pack_into("<I", d, 0x3C, 0x80)
    d[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", d, 0x84, 0x8664, 1)
    struct.pack_into("<H", d, 0x84 + 16, 240)
    opt = 0x98
    struct.pack_into("<H", d, opt, 0x20B)
    struct.pack_into("<I", d, opt + 108, 16)
    if dotnet:
        struct.pack_into("<II", d, opt + 112 + 8 * 14, 0x1000, 0x48)
    sect = opt + 240
    struct.pack_into("<IIII", d, sect + 8, 0x1000, 0x1000, 0x200, 0x200)
    path.write_bytes(bytes(d))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.steam = self.tmp / "Steam"
        cfg = self.steam / "userdata/123/config"
        (cfg / "cloudstorage").mkdir(parents=True)
        (self.steam / "config").mkdir()
        vdf.dump({"users": {str(76561197960265728 + 123): {"PersonaName": "me", "MostRecent": "1"}}},
                 open(self.steam / "config/loginusers.vdf", "w"))
        _w(self.steam / "config/config.vdf", lambda fh: vdf.dump({"InstallConfigStore": {"Software": {"Valve": {"Steam": {}}}}}, fh))
        _w(cfg / "cloudstorage/cloud-storage-namespace-1.json", lambda fh: json.dump(
            [["user-collections.favorite", {"key": "user-collections.favorite", "timestamp": 1,
              "value": '{"id":"favorite","name":"Favorites","added":[],"removed":[]}', "version": "5"}]], fh))
        os.environ["PROTON_PORTER_STEAM_ROOT"] = str(self.steam)
        os.environ["XDG_STATE_HOME"] = str(self.tmp / "state")
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        game = self.tmp / "Old/My Game/Binaries/Win64"
        game.mkdir(parents=True)
        tiny_pe(game / "MyGame-Win64-Shipping.exe")
        (game / "winmm.dll").write_bytes(b"MZ")
        (game / "dbghelp.dll").write_bytes(b"MZ")
        saves = self.tmp / "Backup/Documents/My Games/MyGame"
        saves.mkdir(parents=True)
        (saves / "slot.sav").write_text("x")
        self.exe = game / "MyGame-Win64-Shipping.exe"
        from proton_porter import apply
        apply.MOVE_QUIET_SECONDS = 0

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("PROTON_PORTER_STEAM_ROOT", None)

    def test_analyze(self):
        from proton_porter.analyze import analyze, overrides_string
        a = analyze(self.exe, [self.tmp / "Backup"])
        self.assertEqual(a.root, self.tmp / "Old/My Game")
        self.assertEqual(a.name, "My Game")
        self.assertEqual(a.engine, "Unreal Engine")
        self.assertEqual(overrides_string(a.dlls), "winmm=n,b")  # dbghelp found but off
        self.assertEqual([s.dest for s in a.saves], ["Documents/My Games/MyGame"])

    def test_bat_cleanup(self):
        from proton_porter.analyze import analyze
        from proton_porter.apply import linux_bat
        bat = self.exe.with_name("Launch.bat")
        bat.write_text('@echo off\r\nstart "" game.exe /x\r\npause >nul\r\ntaskkill /IM game.exe /F\r\n')
        self.assertTrue(analyze(bat, []).bat_needs_copy)
        out = linux_bat(bat).read_text()
        self.assertIn('start "" /wait game.exe /x', out)
        self.assertNotIn("pause", out)
        self.assertNotIn("taskkill", out)

    def test_shortcut_compat_collection(self):
        from proton_porter import steam
        user = steam.users()[0]
        sc = steam.Shortcut("My Game", self.exe, self.exe.parent, 'WINEDLLOVERRIDES="winmm=n,b" %command%')
        appid, updated = steam.write_shortcut(user, sc)
        self.assertFalse(updated)
        self.assertEqual(appid, steam.shortcut_appid(f'"{self.exe}"', "My Game"))
        again, updated = steam.write_shortcut(user, sc)
        self.assertTrue(updated)
        self.assertEqual(again, appid)
        data = vdf.binary_loads(Path(user.config / "shortcuts.vdf").read_bytes())
        self.assertEqual(len(data["shortcuts"]), 1)
        steam.set_compat_tool(appid, "proton_experimental")
        cfg = vdf.loads(Path(self.steam / "config/config.vdf").read_text())
        self.assertEqual(cfg["InstallConfigStore"]["Software"]["Valve"]["Steam"]["CompatToolMapping"][str(appid)]["name"],
                         "proton_experimental")
        self.assertTrue(steam.add_to_collection(user, "Linux", appid))
        self.assertTrue(steam.add_to_collection(user, "linux", appid))  # same collection, no duplicate
        self.assertEqual(steam.collections(user), ["Linux"])
        entries = dict(json.loads(Path(user.config / "cloudstorage/cloud-storage-namespace-1.json").read_text()))
        body = json.loads(entries["user-collections.uc-linux"]["value"])
        self.assertEqual(body["added"], [appid])
        self.assertEqual(json.loads(Path(user.config / "cloudstorage/cloud-storage-namespace-1.modified.json").read_text()),
                         ["user-collections.uc-linux"])

    def test_apply_moves_and_wraps_args(self):
        from proton_porter import steam
        from proton_porter.analyze import analyze, overrides_string
        from proton_porter.apply import Choices, apply
        a = analyze(self.exe, [])
        fake = steam.Proton("proton_experimental", "Proton - Experimental", self.tmp)
        c = Choices(name="My Game", proton=fake, user=steam.users()[0], move_to=self.tmp / "Games", args="-windowed")
        r = apply(a, overrides_string(a.dlls), c)
        self.assertEqual(r.moved_to, self.tmp / "Games/My Game")
        self.assertFalse((self.tmp / "Old/My Game").exists())
        self.assertEqual(r.exe.name, "MyGame-Win64-Shipping-linux.bat")
        self.assertEqual(r.launch_options, 'WINEDLLOVERRIDES="winmm=n,b" %command%')

    def test_busy_folder_not_moved(self):
        from proton_porter import apply
        root = self.tmp / "Old/My Game"
        self.assertIsNone(apply.folder_busy(root, 0))
        self.assertIn("changed", apply.folder_busy(root, 60))
        import subprocess, sys, time
        writer = subprocess.Popen([sys.executable, "-c",
                                   f"f=open({str(root / 'part.tmp')!r},'w'); import time; time.sleep(30)"])
        try:
            for _ in range(50):
                if (root / "part.tmp").exists():
                    break
                time.sleep(0.05)
            self.assertIn("writing", apply.folder_busy(root, 0) or "")
        finally:
            writer.kill()
            writer.wait()
        apply.MOVE_QUIET_SECONDS = 60
        with self.assertRaises(RuntimeError):
            apply.move_game(root, self.tmp / "Games", lambda *_: None)
        self.assertTrue(root.exists())

    def test_missing_dlls(self):
        from proton_porter import analyze
        analyze._BUILTIN_CACHE = {"kernel32.dll"}
        try:
            (self.exe.parent / "UnityPlayer.dll").write_bytes(b"MZ")
            self.assertEqual(analyze.missing_dlls(self.exe, ["KERNEL32.dll", "UnityPlayer.dll", "GameAssembly.dll"]),
                             ["GameAssembly.dll"])
        finally:
            analyze._BUILTIN_CACHE = None

    def test_remove_and_restore(self):
        from proton_porter import steam
        user = steam.users()[0]
        sc = steam.Shortcut("My Game", self.exe, self.exe.parent)
        appid, _ = steam.write_shortcut(user, sc)
        steam.set_compat_tool(appid, "proton_experimental")
        steam.add_to_collection(user, "Linux", appid)
        saved = steam.backup(user, "before")
        self.assertEqual([e.name for e in steam.library(user)], ["My Game"])
        self.assertEqual(steam.library(user)[0].tool, "proton_experimental")
        self.assertTrue(steam.remove_shortcut(user, appid))
        self.assertEqual(steam.library(user), [])
        entries = dict(json.loads(Path(user.config / "cloudstorage/cloud-storage-namespace-1.json").read_text()))
        self.assertEqual(json.loads(entries["user-collections.uc-linux"]["value"])["added"], [])
        self.assertNotIn(str(appid), steam.compat_mapping())
        steam.restore_backup(user, saved)
        self.assertEqual([e.name for e in steam.library(user)], ["My Game"])

    def test_launch_prefix(self):
        from proton_porter import steam
        from proton_porter.apply import Choices, command_prefix, launch_options
        c = Choices(name="x", proton=steam.Proton("p", "p", self.tmp), user=steam.users()[0],
                    extra_env="DXVK_HUD=fps", mangohud=True, gamemode=True, proton_log=True)
        prefix = command_prefix("winmm=n,b", c)
        self.assertEqual(prefix, 'WINEDLLOVERRIDES="winmm=n,b" PROTON_LOG=1 DXVK_HUD=fps gamemoderun mangohud')
        self.assertEqual(launch_options(prefix, ""), prefix + " %command%")
        self.assertEqual(launch_options("", "-windowed"), "-windowed")

    def test_scan(self):
        from proton_porter.analyze import scan_folder
        tools = self.tmp / "Old/Some Tool"
        tools.mkdir()
        tiny_pe(tools / "tool.exe")
        found = {a.name: likely for a, likely in scan_folder(self.tmp / "Old", [])}
        self.assertEqual(found, {"My Game": True, "Some Tool": False})

    def test_settings_roundtrip(self):
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")
        from proton_porter.config import Settings
        s = Settings.load()
        s.collection, s.mangohud = "Ported", True
        s.save()
        self.assertEqual((Settings.load().collection, Settings.load().mangohud), ("Ported", True))
        self.assertEqual(oct((self.tmp / "cfg/proton-porter/settings.json").stat().st_mode & 0o777), "0o600")

    def test_valve_names(self):
        from proton_porter.steam import _valve_internal
        self.assertEqual(_valve_internal("Proton 11.0"), "proton_11")
        self.assertEqual(_valve_internal("Proton 6.3"), "proton_63")
        self.assertEqual(_valve_internal("Proton - Experimental"), "proton_experimental")

    def test_dotnet_flag(self):
        from proton_porter.pe import PEFile
        p = self.tmp / "net.exe"
        tiny_pe(p, dotnet=True)
        self.assertTrue(PEFile(p).info().is_dotnet)


if __name__ == "__main__":
    unittest.main()
