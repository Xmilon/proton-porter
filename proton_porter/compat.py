"""Stand-ins for newer libadwaita widgets, so the window runs on libadwaita 1.4 and up.

1.4 (Ubuntu 24.04's 1.5, Fedora 39+, Debian 13, Arch) has everything the
window needs except: Spinner (1.6), AlertDialog, PreferencesDialog and
AboutDialog (1.5). Those fall back to Gtk.Spinner and the older *Window /
MessageDialog classes, with the same calls the UI makes.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

MIN_ADW = (1, 4)


def adw_version() -> tuple[int, int]:
    return Adw.get_major_version(), Adw.get_minor_version()


def too_old() -> str | None:
    """Why this libadwaita can't run the window, or None."""
    if adw_version() < MIN_ADW:
        have = ".".join(map(str, adw_version()))
        return (f"Proton Porter's window needs libadwaita 1.4 or newer; this system has {have}. "
                "The command line works: run `proton-porter --help`.")
    return None


def Spinner(**kw) -> Gtk.Widget:  # noqa: N802 - mirrors the widget name
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(**kw)
    return Gtk.Spinner(spinning=True, **kw)


if hasattr(Adw, "AlertDialog"):
    AlertDialog = Adw.AlertDialog
else:
    class AlertDialog(Adw.MessageDialog):  # type: ignore[no-redef]
        """Adw.MessageDialog with AlertDialog's present(parent)."""

        def __init__(self, extra_child=None, **kw):
            super().__init__(modal=True, **kw)
            if extra_child is not None:
                self.set_extra_child(extra_child)

        def present(self, parent=None):  # noqa: D401
            if parent is not None:
                self.set_transient_for(parent.get_root() if hasattr(parent, "get_root") else parent)
            super().present()


if hasattr(Adw, "PreferencesDialog"):
    PreferencesDialog = Adw.PreferencesDialog
else:
    class PreferencesDialog(Adw.PreferencesWindow):  # type: ignore[no-redef]
        """Adw.PreferencesWindow that emits "closed" like PreferencesDialog does."""

        def __init__(self, **kw):
            super().__init__(modal=True, **kw)
            self._closed_cbs = []
            super().connect("close-request", self._on_close)

        def _on_close(self, *_):
            for cb in self._closed_cbs:
                cb(self)
            return False

        def connect(self, signal, cb, *args):
            if signal == "closed":
                self._closed_cbs.append(lambda w: cb(w, *args))
                return 0
            return super().connect(signal, cb, *args)

        def present(self, parent=None):
            if parent is not None:
                self.set_transient_for(parent)
            super().present()


def AboutDialog(**kw):  # noqa: N802
    if hasattr(Adw, "AboutDialog"):
        return Adw.AboutDialog(**kw)

    class _About(Adw.AboutWindow):
        def present(self, parent=None):
            if parent is not None:
                self.set_transient_for(parent)
            super().present()
    return _About(modal=True, **kw)
