"""GTK 4 / libadwaita window: pick a game's .exe, review, add to Steam."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, steam  # noqa: E402
from .analyze import Analysis, DllChoice, SaveCandidate, analyze, default_search_roots, overrides_string  # noqa: E402
from .apply import Choices, apply, describe, parse_env  # noqa: E402

APP_ID = "io.github.xmilon.ProtonPorter"
SAVE_PLACES = ["Documents/My Games", "Documents", "Saved Games", "AppData/Local", "AppData/LocalLow", "AppData/Roaming"]


def default_games_dir() -> Path:
    for p in ("~/Storage/Games", "~/Games", "~/games"):
        q = Path(p).expanduser()
        if q.is_dir():
            return q
    return Path("~/Games").expanduser()


def row_text(row: Adw.PreferencesRow, title: str, subtitle: str = "") -> None:
    row.set_title(GLib.markup_escape_text(title))
    if isinstance(row, Adw.ActionRow):
        row.set_subtitle(GLib.markup_escape_text(subtitle))


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Proton Porter", default_width=640, default_height=820)
        self.analysis: Analysis | None = None
        self.search_roots: list[Path] = default_search_roots()
        self.games_dir = default_games_dir()
        self.protons = steam.protons()
        self.users = steam.users()

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack()
        self.toasts.set_child(self.stack)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About")
        about.connect("clicked", self.on_about)
        header.pack_end(about)
        view.add_top_bar(header)
        view.set_content(self.toasts)

        self.bottom = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        self.preview_btn = Gtk.Button(label="Preview")
        self.preview_btn.connect("clicked", self.on_preview)
        self.add_btn = Gtk.Button(label="Add to Steam", hexpand=True, halign=Gtk.Align.END)
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.add_css_class("pill")
        self.add_btn.connect("clicked", self.on_add)
        self.bottom.append(self.preview_btn)
        self.bottom.append(self.add_btn)
        view.add_bottom_bar(self.bottom)
        self.set_content(view)
        self.view = view

        # Some icon themes lack the symbolic icons GTK apps use; fall back to Adwaita's.
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if not theme.has_icon("applications-games-symbolic"):
            Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")

        self.stack.add_named(self.welcome_page(), "welcome")
        self.stack.add_named(self.working_page(), "working")
        self.done_page = Adw.StatusPage(icon_name="object-select-symbolic", title="Added to Steam")
        self.stack.add_named(self.done_page, "done")
        self.show_page("welcome")

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)

        if not self.protons:
            self.error("No Proton found", "Install Proton from Steam first: Library > search “Proton” > Install.")
        if not self.users:
            self.error("No Steam account found", "Open Steam and log in once, then start Proton Porter again.")

    # ----------------------------------------------------------------- pages

    def show_page(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        self.view.set_reveal_bottom_bars(name == "form")

    def welcome_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name="applications-games-symbolic", title="Port a Windows game",
                              description="Choose the game's .exe (or the .bat it starts from), or drop it here. "
                                          "Proton Porter works out the fixes and adds it to Steam.")
        btn = Gtk.Button(label="Choose .exe…", halign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", self.on_choose)
        page.set_child(btn)
        return page

    def working_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      margin_start=48, margin_end=48)
        spinner = Adw.Spinner(width_request=48, height_request=48, halign=Gtk.Align.CENTER)
        self.work_label = Gtk.Label(label="Working…", wrap=True, justify=Gtk.Justification.CENTER)
        self.work_label.add_css_class("title-3")
        self.work_bar = Gtk.ProgressBar()
        self.work_note = Gtk.Label(label="Steam closes for a moment and reopens by itself.", wrap=True)
        self.work_note.add_css_class("dim-label")
        for w in (spinner, self.work_label, self.work_bar, self.work_note):
            box.append(w)
        return box

    # --------------------------------------------------------------- choose

    def on_choose(self, *_):
        dialog = Gtk.FileDialog(title="Choose the game's .exe")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Windows programs (.exe, .bat)")
        for pattern in ("*.exe", "*.EXE", "*.bat", "*.BAT", "*.cmd"):
            f.add_pattern(pattern)
        filters.append(f)
        dialog.set_filters(filters)
        start = next((p for p in (Path("~/Old Windows").expanduser(), self.games_dir, Path.home()) if p.is_dir()), Path.home())
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
        if files:
            self.load(Path(files[0].get_path()))
            return True
        return False

    def load(self, exe: Path) -> None:
        try:
            self.analysis = analyze(exe, self.search_roots)
        except Exception as error:  # noqa: BLE001
            self.error("Couldn't read that file", str(error))
            return
        old = self.stack.get_child_by_name("form")
        if old:
            self.stack.remove(old)
        self.stack.add_named(self.form_page(self.analysis), "form")
        self.show_page("form")

    # ----------------------------------------------------------------- form

    def form_page(self, a: Analysis) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # Game
        g = Adw.PreferencesGroup(title="Game", description=GLib.markup_escape_text(f"Engine: {a.engine}"))
        exe_row = Adw.ActionRow(subtitle_selectable=True)
        row_text(exe_row, "Program", str(a.exe))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self.on_choose)
        exe_row.add_suffix(change)
        g.add(exe_row)
        if a.other_exes:
            alt = Adw.ComboRow(title="Use another .exe", subtitle="Other programs in the folder")
            names = ["(keep this one)"] + [str(p.relative_to(a.root)) for p in a.other_exes]
            alt.set_model(Gtk.StringList.new(names))
            alt.connect("notify::selected", lambda r, _p: r.get_selected() and self.load(a.other_exes[r.get_selected() - 1]))
            g.add(alt)
        self.name_row = Adw.EntryRow(title="Name in Steam", text=a.name)
        g.add(self.name_row)
        page.add(g)

        # Notes / warnings first: they matter most.
        if a.warnings or a.notes:
            n = Adw.PreferencesGroup(title="What it found")
            for text, icon in [(w, "dialog-warning-symbolic") for w in a.warnings] + [(t, "dialog-information-symbolic") for t in a.notes]:
                r = Adw.ActionRow(subtitle_lines=0)
                r.set_subtitle(GLib.markup_escape_text(text))
                r.add_prefix(Gtk.Image(icon_name=icon))
                n.add(r)
            page.add(n)

        # Location
        loc = Adw.PreferencesGroup(title="Folder", description=GLib.markup_escape_text(f"Now in {a.root.parent}"))
        self.move_row = Adw.SwitchRow(title="Move to my games folder")
        inside = a.root.parent.resolve() == self.games_dir.resolve()
        self.move_row.set_active(not inside and not str(a.root).startswith(str(self.games_dir)))
        loc.add(self.move_row)
        self.dir_row = Adw.ActionRow(title="Games folder")
        self.dir_row.set_subtitle(GLib.markup_escape_text(str(self.games_dir)))
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Choose folder")
        pick.connect("clicked", self.on_pick_games_dir)
        self.dir_row.add_suffix(pick)
        loc.add(self.dir_row)
        page.add(loc)

        # Proton
        p = Adw.PreferencesGroup(title="Proton")
        self.proton_row = Adw.ComboRow(title="Proton version")
        self.proton_row.set_model(Gtk.StringList.new([t.display for t in self.protons] or ["(none installed)"]))
        best = steam.default_proton(self.protons)
        if best:
            self.proton_row.set_selected(self.protons.index(best))
        p.add(self.proton_row)
        self.args_row = Adw.EntryRow(title="Game arguments (optional)")
        p.add(self.args_row)
        self.env_row = Adw.EntryRow(title="Extra variables, e.g. DXVK_HUD=fps (optional)")
        p.add(self.env_row)
        self.overlay_row = Adw.SwitchRow(title="Steam overlay", active=True,
                                         subtitle="Turn off if the game crashes with it")
        p.add(self.overlay_row)
        page.add(p)

        # DLLs
        d = Adw.PreferencesGroup(title="Loader DLLs",
                                 description="DLLs next to the game that replace Windows ones (mod loaders, "
                                             "ReShade, fixes). Turned on means the game's copy is used instead of Wine's.")
        self.dll_box = d
        self.dll_rows: list[tuple[DllChoice, Adw.SwitchRow]] = []
        if not a.dlls:
            empty = Adw.ActionRow(title="None found", subtitle="Nothing to override")
            d.add(empty)
        for dll in a.dlls:
            self.add_dll_row(dll)
        extra = Adw.EntryRow(title="Add another DLL by name", show_apply_button=True)
        extra.connect("apply", self.on_add_dll)
        d.add(extra)
        page.add(d)

        # Saves
        s = Adw.PreferencesGroup(title="Saves",
                                 description="Copied into the game's own Proton folder. The originals stay where they are.")
        self.saves_group = s
        self.save_rows: list[tuple[SaveCandidate, Adw.SwitchRow]] = []
        for cand in a.saves:
            self.add_save_row(cand)
        if not a.saves:
            s.add(Adw.ActionRow(title="No old saves found", subtitle="Add a folder below, or search a Windows backup"))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START, margin_top=6)
        add_save = Gtk.Button(label="Add save folder…")
        add_save.connect("clicked", self.on_add_save)
        search = Gtk.Button(label="Search a Windows backup…")
        search.connect("clicked", self.on_search_root)
        buttons.append(add_save)
        buttons.append(search)
        s.add(buttons)
        page.add(s)

        # Steam
        st = Adw.PreferencesGroup(title="Steam")
        self.user_row = Adw.ComboRow(title="Steam account")
        self.user_row.set_model(Gtk.StringList.new([u.name for u in self.users] or ["(no account)"]))
        st.add(self.user_row)
        self.collection_row = Adw.EntryRow(title="Collection", text="Linux")
        st.add(self.collection_row)
        self.restart_row = Adw.SwitchRow(title="Reopen Steam afterwards", active=True)
        st.add(self.restart_row)
        page.add(st)
        return page

    def add_dll_row(self, dll: DllChoice) -> None:
        r = Adw.SwitchRow(active=dll.enabled)
        row_text(r, f"{dll.name}.dll", dll.reason)
        self.dll_box.add(r)
        self.dll_rows.append((dll, r))

    def on_add_dll(self, row: Adw.EntryRow):
        name = row.get_text().strip().lower().removesuffix(".dll")
        if name and not any(d.name == name for d, _ in self.dll_rows):
            dll = DllChoice(name, "Added by you", True)
            self.analysis.dlls.append(dll)
            self.add_dll_row(dll)
        row.set_text("")

    def add_save_row(self, cand: SaveCandidate) -> None:
        r = Adw.SwitchRow(active=True, subtitle_lines=0)
        row_text(r, str(cand.source), "→ C:\\users\\steamuser\\" + cand.dest.replace("/", "\\"))
        self.saves_group.add(r)
        self.save_rows.append((cand, r))

    def on_pick_games_dir(self, *_):
        dialog = Gtk.FileDialog(title="Games folder")
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.games_dir)))

        def done(dlg, res):
            try:
                self.games_dir = Path(dlg.select_folder_finish(res).get_path())
            except GLib.Error:
                return
            self.dir_row.set_subtitle(GLib.markup_escape_text(str(self.games_dir)))
            self.move_row.set_active(True)
        dialog.select_folder(self, None, done)

    def on_search_root(self, *_):
        dialog = Gtk.FileDialog(title="Old Windows drive or user folder")

        def done(dlg, res):
            try:
                root = Path(dlg.select_folder_finish(res).get_path())
            except GLib.Error:
                return
            self.search_roots.append(root)
            users = root / "Users"
            if users.is_dir():
                self.search_roots += [u for u in users.iterdir() if u.is_dir()]
            self.load(self.analysis.exe)
            self.toasts.add_toast(Adw.Toast(title=f"Found {len(self.analysis.saves)} save folder(s)"))
        dialog.select_folder(self, None, done)

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
        alert = Adw.AlertDialog(heading="Where did the game keep these?",
                                body="On Windows, under C:\\Users\\<you>\\. Check the game's PCGamingWiki page if unsure.")
        alert.set_extra_child(box)
        alert.add_response("cancel", "Cancel")
        alert.add_response("ok", "Add")
        alert.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def respond(_a, resp):
            if resp == "ok" and sub.get_text().strip():
                dest = f"{SAVE_PLACES[place.get_selected()]}/{sub.get_text().strip()}"
                cand = SaveCandidate(src, dest, "Added by you")
                self.analysis.saves.append(cand)
                self.add_save_row(cand)
        alert.connect("response", respond)
        alert.present(self)

    # ---------------------------------------------------------------- apply

    def choices(self) -> tuple[str, Choices]:
        a = self.analysis
        for dll, row in self.dll_rows:
            dll.enabled = row.get_active()
        env = parse_env(self.env_row.get_text())
        name = self.name_row.get_text().strip()
        if not name:
            raise ValueError("The game needs a name.")
        if not self.protons or not self.users:
            raise ValueError("Steam or Proton isn't set up.")
        c = Choices(
            name=name,
            proton=self.protons[self.proton_row.get_selected()],
            user=self.users[self.user_row.get_selected()],
            move_to=self.games_dir if self.move_row.get_active() else None,
            args=self.args_row.get_text(),
            extra_env=env,
            collection=self.collection_row.get_text(),
            saves=[cand for cand, row in self.save_rows if row.get_active()],
            overlay=self.overlay_row.get_active(),
            restart_steam=self.restart_row.get_active(),
        )
        return overrides_string(a.dlls), c

    def on_preview(self, *_):
        try:
            dlls, c = self.choices()
        except ValueError as error:
            self.error("Check the form", str(error))
            return
        label = Gtk.Label(label=describe(self.analysis, dlls, c), selectable=True, xalign=0, wrap=True)
        label.add_css_class("monospace")
        alert = Adw.AlertDialog(heading="This is what will happen")
        alert.set_extra_child(label)
        alert.add_response("close", "Close")
        alert.present(self)

    def on_add(self, *_):
        try:
            dlls, c = self.choices()
        except ValueError as error:
            self.error("Check the form", str(error))
            return
        if steam.game_running():
            self.error("A game is running", "Close the Steam game first: Steam has to restart to pick up the change.")
            return
        if steam.steam_running():
            alert = Adw.AlertDialog(heading="Steam will close for a moment",
                                    body="Steam only reads its game list at start, so it closes now "
                                         + ("and reopens by itself." if c.restart_steam else "and stays closed."))
            alert.add_response("cancel", "Cancel")
            alert.add_response("go", "Continue")
            alert.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            alert.connect("response", lambda _a, r: r == "go" and self.run_apply(dlls, c))
            alert.present(self)
        else:
            self.run_apply(dlls, c)

    def run_apply(self, dlls: str, c: Choices) -> None:
        self.show_page("working")
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
                result = apply(self.analysis, dlls, c, progress)
            except Exception as error:  # noqa: BLE001
                detail = "".join(traceback.format_exception_only(error)).strip()
                GLib.idle_add(self.failed, detail)
                return
            GLib.idle_add(self.finished, result)

        threading.Thread(target=work, daemon=True).start()

    def failed(self, detail: str) -> None:
        self.show_page("form")
        self.error("That didn't work", detail + "\n\nSteam's files were backed up first, in ~/.local/state/proton-porter/backups.")

    def finished(self, r) -> None:
        lines = [f"“{r.name}” is in your Steam library" + (f" and the “{self.collection_row.get_text()}” collection." if r.collection_ok and self.collection_row.get_text().strip() else ".")]
        if r.moved_to:
            lines.append(f"Moved to {r.moved_to}.")
        if r.saves_copied:
            lines.append("Saves copied: " + ", ".join(r.saves_copied) + ".")
        lines += r.notes
        if not r.collection_ok:
            lines.append("Couldn't add it to a collection: open Steam once and try again.")
        if r.launch_options:
            lines.append(f"Launch options: {r.launch_options}")
        self.done_page.set_description(GLib.markup_escape_text("\n".join(lines)))
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        again = Gtk.Button(label="Port another game")
        again.add_css_class("pill")
        again.add_css_class("suggested-action")
        again.connect("clicked", lambda *_: self.show_page("welcome"))
        box.append(again)
        self.done_page.set_child(box)
        self.show_page("done")

    # ---------------------------------------------------------------- misc

    def error(self, heading: str, body: str) -> None:
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", "OK")
        alert.present(self)

    def on_about(self, *_):
        about = Adw.AboutDialog(application_name="Proton Porter", version=__version__,
                                application_icon="io.github.xmilon.ProtonPorter",
                                comments="Add Windows games to Steam with Proton, with loader DLLs, saves and collections sorted out.",
                                license_type=Gtk.License.MIT_X11)
        about.present(self)


def render_png(widget: Gtk.Widget, path: str) -> bool:
    """Debug aid (PROTON_PORTER_SNAPSHOT=file.png): draw the window to a PNG."""
    w, h = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable(widget=widget)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is not None:
        widget.get_native().get_renderer().render_texture(node, None).save_to_png(path)
    return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.connect("activate", self.on_activate)
        self.connect("open", self.on_open)

    def window(self) -> Window:
        win = self.get_active_window()
        if win is None:
            win = Window(self)
        win.present()
        return win

    def on_activate(self, *_):
        self.window()

    def on_open(self, _app, files, *_):
        win = self.window()
        if files:
            win.load(Path(files[0].get_path()))
        shot = os.environ.get("PROTON_PORTER_SNAPSHOT")
        if shot:
            GLib.timeout_add(1500, render_png, win.get_content(), shot)


def run(argv: list[str] | None = None) -> int:
    return App().run(argv if argv is not None else sys.argv[:1])
