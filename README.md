# Proton Porter

Add a Windows game you already have on disk to Steam on Linux, with the Proton
fixes already applied. Pick the game's `.exe` and Proton Porter:

- **finds loader DLLs** next to the game (`winmm.dll`, `version.dll`, `dinput8.dll`,
  `winhttp.dll`, `dxgi.dll`... as used by mod loaders, ASI loaders, ReShade,
  BepInEx and similar). Wine ignores these unless told otherwise, which is the
  most common reason a game that worked on Windows does nothing under Proton. It
  sets `WINEDLLOVERRIDES` for them.
- **fixes `.bat` launchers**: a `.bat` that ends in `pause` + `taskkill` closes
  the game at once under Proton. Proton Porter makes a `-linux.bat` copy
  without those lines.
- **can move the game folder** to your games folder (copies, checks the copy,
  then deletes the original). It won't move a folder that is still being
  extracted or copied.
- **spots incomplete folders**: if the `.exe` needs a DLL that is neither next
  to it nor part of Proton (say `UnityPlayer.dll`), it warns you before adding.
- **adds it to Steam** as a non-Steam game, sets the Proton version, adds the
  icon from the `.exe`, and puts it in a collection (default: **Linux**).
- **imports old saves**: it looks in `~/Documents`, `~/Old Windows` (or any
  Windows backup you point it at) for folders named after the game, plus Unity's
  `AppData/LocalLow/<company>/<game>`. It creates the game's Proton prefix, without
  starting the game, and copies the saves into it.
- **tells you what else to expect**: engine, 32/64-bit, anti-cheat, .NET/XNA,
  Visual C++ redistributables, with the `protontricks` command to run if needed.

It also has:

- **Scan folder**: point it at a folder (an old `Program Files`, a games drive)
  and it lists every sub-folder with a game in it, picks the right `.exe` (it
  prefers script-extender loaders like `nvse_loader.exe`), and adds the ticked
  ones in one go.
- **Library**: every non-Steam game in your Steam library, with buttons to open
  its folder or its Proton folder, run Protontricks, reset its Proton folder, or
  remove it from Steam.
- **Per-game switches** for MangoHud, GameMode, the Steam overlay and Proton
  logging (`~/steam-<appid>.log`).
- **Artwork**: covers, banners and logos from SteamGridDB (free API key, set in
  Settings).
- **Settings**: default games folder, Proton version, collection, launch
  switches, extra folders to search for saves, the SteamGridDB key, and backups
  with a Restore button.

Steam rewrites its settings from memory, so Proton Porter closes Steam for a
moment and reopens it. It backs up `shortcuts.vdf`, `config.vdf` and the
collections file to `~/.local/state/proton-porter/backups/<date>` first.

## Install

Arch / Omarchy, from a release:

```sh
tar xf proton-porter-1.1.1.tar.gz
cd proton-porter-1.1.1
./install.sh            # installs the deps with pacman if missing
```

It shows up as **Proton Porter** in the app launcher. `./install.sh --remove`
uninstalls it. There's also a `PKGBUILD` (`makepkg -si`) and a single-file
`proton-porter.pyz` (needs `python-vdf`, `python-gobject`, `gtk4`, `libadwaita`).

## Use

Open it, then **Choose .exe…** (or drag the `.exe` onto the window). Check
what it found, change anything you want, and press **Add to Steam**. **Preview**
shows exactly what will be written.

Command line:

```sh
proton-porter analyze "~/Old Windows/Programmi/Some Game/Game.exe"
proton-porter add GAME.exe --move-to ~/Storage/Games --collection Linux
proton-porter add GAME.exe --args=-windowed --env "DXVK_HUD=fps" --dry-run
proton-porter protons          # Proton versions Steam can use
proton-porter scan ~/Old\ Windows/Programmi   # what a folder scan would find
proton-porter list             # non-Steam games in the library
```

`add` options: `--name`, `--proton <internal name>`, `--dll NAME` / `--no-dll NAME`,
`--search DIR` (another place to look for saves), `--no-saves`, `--no-restart`,
`--mangohud`, `--gamemode`, `--log`, `--artwork`.

## Notes

- Launch arguments: Steam can't combine `VAR=... %command%` with arguments after
  `%command%` for non-Steam games, so when both are needed the arguments go in a
  small `<game>-linux.bat` next to the `.exe`.
- Adding the same `.exe` (or name) again updates the existing shortcut instead of
  making a second one.
- Games with kernel anti-cheat that hasn't been enabled for Proton won't run
  online whatever you do.

## Made by

[Xmilon](https://github.com/Xmilon).

## Development

```sh
python3 -m proton_porter              # run from the source tree
python3 -m unittest discover tests    # tests use a fake Steam folder
scripts/build-release.sh              # dist/*.tar.gz and dist/proton-porter.pyz
```

`PROTON_PORTER_STEAM_ROOT=/some/copy` points it at a copy of a Steam folder; Steam
is never closed or opened in that mode.
