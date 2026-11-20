# What usually works
- **Repacks (FitGirl, DODI):** install them with “Install a repack”. Keep the folder the installer suggests. If an installer fails under Proton, turn on “Run installers with the system's Wine” in Settings.
- **GOG-based releases:** they usually run best. With no Steam API, they have nothing to crack.
- **OnlineFix, new style:** an `OnlineFix64.dll` loaded through `winmm.dll` (Schedule I, for example) works. Steam must be running and logged in. Proton Porter sets the `winmm=n,b` override for you.
- **Steam emulators:** Goldberg (a `steam_settings` folder) and CODEX (`steam_emu.ini`) usually work.
- **Mod loaders:** BepInEx (`winhttp.dll`), MelonLoader (`version.dll`), ASI loaders and ReShade work once their DLL is set to native. Proton Porter does this when it finds them.
- **Script extenders:** with SKSE, F4SE or NVSE, add the loader's `.exe` (for example `nvse_loader.exe`) instead of the game's.
- **Emulator bundles:** Windows emulator packages run under Proton. A native Linux build of the same emulator is usually smoother.

# Often broken
- **OnlineFix, old style:** some builds replace `steam_api.dll` and ship `OnlineFix.dll` plus `steam_api.of` (the SteamRIP Terraria, for example). They show an error box and refuse to load under Wine and every Proton. Use another release.
- **Kernel anti-cheat online:** BattlEye and EasyAntiCheat only work if the developer turned on Proton support. Rainbow Six Siege online doesn't.
- **Rainbow Six Siege Y9S2 with Heated Metal:** it only runs with the Proton-R6HM build.
- **Denuvo-protected games:** results vary, and a crack that works on Windows can still fail under Wine.
- **Installers that need .NET Framework:** installing .NET 4 into a Proton prefix often fails with `0x80070643`. Proton's built-in Mono usually runs .NET games without it.

# When a game won't start
- **Fix it…:** tests the game, applies the fixes its log points to, then tries other Proton versions, a fresh Proton folder and the system's Wine. It keeps the first one that works.
- **Latest log:** shows what went wrong: a missing DLL, the module that crashed, an error box, or the game's own crash log.
- **Log Steam launches:** use this if a game only crashes when started from Steam. Turn it on, play until it crashes, then open “Latest log”.
- **Runs with:** switches between Proton versions, the system's Wine, and native Linux.

# Good to know
- The first start compiles shaders, so expect some stutter for a few minutes.
- A game's Windows folder (saves, settings) is `steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser`. Games set to the system's Wine have their own folder in `~/.local/share/proton-porter/wine-prefixes`.
- Steam has to be closed while Proton Porter changes its files. It closes and reopens by itself, with a backup first.
- Uninstall moves files to the Trash, so you can restore them.
