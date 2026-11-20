"""GTK 4 / libadwaita window: add one game, scan a folder of games, manage the library."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import traceback
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, archive, compat, desktop, doctor, protontools, repack, retry, runner, runners, steam, uninstall, update  # noqa: E402
from .analyze import (Analysis, DllChoice, SaveCandidate, analyze, default_search_roots,  # noqa: E402
                      overrides_string, scan_folder)
from .apply import Choices, Job, apply_many, data_dir, describe, parse_env  # noqa: E402
from .config import MAKER, MAKER_URL, REPO_URL, Settings  # noqa: E402

APP_ID = "io.github.xmilon.ProtonPorter"
SAVE_PLACES = ["Documents/My Games", "Documents", "Saved Games", "AppData/Local", "AppData/LocalLow", "AppData/Roaming"]


def esc(text: str) -> str:
    return GLib.markup_escape_text(str(text))


def asset_bytes(name: str) -> bytes:
    return resources.files("proton_porter").joinpath("assets", name).read_bytes()


def maker_texture() -> Gdk.Texture | None:
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(asset_bytes("maker-avatar.jpg")))
    except (GLib.Error, OSError, FileNotFoundError):
        return None


def install_app_icon() -> None:
    """Make the app icon findable even when running from the source tree or the .pyz."""
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not theme.has_icon("applications-games-symbolic"):
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not theme.has_icon(APP_ID):
        folder = data_dir() / "icon"
        folder.mkdir(exist_ok=True)
        (folder / f"{APP_ID}.svg").write_bytes(asset_bytes("app-icon.svg"))
        theme.add_search_path(str(folder))


def open_uri(uri: str) -> None:
    Gio.AppInfo.launch_default_for_uri(uri, None)


def open_path(path: Path) -> None:
    open_uri(Gio.File.new_for_path(str(path)).get_uri())


def tool_installed(name: str) -> bool:
    return shutil.which(name) is not None


def maker_button() -> Gtk.Widget:
    """“Made by Xmilon” with the GitHub avatar, linking to the profile."""
    box = Gtk.Box(spacing=8)
    avatar = Adw.Avatar(size=28, text=MAKER, show_initials=True)
    tex = maker_texture()
    if tex:
        avatar.set_custom_image(tex)
    box.append(avatar)
    label = Gtk.Label(label=f"Made by {MAKER}")
    label.add_css_class("dim-label")
    box.append(label)
    btn = Gtk.Button(child=box, halign=Gtk.Align.CENTER, tooltip_text=MAKER_URL)
    btn.add_css_class("flat")
    btn.connect("clicked", lambda *_: open_uri(MAKER_URL))
    return btn


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Proton Porter", default_width=720, default_height=860,
                         icon_name=APP_ID)
        install_app_icon()
        self.settings = Settings.load()
        self.analysis: Analysis | None = None
        self.protons = steam.protons()
        self.runners = runners.all_runners()  # Protons + System Wine + Native Linux
        try:
            self.users = steam.users()
        except FileNotFoundError:
            self.users = []

        # Root: the tabbed UI, or a full-window progress / result page.
        self.root = Gtk.Stack()
        self.toasts = Adw.ToastOverlay(child=self.root)
        self.set_content(self.toasts)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.tabs = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.tabs, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        menu.append("Compatibility guide", "win.guide")
        menu.append("Check for updates", "win.update")
        menu.append("Settings", "win.settings")
        menu.append(f"{MAKER} on GitHub", "win.github")
        menu.append("About Proton Porter", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        view.add_top_bar(header)
        view.set_content(self.tabs)
        for name, cb in (("settings", self.on_settings), ("about", self.on_about),
                         ("update", lambda *_: self.check_update(True)),
                         ("guide", self.on_guide),
                         ("github", lambda *_: open_uri(MAKER_URL))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        app.set_accels_for_action("win.settings", ["<Control>comma"])

        self.add_stack = Gtk.Stack()
        self.add_stack.add_named(self.welcome_page(), "welcome")
        self.tabs.add_titled_with_icon(self.add_stack, "add", "Add game", "list-add-symbolic")
        self.scan_stack = Gtk.Stack()
        self.scan_stack.add_named(self.scan_intro(), "intro")
        self.tabs.add_titled_with_icon(self.scan_stack, "scan", "Scan folder", "system-search-symbolic")
        self.library_stack = Gtk.Stack()
        self.tabs.add_titled_with_icon(self.library_stack, "library", "Library", "view-grid-symbolic")
        self.tabs.connect("notify::visible-child-name",
                          lambda s, _p: s.get_visible_child_name() == "library" and self.refresh_library())

        self.root.add_named(view, "main")
        self.root.add_named(self.working_page(), "working")
        self.done_page = Adw.StatusPage(icon_name="object-select-symbolic", title="Added to Steam")
        self.root.add_named(self.done_page, "done")

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)

        if self.settings.check_updates:
            GLib.timeout_add_seconds(3, lambda: self.check_update(False) or False)
        if not self.users and not self.protons:
            self.error("Steam isn't set up", "Install Steam, log in once, and install a Proton version "
                       "(Library > search “Proton”). Then start Proton Porter again.")
        elif not self.protons:
            self.error("No Proton found", "Install Proton from Steam first: Library > search “Proton” > Install.")
        elif not self.users:
            self.error("No Steam account found", "Open Steam and log in once, then start Proton Porter again.")

    # ------------------------------------------------------------- helpers

    @property
    def user(self) -> steam.SteamUser | None:
        return self.users[0] if self.users else None

    def search_roots(self) -> list[Path]:
        return default_search_roots() + [Path(p) for p in self.settings.search_folders if Path(p).is_dir()]

    def default_proton_index(self) -> int:
        """Index into self.runners: Native for Linux programs, else the configured or best Proton."""
        want = runners.NATIVE if self.analysis is not None and self.analysis.native else self.settings.proton
        for i, t in enumerate(self.runners):
            if t.internal == want:
                return i
        best = steam.default_proton(self.protons)
        return self.runners.index(best) if best in self.runners else 0

    def error(self, heading: str, body: str) -> None:
        alert = compat.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", "OK")
        alert.present(self)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def confirm(self, heading: str, body: str, action: str, callback, destructive: bool = False) -> None:
        alert = compat.AlertDialog(heading=heading, body=body)
        alert.add_response("cancel", "Cancel")
        alert.add_response("go", action)
        alert.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                      else Adw.ResponseAppearance.SUGGESTED)
        alert.connect("response", lambda _a, r: r == "go" and callback())
        alert.present(self)

    def replace(self, stack: Gtk.Stack, name: str, child: Gtk.Widget) -> None:
        old = stack.get_child_by_name(name)
        if old:
            stack.remove(old)
        stack.add_named(child, name)
        stack.set_visible_child_name(name)

    # ------------------------------------------------------------- add tab

    def welcome_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name=APP_ID, title="Port a Windows game",
                              description="Choose the game's .exe (or the .bat it starts from), or drop it here. "
                                          "Proton Porter works out the fixes and adds it to Steam.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, halign=Gtk.Align.CENTER)
        btn = Gtk.Button(label="Choose .exe…", halign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", self.on_choose)
        box.append(btn)
        arc = Gtk.Button(label="Install from archive (.rar, .7z, .zip)…", halign=Gtk.Align.CENTER)
        arc.add_css_class("pill")
        arc.connect("clicked", self.on_choose_archive)
        box.append(arc)
        rpk = Gtk.Button(label="Install a repack (FitGirl, DODI)…", halign=Gtk.Align.CENTER)
        rpk.add_css_class("pill")
        rpk.connect("clicked", self.on_choose_repack)
        box.append(rpk)
        box.append(maker_button())
        page.set_child(box)
        return page

    def on_choose_repack(self, *_):
        dialog = Gtk.FileDialog(title="Choose the repack's folder (the one with setup.exe)")
        start = Path.home() / "Downloads"
        if start.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def done(dlg, res):
            try:
                folder = Path(dlg.select_folder_finish(res).get_path())
            except GLib.Error:
                return
            rp = repack.detect(folder)
            if rp:
                self.install_repack(rp)
            else:
                self.error("Not a repack", f"{folder.name} has no setup.exe with .bin files next to it.")
        dialog.select_folder(self, None, done)

    def on_choose_archive(self, *_):
        dialog = Gtk.FileDialog(title="Choose the game's archive")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Archives")
        for suffix in archive.ARCHIVE_SUFFIXES:
            f.add_suffix(suffix.lstrip("."))
        filters.append(f)
        dialog.set_filters(filters)
        dialog.open(self, None, self._archive_chosen)

    def _archive_chosen(self, dialog, result):
        try:
            path = Path(dialog.open_finish(result).get_path())
        except GLib.Error:
            return
        self.install_archive(path)

    def install_repack(self, rp: "repack.Repack") -> None:
        games = Path(self.settings.games_dir).expanduser()
        tool = (runners.pseudo(runners.WINE) if self.settings.repack_use_wine else None) \
            or next((t for t in self.protons if t.internal == self.settings.proton), None) \
            or steam.default_proton(self.protons)
        if tool is None:
            self.error("No Proton found", "Install Proton from Steam first.")
            return

        def go():
            def done(res):
                if isinstance(res, Exception):
                    self.error(f"Couldn't install {rp.name}", str(res))
                    return
                from .analyze import best_exe
                exe = best_exe(res)
                if exe:
                    self.toast(f"Installed {rp.name}")
                    self.load(exe)
                else:
                    self.error("No game found", f"The installer put files in {res}, but no game .exe was found.")
            self.run_task(f"Installing {rp.name}",
                          lambda progress: repack.install(rp, games, tool, progress, self.settings.repack_check_md5), done)
        self.confirm(f"Install the {rp.kind} repack “{rp.name}”?",
                     ("Its files are checked against its MD5 list first. " if rp.md5_file else "")
                     + f"Then its installer opens: click through it. The install folder is preset to "
                     f"{games / rp.name}; keep it, so Proton Porter can find the game afterwards. "
                     "Untick DirectX / Visual C++ extras: Proton has its own.", "Install", go)

    def install_archive(self, path: Path) -> None:
        """Extract into <games folder>/<game>, verify, then open the Add form for it."""
        games = Path(self.settings.games_dir).expanduser()

        def done(res):
            if isinstance(res, Exception):
                self.error("Couldn't install the archive", str(res))
                return
            from .analyze import best_exe
            exe = best_exe(res)
            self.toast(f"Extracted and verified {res.name}")
            if exe:
                self.load(exe)
            else:
                self.error("No game found", f"{res} was extracted, but no game .exe was found in it.")
        self.run_task(f"Extracting {path.name}",
                      lambda progress: archive.install_game_archive(path, games, progress), done)

    def on_choose(self, *_):
        dialog = Gtk.FileDialog(title="Choose the game's .exe or archive")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Games (.exe, .bat, a repack's setup.exe, or an archive: .zip .rar .7z .tar.*)")
        for pattern in ("*.exe", "*.EXE", "*.bat", "*.BAT", "*.cmd"):
            f.add_pattern(pattern)
        for suffix in archive.ARCHIVE_SUFFIXES:
            f.add_suffix(suffix.lstrip("."))
        filters.append(f)
        dialog.set_filters(filters)
        start = next((p for p in (Path("~/Old Windows").expanduser(), Path(self.settings.games_dir), Path.home())
                      if p.is_dir()), Path.home())
        dialog.set_initial_folder(Gio.File.new_for_path(str(start)))
        dialog.open(self, None, self._chosen)

    def _chosen(self, dialog, result):
        try:
            f = dialog.open_finish(result)
        except GLib.Error:
            return
        self.load(Path(f.get_path()))

    def on_drop(self, _target, value, *_):
        files = value.get_files()
        if not files:
            return False
        path = Path(files[0].get_path())
        if path.is_dir() and repack.detect(path):
            self.load(path)
        elif path.is_dir():
            self.tabs.set_visible_child_name("scan")
            self.start_scan(path)
        else:
            self.load(path)
        return True

    def load(self, exe: Path, analysis: Analysis | None = None) -> None:
        if analysis is None and (exe.is_dir() or exe.name.lower() == "setup.exe"):
            rp = repack.detect(exe)
            if rp:
                self.install_repack(rp)
                return
        if archive.is_archive(exe):
            self.install_archive(exe)
            return
        try:
            self.analysis = analysis or analyze(exe, self.search_roots())
        except Exception as error:  # noqa: BLE001
            self.error("Couldn't read that file", str(error))
            return
        self.replace(self.add_stack, "form", self.form_page(self.analysis))
        self.tabs.set_visible_child_name("add")

    def form_page(self, a: Analysis) -> Gtk.Widget:
        s = self.settings
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Game", description=esc(f"Engine: {a.engine}"))
        exe_row = Adw.ActionRow(title="Program", subtitle=esc(a.exe), subtitle_selectable=True)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self.on_choose)
        exe_row.add_suffix(change)
        g.add(exe_row)
        if a.other_exes:
            alt = Adw.ComboRow(title="Use another .exe", subtitle="Other programs in the folder")
            alt.set_model(Gtk.StringList.new(["(keep this one)"] + [str(p.relative_to(a.root)) for p in a.other_exes]))
            alt.connect("notify::selected",
                        lambda r, _p: r.get_selected() and self.load(a.other_exes[r.get_selected() - 1]))
            g.add(alt)
        self.name_row = Adw.EntryRow(title="Name in Steam", text=a.name)
        g.add(self.name_row)
        if self.user and any(e.exe == a.exe for e in steam.library(self.user)):
            g.add(Adw.ActionRow(title="Already in Steam", subtitle="Adding it again updates the existing entry."))
        page.add(g)

        if a.warnings or a.notes:
            n = Adw.PreferencesGroup(title="What it found")
            for text, icon in [(w, "dialog-warning-symbolic") for w in a.warnings] + \
                              [(t, "dialog-information-symbolic") for t in a.notes]:
                r = Adw.ActionRow(subtitle=esc(text), subtitle_lines=0)
                r.add_prefix(Gtk.Image(icon_name=icon))
                n.add(r)
            page.add(n)

        games_dir = Path(s.games_dir)
        loc = Adw.PreferencesGroup(title="Folder", description=esc(f"Now in {a.root.parent}"))
        self.move_row = Adw.SwitchRow(title="Move to my games folder", subtitle=esc(games_dir))
        inside = str(a.root.resolve()).startswith(str(games_dir.resolve()) + "/")
        self.move_row.set_active(s.move_by_default and not inside)
        self.move_row.set_sensitive(not inside)
        loc.add(self.move_row)
        page.add(loc)

        p = Adw.PreferencesGroup(title="Running it")
        self.proton_row = Adw.ComboRow(title="Runs with", subtitle="A Proton version, the system's Wine, or none")
        self.proton_row.set_model(Gtk.StringList.new([t.display for t in self.runners] or ["(none installed)"]))
        self.proton_row.set_selected(self.default_proton_index())
        p.add(self.proton_row)
        self.args_row = Adw.EntryRow(title="Game arguments (optional)")
        p.add(self.args_row)
        emu = a.emulator
        self.emu_row = None
        if emu is not None and emu.ui_exe != a.exe:
            self.emu_row = Adw.SwitchRow(title="Also add the emulator itself",
                                         subtitle=esc(f"A second Steam entry, “{a.name} (emulator)”, opens "
                                                      f"{emu.ui_exe.name} for its settings and other games"),
                                         active=True)
            p.add(self.emu_row)
        self.env_row = Adw.EntryRow(title="Extra variables, e.g. DXVK_HUD=fps (optional)", text=s.extra_env)
        p.add(self.env_row)
        self.overlay_row = Adw.SwitchRow(title="Steam overlay", active=s.overlay,
                                         subtitle="Turn off if the game crashes with it")
        p.add(self.overlay_row)
        has_mango, has_gm = tool_installed("mangohud"), tool_installed("gamemoderun")
        self.mango_row = Adw.SwitchRow(title="MangoHud", subtitle="FPS and temperatures on screen" if has_mango
                                       else "Not installed", active=s.mangohud and has_mango, sensitive=has_mango)
        p.add(self.mango_row)
        self.gamemode_row = Adw.SwitchRow(title="GameMode", subtitle="CPU performance mode while playing" if has_gm
                                          else "Not installed", active=s.gamemode and has_gm, sensitive=has_gm)
        p.add(self.gamemode_row)
        self.log_row = Adw.SwitchRow(title="Proton log", active=s.proton_log,
                                     subtitle=esc("Writes ~/steam-<appid>.log, for when it won't start"))
        p.add(self.log_row)
        page.add(p)

        d = Adw.PreferencesGroup(title="Loader DLLs",
                                 description="DLLs next to the game that replace Windows ones (game and mod loaders, "
                                             "ReShade, fixes). On means the game's copy is used instead of Wine's.")
        self.dll_group = d
        self.dll_rows: list[tuple[DllChoice, Adw.SwitchRow]] = []
        if not a.dlls:
            d.add(Adw.ActionRow(title="None found", subtitle="Nothing to override"))
        for dll in a.dlls:
            self.add_dll_row(dll)
        extra = Adw.EntryRow(title="Add another DLL by name", show_apply_button=True)
        extra.connect("apply", self.on_add_dll)
        d.add(extra)
        page.add(d)

        sv = Adw.PreferencesGroup(title="Saves",
                                  description="Copied into the game's own Proton folder. The originals stay where they are.")
        self.saves_group = sv
        self.save_rows: list[tuple[SaveCandidate, Adw.SwitchRow]] = []
        for cand in a.saves:
            self.add_save_row(cand, s.import_saves)
        if not a.saves:
            sv.add(Adw.ActionRow(title="No old saves found",
                                 subtitle="Add a folder below, or add search folders in Settings"))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START, margin_top=6)
        add_save = Gtk.Button(label="Add save folder…")
        add_save.connect("clicked", self.on_add_save)
        buttons.append(add_save)
        sv.add(buttons)
        page.add(sv)

        st = Adw.PreferencesGroup(title="Steam")
        self.collection_row = Adw.EntryRow(title="Collection", text=s.collection)
        st.add(self.collection_row)
        has_key = bool(s.steamgriddb_key)
        self.art_row = Adw.SwitchRow(title="Artwork from SteamGridDB",
                                     subtitle="Cover, banner and logo" if has_key else "Add an API key in Settings first",
                                     active=has_key and s.fetch_artwork, sensitive=has_key)
        st.add(self.art_row)
        page.add(st)

        bar = Gtk.ActionBar()
        prev = Gtk.Button(label="Preview")
        prev.connect("clicked", self.on_preview)
        bar.pack_start(prev)
        add = Gtk.Button(label="Add to Steam")
        add.add_css_class("suggested-action")
        add.connect("clicked", self.on_add)
        bar.pack_end(add)
        view = Adw.ToolbarView(content=page)
        view.add_bottom_bar(bar)
        return view

    def add_dll_row(self, dll: DllChoice) -> None:
        r = Adw.SwitchRow(title=esc(f"{dll.name}.dll"), subtitle=esc(dll.reason), active=dll.enabled)
        self.dll_group.add(r)
        self.dll_rows.append((dll, r))

    def on_add_dll(self, row: Adw.EntryRow):
        name = row.get_text().strip().lower().removesuffix(".dll")
        if name and not any(d.name == name for d, _ in self.dll_rows):
            dll = DllChoice(name, "Added by you", True)
            self.analysis.dlls.append(dll)
            self.add_dll_row(dll)
        row.set_text("")

    def add_save_row(self, cand: SaveCandidate, active: bool = True) -> None:
        r = Adw.SwitchRow(title=esc(cand.source), subtitle_lines=0, active=active,
                          subtitle=esc("→ C:\\users\\steamuser\\" + cand.dest.replace("/", "\\")))
        self.saves_group.add(r)
        self.save_rows.append((cand, r))

    def on_add_save(self, *_):
        dialog = Gtk.FileDialog(title="Folder with the saves")

        def done(dlg, res):
            try:
                src = Path(dlg.select_folder_finish(res).get_path())
            except GLib.Error:
                return
            self.ask_save_dest(src)
        dialog.select_folder(self, None, done)

    def ask_save_dest(self, src: Path) -> None:
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        place = Adw.ComboRow(title="Goes in")
        place.set_model(Gtk.StringList.new(SAVE_PLACES))
        sub = Adw.EntryRow(title="Folder name there", text=src.name)
        box.append(place)
        box.append(sub)
        alert = compat.AlertDialog(heading="Where did the game keep these?",
                                body="On Windows, under C:\\Users\\<you>\\. PCGamingWiki lists it for most games.")
        alert.set_extra_child(box)
        alert.add_response("cancel", "Cancel")
        alert.add_response("ok", "Add")
        alert.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def respond(_a, resp):
            if resp == "ok" and sub.get_text().strip():
                cand = SaveCandidate(src, f"{SAVE_PLACES[place.get_selected()]}/{sub.get_text().strip()}", "Added by you")
                self.analysis.saves.append(cand)
                self.add_save_row(cand)
        alert.connect("response", respond)
        alert.present(self)

    def form_choices(self) -> tuple[str, Choices]:
        a = self.analysis
        for dll, row in self.dll_rows:
            dll.enabled = row.get_active()
        name = self.name_row.get_text().strip()
        if not name:
            raise ValueError("The game needs a name.")
        if not self.runners or not self.user:
            raise ValueError("Steam or Proton isn't set up.")
        s = self.settings
        c = Choices(
            name=name,
            proton=self.runners[self.proton_row.get_selected()],
            user=self.user,
            move_to=Path(s.games_dir) if self.move_row.get_active() else None,
            args=self.args_row.get_text(),
            extra_env=parse_env(self.env_row.get_text()),
            collection=self.collection_row.get_text(),
            saves=[cand for cand, row in self.save_rows if row.get_active()],
            overlay=self.overlay_row.get_active(),
            restart_steam=s.reopen_steam,
            mangohud=self.mango_row.get_active(),
            gamemode=self.gamemode_row.get_active(),
            proton_log=self.log_row.get_active(),
            artwork_key=s.steamgriddb_key if self.art_row.get_active() else "",
        )
        return overrides_string(a.dlls), c

    def on_preview(self, *_):
        try:
            dlls, c = self.form_choices()
        except ValueError as error:
            self.error("Check the form", str(error))
            return
        label = Gtk.Label(label=describe(self.analysis, dlls, c), selectable=True, xalign=0, wrap=True)
        label.add_css_class("monospace")
        alert = compat.AlertDialog(heading="This is what will happen")
        alert.set_extra_child(label)
        alert.add_response("close", "Close")
        alert.present(self)

    def on_add(self, *_):
        try:
            dlls, c = self.form_choices()
        except ValueError as error:
            self.error("Check the form", str(error))
            return
        jobs = [Job(self.analysis, dlls, c)]
        emu = self.analysis.emulator
        if self.emu_row is not None and self.emu_row.get_active():
            from dataclasses import replace
            ea = analyze(emu.ui_exe, [])
            # same folder, so no second move; same Proton and options as the game
            jobs.append(Job(ea, overrides_string(ea.dlls), replace(c, name=f"{c.name} (emulator)", move_to=None,
                                                                    saves=[], args="")))
        self.run_jobs(jobs)

    # ------------------------------------------------------------ scan tab

    def scan_intro(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name="folder-symbolic", title="Add a whole folder of games",
                              description="Pick a folder like your old Windows “Program Files” or a games folder. "
                                          "Each sub-folder with a game in it is listed; tick the ones to add.")
        btn = Gtk.Button(label="Choose folder…", halign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", self.on_scan_choose)
        page.set_child(btn)
        return page

    def on_scan_choose(self, *_):
        dialog = Gtk.FileDialog(title="Folder with games")
        start = next((p for p in (Path("~/Old Windows/Programmi").expanduser(), Path("~/Old Windows").expanduser(),
                                  Path(self.settings.games_dir)) if p.is_dir()), Path.home())
        dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def done(dlg, res):
            try:
                self.start_scan(Path(dlg.select_folder_finish(res).get_path()))
            except GLib.Error:
                pass
        dialog.select_folder(self, None, done)

    def start_scan(self, folder: Path) -> None:
        wait = Adw.StatusPage(title="Looking for games…", description=esc(folder))
        wait.set_child(compat.Spinner(width_request=32, height_request=32))
        self.replace(self.scan_stack, "results", wait)
        roots = self.search_roots()

        def work():
            try:
                found = scan_folder(folder, roots)
            except Exception as error:  # noqa: BLE001
                GLib.idle_add(self.error, "Couldn't scan that folder", str(error))
                found = []
            GLib.idle_add(self.show_scan, folder, found)
        threading.Thread(target=work, daemon=True).start()

    def show_scan(self, folder: Path, found: list[tuple[Analysis, bool]]) -> None:
        known = {e.exe for e in steam.library(self.user)} if self.user else set()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=esc(folder.name or folder),
                                     description=f"{len(found)} program(s) found. Likely games are ticked; check the rest.")
        rows: list[tuple[Analysis, Adw.SwitchRow]] = []
        for a, likely in found:
            already = a.exe in known
            bits = [a.engine, str(a.exe.relative_to(folder))]
            on = [d.name for d in a.dlls if d.enabled]
            if on:
                bits.append("DLLs: " + ", ".join(on))
            if a.saves:
                bits.append(f"{len(a.saves)} save folder(s)")
            if already:
                bits.insert(0, "already in Steam")
            row = Adw.SwitchRow(title=esc(a.name), subtitle=esc("  ·  ".join(bits)),
                                subtitle_lines=2, active=likely and not already)
            details = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Open in the Add tab to fine-tune")
            details.add_css_class("flat")
            details.connect("clicked", lambda _b, an=a: self.load(an.exe, an))
            row.add_suffix(details)
            group.add(row)
            rows.append((a, row))
        if not found:
            group.add(Adw.ActionRow(title="Nothing found", subtitle="No .exe files in the sub-folders"))
        page.add(group)

        bar = Gtk.ActionBar()
        again = Gtk.Button(label="Other folder…")
        again.connect("clicked", self.on_scan_choose)
        bar.pack_start(again)
        add = Gtk.Button(label="Add selected")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda *_: self.add_scanned([a for a, r in rows if r.get_active()]))
        bar.pack_end(add)
        view = Adw.ToolbarView(content=page)
        view.add_bottom_bar(bar)
        self.replace(self.scan_stack, "results", view)

    def add_scanned(self, games: list[Analysis]) -> None:
        if not games:
            self.toast("Nothing selected")
            return
        if not self.protons or not self.user:
            self.error("Not ready", "Steam or Proton isn't set up.")
            return
        s = self.settings
        chosen = next((t for t in self.runners if t.internal == s.proton), None) or steam.default_proton(self.protons)
        jobs = []
        for a in games:
            inside = str(a.root.resolve()).startswith(str(Path(s.games_dir).resolve()) + "/")
            proton = runners.pseudo(runners.NATIVE) if a.native else chosen
            c = Choices(name=a.name, proton=proton, user=self.user,
                        move_to=Path(s.games_dir) if s.move_by_default and not inside else None,
                        extra_env=s.extra_env, collection=s.collection,
                        saves=a.saves if s.import_saves else [], overlay=s.overlay, restart_steam=s.reopen_steam,
                        mangohud=s.mangohud and tool_installed("mangohud"),
                        gamemode=s.gamemode and tool_installed("gamemoderun"), proton_log=s.proton_log,
                        artwork_key=s.steamgriddb_key if s.fetch_artwork else "")
            jobs.append(Job(a, overrides_string(a.dlls), c))
        self.run_jobs(jobs)

    # ---------------------------------------------------------- library tab

    def refresh_library(self) -> None:
        if not self.user:
            self.replace(self.library_stack, "list", Adw.StatusPage(title="No Steam account found"))
            return
        entries = steam.library(self.user)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Non-Steam games",
                                     description="Everything added outside the Steam store, by Proton Porter or by hand.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh", valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh_library())
        group.set_header_suffix(refresh)
        names = {t.internal: t.display for t in self.runners}
        for e in entries:
            row = Adw.ExpanderRow(title=esc(e.name),
                                  subtitle=esc(names.get(e.tool, e.tool) or "No Proton set (runs natively)"))
            icon = Path(e.icon) if e.icon else data_dir() / "icons" / f"{e.appid}.png"
            if icon.suffix.lower() in (".png", ".jpg", ".jpeg", ".ico", ".svg") and icon.is_file():
                row.add_prefix(Gtk.Image(file=str(icon), pixel_size=32))
            else:  # same indent for every row
                row.add_prefix(Gtk.Image(icon_name="applications-games-symbolic", pixel_size=32))
            play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Play", sensitive=e.exe.is_file())
            play.add_css_class("flat")
            play.connect("clicked", lambda _b, e=e: self.play_game(e))
            row.add_suffix(play)
            row.add_row(Adw.ActionRow(title="Program", subtitle=esc(e.exe), subtitle_selectable=True))
            choices = [t for t in self.runners if t.internal != runners.NATIVE or e.tool == runners.NATIVE
                       or runners.is_native(e.exe)]
            runs = Adw.ComboRow(title="Runs with", model=Gtk.StringList.new([t.display for t in choices]))
            current = next((i for i, t in enumerate(choices) if t.internal == e.tool), None)
            if current is not None:
                runs.set_selected(current)
            runs.connect("notify::selected",
                         lambda r, _p, e=e, choices=choices, current=current:
                         r.get_selected() != current and self.change_runner(e, choices[r.get_selected()]))
            row.add_row(runs)
            launcher = Adw.SwitchRow(title="Show in the app launcher", subtitle="With the game's icon; starts it through Steam",
                                     active=desktop.exists(e.appid))
            launcher.connect("notify::active", lambda r, _p, e=e: self.toggle_launcher(e, r.get_active()))
            row.add_row(launcher)
            logging_on = runner.steam_logging(e.launch_options)
            slog = Adw.SwitchRow(title="Log Steam launches",
                                 subtitle="Keeps a Proton log when you start it from Steam; see “Latest log”",
                                 active=logging_on, sensitive=not runners.is_pseudo(e.tool))
            slog.connect("notify::active", lambda r, _p, e=e, was=logging_on:
                         r.get_active() != was and self.toggle_steam_log(e, r.get_active()))
            row.add_row(slog)
            if e.launch_options:
                row.add_row(Adw.ActionRow(title="Launch options", subtitle=esc(e.launch_options),
                                          subtitle_selectable=True))
            actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5,
                                  column_spacing=6, row_spacing=6, margin_top=8, margin_bottom=8,
                                  margin_start=12, margin_end=12)
            prefix = steam.prefix_dir(e.appid)
            for label, cb, css, ok in (
                ("Play", lambda e=e: self.play_game(e), "suggested-action", e.exe.is_file()),
                ("Latest log", lambda e=e: self.show_latest_log(e), None, True),
                ("Game folder", lambda e=e: open_path(e.start_dir), None, e.start_dir.is_dir()),
                ("Proton folder", lambda p=prefix: open_path(p / "pfx/drive_c"), None, (prefix / "pfx/drive_c").is_dir()),
                ("Fix it…", lambda e=e: self.fix_game(e), "suggested-action", e.exe.is_file()),
                ("Test & diagnose", lambda e=e: self.test_game(e), None, e.exe.is_file()),
                ("Try other Protons", lambda e=e: self.retry_game(e), None, e.exe.is_file()),
                ("Protontricks", lambda e=e: self.protontricks(e), None, tool_installed("protontricks")),
                ("Reset Proton folder", lambda e=e: self.reset_prefix(e), None, prefix.is_dir()),
                ("Remove from Steam", lambda e=e: self.remove_game(e), None, True),
                ("Uninstall…", lambda e=e: self.uninstall_game(e), "destructive-action", True),
            ):
                b = Gtk.Button(label=label, sensitive=ok)
                if css:
                    b.add_css_class(css)
                b.connect("clicked", lambda _b, cb=cb: cb())
                actions.append(b)
            holder = Adw.PreferencesRow(activatable=False)
            holder.set_child(actions)
            row.add_row(holder)
            group.add(row)
        if not entries:
            group.add(Adw.ActionRow(title="No non-Steam games yet"))
        page.add(group)
        self.replace(self.library_stack, "list", page)

    def check_update(self, manual: bool) -> None:
        """Ask GitHub's main branch for a newer version; on yes, download and install it."""
        def work():
            try:
                newer, err = update.check(), None
            except Exception as error:  # noqa: BLE001
                newer, err = None, str(error)

            def ui():
                if err:
                    manual and self.error("Couldn't check for updates", err)
                elif newer:
                    self.confirm(f"Version {newer} is available", f"You have {__version__}. Download it from GitHub "
                                 "and install it now? Restart Proton Porter afterwards.", "Update", self.do_update)
                elif manual:
                    self.toast(f"Proton Porter {__version__} is up to date")
                return False
            GLib.idle_add(ui)
        threading.Thread(target=work, daemon=True).start()

    def do_update(self) -> None:
        def done(res):
            if isinstance(res, Exception):
                self.error("Update failed", str(res))
            elif res:
                self.error(f"Updated to {res}", "Close and reopen Proton Porter to use it.")
            else:
                self.toast("Already up to date")
        self.run_task("Updating Proton Porter", lambda progress: update.update(progress), done)

    def play_game(self, e: steam.LibraryEntry) -> None:
        """Run the game with a Proton log; if it crashes the log is kept and shown."""
        try:
            spec = runner.spec_for(e, self.runners)
        except RuntimeError as err:
            self.error("Can't start", str(err))
            return
        self.toast(f"Starting {e.name}")

        def work():
            try:
                res = runner.play(spec)
            except Exception as err:  # noqa: BLE001
                res = err

            def ui():
                if isinstance(res, Exception):
                    self.error(f"Couldn't start {e.name}", str(res))
                elif not res.survived:
                    self.error(f"{e.name} crashed", "\n".join(res.diagnosis.summary())
                               + f"\n\nExit code {res.exit_code}. Log saved:\n{res.log}")
                return False
            GLib.idle_add(ui)
        threading.Thread(target=work, daemon=True).start()

    def show_latest_log(self, e: steam.LibraryEntry) -> None:
        text, folder = runner.latest_report(e)
        alert = compat.AlertDialog(heading=f"{e.name}: latest log", body=text)
        alert.add_response("copy", "Copy")
        alert.add_response("open", "Open folder")
        alert.add_response("ok", "OK")
        alert.set_default_response("ok")

        def respond(_a, r):
            if r == "copy":
                self.get_clipboard().set(text)
                self.toast("Copied")
            elif r == "open" and folder:
                open_path(folder)
        alert.connect("response", respond)
        alert.present(self)

    def run_task(self, label: str, fn, done) -> None:
        """Run fn(progress) off the UI thread behind the progress page; done(result or Exception) runs after."""
        self.root.set_visible_child_name("working")
        self.work_label.set_label(label)
        self.work_bar.set_fraction(0)

        def progress(msg, frac):
            def ui():
                self.work_label.set_label(msg)
                if frac is None:
                    self.work_bar.pulse()
                else:
                    self.work_bar.set_fraction(frac)
                return False
            GLib.idle_add(ui)

        def work():
            try:
                res = fn(progress)
            except Exception as err:  # noqa: BLE001
                res = err

            def ui():
                self.root.set_visible_child_name("main")
                done(res)
                return False
            GLib.idle_add(ui)
        threading.Thread(target=work, daemon=True).start()

    def test_game(self, e: steam.LibraryEntry) -> None:
        ws, secs = self.settings.test_workspace, self.settings.test_seconds

        def go():
            def done(res):
                if isinstance(res, Exception):
                    self.error("Test didn't run", str(res))
                    return
                body = "\n".join(res.diagnosis.summary()) + (f"\n\nLog: {res.log}" if res.log else "")
                self.error(("Still running: " if res.survived else "Stopped: ") + res.line(), body)
            self.run_task(f"Testing {e.name}",
                          lambda progress: runner.run_test(runner.spec_for(e, self.runners), ws, secs, progress), done)
        self.confirm(f"Test {e.name}?", f"Starts the game {runner.where(ws)} with a log, waits {secs} seconds, "
                     "stops it and reads the log.", "Start", go)

    def retry_game(self, e: steam.LibraryEntry) -> None:
        ws, secs = self.settings.test_workspace, self.settings.test_seconds

        def go():
            def done(rep):
                if isinstance(rep, Exception):
                    self.error("Auto-fix didn't run", str(rep))
                    return
                lines = [("✓ " if a.result.survived else "✗ ") + a.result.line()
                         + (" (fresh prefix)" if a.fresh_prefix else "") for a in rep.attempts]
                if rep.winner:
                    lines.append(f"\nKept: {rep.winner.proton.display}.")
                lines += rep.notes
                self.error("Found a setup that works" if rep.winner else "Nothing survived", "\n".join(lines))
                self.refresh_library()
            self.run_task(f"Trying other Proton versions for {e.name}",
                          lambda progress: retry.retry(e, self.user, self.protons, ws, secs, 6, True, progress), done)
        self.confirm(f"Try other Proton versions for {e.name}?",
                     f"Starts the game {runner.where(ws)} with different Proton versions and a fresh Proton folder "
                     f"(the old one is kept), {secs} seconds each, and keeps the first that stays up. "
                     "Steam closes and reopens when a winner is saved.", "Start", go)

    def protontricks(self, e: steam.LibraryEntry) -> None:
        if not steam.prefix_dir(e.appid).is_dir():
            self.error("Start the game once first", "Proton creates the game's Windows folder on the first start.")
            return
        subprocess.Popen(["protontricks", "--gui", str(e.appid)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def reset_prefix(self, e: steam.LibraryEntry) -> None:
        def go():
            shutil.rmtree(steam.prefix_dir(e.appid), ignore_errors=True)
            self.toast(f"Reset {e.name}'s Proton folder")
            self.refresh_library()
        self.confirm(f"Reset {e.name}'s Proton folder?",
                     "Deletes its Windows folder (settings, protontricks installs, and any saves kept there). "
                     "Proton makes a fresh one on the next start. The game itself isn't touched.",
                     "Reset", go, destructive=True)

    def toggle_launcher(self, e: steam.LibraryEntry, on: bool) -> None:
        try:
            if on:
                desktop.add(e)
                self.toast(f"{e.name} is in the app launcher")
            else:
                desktop.remove(e.appid)
                self.toast(f"Removed {e.name} from the app launcher")
        except OSError as err:
            self.error("Couldn't change the launcher entry", str(err))

    def toggle_steam_log(self, e: steam.LibraryEntry, on: bool) -> None:
        try:
            opts = runner.with_steam_logging(e.launch_options, e.appid, on)
        except RuntimeError as err:
            self.error("Can't log Steam launches", str(err))
            self.refresh_library()
            return
        self.run_steam_task("Changing launch options", lambda: steam.set_launch_options(self.user, e.appid, opts),
                            f"Steam launches of {e.name} are logged now. Play it, then open “Latest log”."
                            if on else f"Stopped logging {e.name}")

    def fix_game(self, e: steam.LibraryEntry) -> None:
        ws, secs = self.settings.test_workspace, self.settings.test_seconds

        def go():
            def done(rep):
                if isinstance(rep, Exception):
                    self.error("Fix it stopped", str(rep))
                    return
                self.error(f"{e.name}: fixed" if rep.fixed else f"{e.name}: no fix found", "\n".join(rep.lines()))
                self.refresh_library()
            self.run_task(f"Fixing {e.name}", lambda progress: doctor.fix_it(e, self.user, ws, secs, progress), done)
        self.confirm(f"Find a fix for {e.name}?",
                     f"Starts the game {runner.where(ws)} for {secs} seconds at a time: as it is, then with the fixes "
                     "its log points to (protontricks components, WineD3D), then other Proton versions, a fresh "
                     "Proton folder and the system's Wine. The first that works is kept. "
                     "Takes a few minutes; protontricks windows may open.", "Start", go)

    def change_runner(self, e: steam.LibraryEntry, tool: steam.Proton) -> None:
        from .apply import switch_runner
        self.confirm(f"Run {e.name} with {tool.display}?",
                     "Steam closes for a moment to save the change." + (
                         " Plain Wine uses its own Windows folder, separate from Proton's (saves there don't carry over)."
                         if tool.internal == runners.WINE else ""),
                     "Change", lambda: self.run_steam_task(f"Switching {e.name} to {tool.display}",
                                                          lambda: switch_runner(self.user, e, tool),
                                                          f"{e.name} now runs with {tool.display}"))
        self.refresh_library()  # the picker shows the saved choice until the change is done

    def uninstall_game(self, e: steam.LibraryEntry) -> None:
        """Run the game's uninstaller or move its folder to the Trash, then remove it from Steam."""
        plan = uninstall.make_plan(e, Path(self.settings.games_dir).expanduser(), steam.library(self.user))
        prefix_check = Gtk.CheckButton(label="Also move its Proton folder to the Trash (it may hold saves)",
                                       sensitive=plan.prefix is not None)
        body = Gtk.Label(label=plan.describe(False), wrap=True, xalign=0)
        prefix_check.connect("toggled", lambda c: body.set_label(plan.describe(c.get_active())))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(body)
        box.append(prefix_check)
        note = Gtk.Label(label="Files go to the Trash, so you can restore them. Steam restarts for a moment.",
                         wrap=True, xalign=0)
        note.add_css_class("dim-label")
        box.append(note)
        alert = compat.AlertDialog(heading=f"Uninstall {e.name}?", extra_child=box)
        alert.add_response("cancel", "Cancel")
        alert.add_response("go", "Uninstall")
        alert.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        tool = next((t for t in self.protons if t.internal == e.tool), None) or steam.default_proton(self.protons)

        def respond(_a, r):
            if r != "go":
                return
            remove_prefix = prefix_check.get_active()

            def done(res):
                if isinstance(res, Exception):
                    self.error(f"Couldn't uninstall {e.name}", str(res))
                    return
                self.run_steam_task(f"Removing {e.name} from Steam",
                                    lambda: steam.remove_shortcut(self.user, e.appid),
                                    f"Uninstalled {e.name}. " + " ".join(res))
            self.run_task(f"Uninstalling {e.name}",
                          lambda progress: uninstall.execute(plan, tool, remove_prefix, progress), done)
        alert.connect("response", respond)
        alert.present(self)

    def remove_game(self, e: steam.LibraryEntry) -> None:
        self.confirm(f"Remove {e.name} from Steam?",
                     "Only the Steam entry, its Proton choice and its artwork go. The game files and saves stay. "
                     "Steam restarts for a moment.", "Remove",
                     lambda: self.run_steam_task(f"Removing {e.name}", lambda: steam.remove_shortcut(self.user, e.appid),
                                                 f"Removed {e.name} from Steam"),
                     destructive=True)

    def run_steam_task(self, label: str, task, done_text: str) -> None:
        """Run something that edits Steam's files, with Steam closed and a backup first."""
        if steam.game_running():
            self.error("A game is running", "Close it first: Steam has to restart.")
            return
        self.root.set_visible_child_name("working")
        self.work_label.set_label(label)

        def work():
            was = steam.steam_running()
            error = None
            try:
                if was:
                    steam.close_steam()
                steam.backup(self.user, GLib.DateTime.new_now_local().format("%Y%m%d-%H%M%S"))
                task()
            except Exception as err:  # noqa: BLE001
                error = "".join(traceback.format_exception_only(err)).strip()
            finally:
                if was and self.settings.reopen_steam:
                    steam.open_steam()

            def ui():
                self.root.set_visible_child_name("main")
                if error:
                    self.error("That didn't work", error)
                else:
                    self.toast(done_text)
                self.refresh_library()
            GLib.idle_add(ui)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ running

    def working_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      margin_start=48, margin_end=48)
        spinner = compat.Spinner(width_request=48, height_request=48, halign=Gtk.Align.CENTER)
        self.work_label = Gtk.Label(label="Working…", wrap=True, justify=Gtk.Justification.CENTER)
        self.work_label.add_css_class("title-3")
        self.work_bar = Gtk.ProgressBar()
        note = Gtk.Label(label="Steam closes for a moment and reopens by itself.", wrap=True)
        note.add_css_class("dim-label")
        for w in (spinner, self.work_label, self.work_bar, note):
            box.append(w)
        return box

    def run_jobs(self, jobs: list[Job]) -> None:
        if steam.game_running():
            self.error("A game is running", "Close the Steam game first: Steam has to restart to pick up the change.")
            return

        def start():
            self.root.set_visible_child_name("working")
            self.work_bar.set_fraction(0)

            def progress(msg, frac):
                def ui():
                    self.work_label.set_label(msg)
                    if frac is None:
                        self.work_bar.pulse()
                    else:
                        self.work_bar.set_fraction(frac)
                GLib.idle_add(ui)

            def work():
                try:
                    results = apply_many(jobs, progress, self.settings.keep_backups, self.settings.reopen_steam)
                except Exception as error:  # noqa: BLE001
                    results = [error] * len(jobs)
                GLib.idle_add(self.finished, jobs, results)
            threading.Thread(target=work, daemon=True).start()

        if steam.steam_running():
            n = len(jobs)
            self.confirm("Steam will close for a moment",
                         f"Steam only reads its game list at start, so it closes while "
                         f"{n} game{'s are' if n > 1 else ' is'} added"
                         + (" and reopens by itself." if self.settings.reopen_steam else "."),
                         "Continue", start)
        else:
            start()

    def finished(self, jobs: list[Job], results) -> None:
        ok = [r for r in results if not isinstance(r, Exception)]
        failed = [(j, r) for j, r in zip(jobs, results) if isinstance(r, Exception)]
        lines = []
        for r in ok:
            line = f"✓ {r.name}"
            if r.moved_to:
                line += f": moved to {r.moved_to.parent}"
            if r.saves_copied:
                line += f", {len(r.saves_copied)} save folder(s) copied"
            lines.append(line)
            lines += [f"    {n}" for n in r.notes]
        for j, err in failed:
            lines.append(f"✗ {j.choices.name}: {err}")
        self.done_page.set_title(f"{len(ok)} game{'s' if len(ok) != 1 else ''} added to Steam" if ok
                                 else "Nothing was added")
        self.done_page.set_icon_name("dialog-warning-symbolic" if failed else "object-select-symbolic")
        collection = jobs[0].choices.collection.strip() if jobs else ""
        if ok and collection:
            lines.append(f"\nIn the “{collection}” collection.")
        if failed:
            lines.append("\nSteam's files were backed up first (Settings › Backups).")
        self.done_page.set_description(esc("\n".join(lines)))
        back = Gtk.Button(label="Done", halign=Gtk.Align.CENTER)
        back.add_css_class("pill")
        back.add_css_class("suggested-action")

        def go_back(*_):
            self.add_stack.set_visible_child_name("welcome")
            self.root.set_visible_child_name("main")
        back.connect("clicked", go_back)
        self.done_page.set_child(back)
        self.root.set_visible_child_name("done")

    # ----------------------------------------------------------- settings

    def on_settings(self, *_):
        SettingsDialog(self).present(self)

    def on_guide(self, *_):
        """What works, what doesn't, and what to do when a game won't start (data/guide.md), as a
        normal window: one group per section, one row per point, readable width."""
        import re
        try:
            text = resources.files("proton_porter").joinpath("data/guide.md").read_text(encoding="utf-8")
        except (OSError, ModuleNotFoundError):
            text = "# Guide missing\n- Reinstall Proton Porter."
        page = Adw.PreferencesPage()
        group = None

        def markup(t: str) -> str:
            t = esc(t)
            return re.sub(r"`(.+?)`", r"<tt>\1</tt>", re.sub(r"\*\*(.+?)\*\*", r"\1", t))
        for line in text.splitlines():
            if line.startswith("# "):
                group = Adw.PreferencesGroup(title=esc(line[2:]))
                page.add(group)
            elif line.startswith("- ") and group is not None:
                m = re.match(r"- \*\*(.+?):?\*\*:?\s*(.*)", line)
                title, body = (m.group(1).rstrip(":"), m.group(2)) if m else ("", line[2:])
                body = body[:1].upper() + body[1:]
                row = Adw.ActionRow(title=markup(title) if title else markup(body),
                                    subtitle=markup(body) if title else "", subtitle_lines=0, title_lines=0)
                group.add(row)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        win = Adw.Window(title="Compatibility guide", content=view, default_width=760, default_height=760,
                         modal=True, transient_for=self)
        win.present()
        self.guide_window = win  # debug snapshots (PROTON_PORTER_VIEW=guide) draw this one

    def on_about(self, *_):
        about = compat.AboutDialog(application_name="Proton Porter", version=__version__, application_icon=APP_ID,
                                developer_name=MAKER, developers=[f"{MAKER} {MAKER_URL}"],
                                website=REPO_URL, issue_url=f"{REPO_URL}/issues", copyright=f"© 2026 {MAKER}",
                                comments="Add Windows games to Steam with Proton: loader DLLs, saves, "
                                         "artwork and collections sorted out.",
                                license_type=Gtk.License.MIT_X11)
        about.add_link(f"{MAKER} on GitHub", MAKER_URL)
        about.present(self)


class SettingsDialog(compat.PreferencesDialog):
    def __init__(self, win: Window):
        super().__init__(title="Settings", search_enabled=True)
        self.win = win
        s = win.settings
        self.connect("closed", lambda *_: self.save())

        general = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        g = Adw.PreferencesGroup(title="New games")
        self.games_row = Adw.ActionRow(title="Games folder", subtitle=esc(s.games_dir))
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Choose folder")
        pick.add_css_class("flat")
        pick.connect("clicked", self.pick_games_dir)
        self.games_row.add_suffix(pick)
        g.add(self.games_row)
        self.move = Adw.SwitchRow(title="Move games into it", subtitle="Copies, checks, then deletes the original",
                                  active=s.move_by_default)
        g.add(self.move)
        self.proton = Adw.ComboRow(title="Runs with", subtitle="For new games; Linux programs always run natively")
        picks = [t for t in win.runners if t.internal != runners.NATIVE]
        self.internals = [""] + [t.internal for t in picks]
        self.proton.set_model(Gtk.StringList.new(["Best available (GE if installed)"] + [t.display for t in picks]))
        self.proton.set_selected(self.internals.index(s.proton) if s.proton in self.internals else 0)
        g.add(self.proton)
        self.collection = Adw.EntryRow(title="Collection", text=s.collection)
        g.add(self.collection)
        self.saves = Adw.SwitchRow(title="Import saves it finds", active=s.import_saves)
        g.add(self.saves)
        general.add(g)
        st = Adw.PreferencesGroup(title="Steam")
        self.reopen = Adw.SwitchRow(title="Reopen Steam afterwards", active=s.reopen_steam)
        st.add(self.reopen)
        self.upd = Adw.SwitchRow(title="Check for updates at start", subtitle="Looks at GitHub's main branch",
                                 active=s.check_updates)
        st.add(self.upd)
        general.add(st)
        tg = Adw.PreferencesGroup(title="Testing and Proton",
                                  description="Games are only started on this workspace when testing."
                                  if runner.workspace_support() else
                                  "Tests open games on the current screen (picking a workspace needs Hyprland "
                                  "with hypruse).")
        self.test_ws = Adw.SpinRow.new_with_range(1, 10, 1)
        self.test_ws.set_title("Test workspace")
        self.test_ws.set_value(s.test_workspace)
        tg.add(self.test_ws)
        self.test_secs = Adw.SpinRow.new_with_range(10, 300, 5)
        self.test_secs.set_title("Seconds a game must stay up")
        self.test_secs.set_value(s.test_seconds)
        tg.add(self.test_secs)
        inst = Adw.ActionRow(title="Install a custom Proton", subtitle="From a .tar.gz / .tar.xz / .tar.zst / .7z / .zip")
        ib = Gtk.Button(label="Choose archive…", valign=Gtk.Align.CENTER)
        ib.connect("clicked", self.pick_proton_archive)
        inst.add_suffix(ib)
        tg.add(inst)
        general.add(tg)
        rg2 = Adw.PreferencesGroup(title="Repacks (FitGirl, DODI)",
                                   description="Their installer runs under Proton with the install folder set to the games folder.")
        self.repack_md5 = Adw.SwitchRow(title="Check files before installing",
                                        subtitle="Compares the .bin files with the repack's MD5 list",
                                        active=s.repack_check_md5)
        rg2.add(self.repack_md5)
        self.repack_wine = Adw.SwitchRow(title="Run installers with the system's Wine",
                                         subtitle="Try this if an installer fails under Proton" if runners.wine_binary()
                                         else "Wine isn't installed", active=s.repack_use_wine,
                                         sensitive=bool(runners.wine_binary()))
        rg2.add(self.repack_wine)
        rrow = Adw.ActionRow(title="Install a repack", subtitle="Choose the folder with setup.exe")
        rbtn = Gtk.Button(label="Choose folder…", valign=Gtk.Align.CENTER)
        rbtn.connect("clicked", lambda *_: (self.close(), win.on_choose_repack()))
        rrow.add_suffix(rbtn)
        rg2.add(rrow)
        general.add(rg2)
        made = Adw.PreferencesGroup()
        made.add(maker_button())
        general.add(made)
        self.add(general)

        launch = Adw.PreferencesPage(title="Launch", icon_name="media-playback-start-symbolic")
        lg = Adw.PreferencesGroup(title="Defaults for new games", description="Each game can still change these.")
        self.overlay = Adw.SwitchRow(title="Steam overlay", active=s.overlay)
        lg.add(self.overlay)
        has_mango, has_gm = tool_installed("mangohud"), tool_installed("gamemoderun")
        self.mango = Adw.SwitchRow(title="MangoHud", subtitle="FPS overlay" if has_mango else "Not installed",
                                   active=s.mangohud, sensitive=has_mango)
        lg.add(self.mango)
        self.gamemode = Adw.SwitchRow(title="GameMode", subtitle="CPU performance mode" if has_gm else "Not installed",
                                      active=s.gamemode, sensitive=has_gm)
        lg.add(self.gamemode)
        self.log = Adw.SwitchRow(title="Proton log", subtitle=esc("~/steam-<appid>.log"), active=s.proton_log)
        lg.add(self.log)
        self.env = Adw.EntryRow(title="Extra variables for every game", text=s.extra_env)
        lg.add(self.env)
        launch.add(lg)
        self.add(launch)

        saves = Adw.PreferencesPage(title="Saves", icon_name="document-save-symbolic")
        self.search_group = Adw.PreferencesGroup(
            title="Where to look for old saves",
            description="Always searched: your home folder and ~/Old Windows. Add Windows backups or user folders "
                        "(the folder with Documents and AppData in it).")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add folder", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", self.add_search)
        self.search_group.set_header_suffix(add)
        self.search_rows: list[tuple[str, Adw.ActionRow]] = []
        for folder in s.search_folders:
            self.add_search_row(folder)
        saves.add(self.search_group)
        self.add(saves)

        art = Adw.PreferencesPage(title="Artwork", icon_name="image-x-generic-symbolic")
        ag = Adw.PreferencesGroup(title="SteamGridDB",
                                  description="Covers, banners and logos for the Steam library. Needs a free API key.")
        self.key = Adw.PasswordEntryRow(title="API key", text=s.steamgriddb_key)
        ag.add(self.key)
        get = Adw.ActionRow(title="Get a key", subtitle="steamgriddb.com › Preferences › API", activatable=True)
        get.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        get.connect("activated", lambda *_: open_uri("https://www.steamgriddb.com/profile/preferences/api"))
        ag.add(get)
        self.fetch = Adw.SwitchRow(title="Fetch artwork for new games", active=s.fetch_artwork)
        ag.add(self.fetch)
        art.add(ag)
        self.add(art)

        bk = Adw.PreferencesPage(title="Backups", icon_name="document-revert-symbolic")
        bg = Adw.PreferencesGroup(title="Steam settings backups",
                                  description="Made before every change: shortcuts, Proton choices and collections.")
        self.keep = Adw.SpinRow.new_with_range(1, 200, 1)
        self.keep.set_title("Backups to keep")
        self.keep.set_value(s.keep_backups)
        bg.add(self.keep)
        bk.add(bg)
        rg = Adw.PreferencesGroup(title="Restore")
        found = steam.backups()
        for folder in found[:15]:
            row = Adw.ActionRow(title=esc(folder.name), subtitle=esc(", ".join(p.name for p in folder.iterdir())))
            btn = Gtk.Button(label="Restore", valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, f=folder: self.restore(f))
            row.add_suffix(btn)
            rg.add(row)
        if not found:
            rg.add(Adw.ActionRow(title="No backups yet"))
        bk.add(rg)
        self.add(bk)

    def add_search_row(self, folder: str) -> None:
        row = Adw.ActionRow(title=esc(folder))
        rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
        rm.add_css_class("flat")

        def remove(*_):
            self.search_group.remove(row)
            self.search_rows.remove((folder, row))
        rm.connect("clicked", remove)
        row.add_suffix(rm)
        self.search_group.add(row)
        self.search_rows.append((folder, row))

    def add_search(self, *_):
        dialog = Gtk.FileDialog(title="Folder to search for saves")

        def done(dlg, res):
            try:
                path = dlg.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            if path not in [f for f, _ in self.search_rows]:
                self.add_search_row(path)
        dialog.select_folder(self.win, None, done)

    def pick_proton_archive(self, *_):
        dialog = Gtk.FileDialog(title="Choose a Proton archive")

        def done(dlg, res):
            try:
                path = Path(dlg.open_finish(res).get_path())
            except GLib.Error:
                return
            self.close()

            def finished(result):
                if isinstance(result, Exception):
                    self.win.error("Couldn't install Proton", str(result))
                    return
                self.win.protons = steam.protons()
                self.win.error(f"Installed {result.display}",
                               "Restart Steam so it lists the new version. Then pick it for a game in Settings or "
                               "with “Try other Protons”.")
            self.win.run_task(f"Installing {path.name}",
                              lambda progress: protontools.install_proton(path, progress), finished)
        dialog.open(self.win, None, done)

    def pick_games_dir(self, *_):
        dialog = Gtk.FileDialog(title="Games folder")
        dialog.set_initial_folder(Gio.File.new_for_path(self.win.settings.games_dir))

        def done(dlg, res):
            try:
                path = dlg.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            self.win.settings.games_dir = path
            self.games_row.set_subtitle(esc(path))
        dialog.select_folder(self.win, None, done)

    def restore(self, folder: Path) -> None:
        win = self.win
        self.close()
        win.confirm("Restore this backup?",
                    f"Steam's shortcuts, Proton choices and collections go back to how they were at {folder.name}. "
                    "Steam restarts for a moment.", "Restore",
                    lambda: win.run_steam_task("Restoring Steam's settings",
                                               lambda: steam.restore_backup(win.user, folder),
                                               f"Restored the backup from {folder.name}"))

    def save(self) -> None:
        s = self.win.settings
        s.move_by_default = self.move.get_active()
        s.proton = self.internals[self.proton.get_selected()]
        s.collection = self.collection.get_text().strip()
        s.import_saves = self.saves.get_active()
        s.reopen_steam = self.reopen.get_active()
        s.check_updates = self.upd.get_active()
        s.repack_check_md5 = self.repack_md5.get_active()
        s.repack_use_wine = self.repack_wine.get_active()
        s.overlay = self.overlay.get_active()
        s.mangohud = self.mango.get_active()
        s.gamemode = self.gamemode.get_active()
        s.proton_log = self.log.get_active()
        try:
            s.extra_env = parse_env(self.env.get_text())
        except ValueError:
            pass
        s.search_folders = [f for f, _ in self.search_rows]
        s.steamgriddb_key = self.key.get_text().strip()
        s.fetch_artwork = self.fetch.get_active()
        s.keep_backups = int(self.keep.get_value())
        s.test_workspace = int(self.test_ws.get_value())
        s.test_seconds = int(self.test_secs.get_value())
        s.save()


def guide_markup(text: str) -> str:
    """The guide's small Markdown subset (# headings, - bullets, **bold**, `code`) as Pango markup."""
    import re
    out = []
    for line in text.splitlines():
        line = esc(line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`(.+?)`", r"<tt>\1</tt>", line)
        if line.startswith("# "):
            out.append(f"\n<span size='large'><b>{line[2:]}</b></span>")
        elif line.startswith("- "):
            out.append(f"  •  {line[2:]}")
        else:
            out.append(line)
    return "\n".join(out).strip()


def render_png(widget: Gtk.Widget, path: str) -> bool:
    """Debug aid (PROTON_PORTER_SNAPSHOT=file.png): draw the window to a PNG."""
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable(widget=widget).snapshot(snap, w, h)
    node = snap.to_node()
    if node is not None:
        widget.get_native().get_renderer().render_texture(node, None).save_to_png(path)
    return False


class App(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("PROTON_PORTER_SNAPSHOT"):  # debug snapshots run beside an open window
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.connect("activate", lambda *_: self.window())
        self.connect("open", self.on_open)

    def window(self) -> Window:
        win = self.get_active_window() or Window(self)
        win.present()
        shot = os.environ.get("PROTON_PORTER_SNAPSHOT")
        if shot:  # debug: PROTON_PORTER_VIEW=scan:<folder> | library | settings
            view = os.environ.get("PROTON_PORTER_VIEW", "")
            if view.startswith("scan:"):
                win.tabs.set_visible_child_name("scan")
                win.start_scan(Path(view[5:]))
            elif view == "library":
                win.tabs.set_visible_child_name("library")
            elif view == "settings":
                win.on_settings()
            elif view == "guide":
                win.on_guide()
            GLib.timeout_add(int(os.environ.get("PROTON_PORTER_SNAPSHOT_DELAY", "3000")),
                             lambda: render_png(getattr(win, "guide_window", None) or win, shot))
        return win

    def on_open(self, _app, files, *_):
        win = self.window()
        if files:
            path = Path(files[0].get_path())
            if path.is_dir():
                win.tabs.set_visible_child_name("scan")
                win.start_scan(path)
            else:
                win.load(path)


def run(argv: list[str] | None = None) -> int:
    problem = compat.too_old()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return App().run(argv if argv is not None else sys.argv[:1])
