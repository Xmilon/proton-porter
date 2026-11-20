#!/usr/bin/env bash
# Install Proton Porter for the current user.
#   ./install.sh            install or update (asks before installing system packages)
#   ./install.sh --yes      same, without asking
#   ./install.sh --remove   uninstall
# Works on Arch (pacman), Debian/Ubuntu (apt), Fedora (dnf) and openSUSE (zypper).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
share=${XDG_DATA_HOME:-$HOME/.local/share}
app=$share/proton-porter/app
bin=$HOME/.local/bin/proton-porter
yes=0
[ "${1:-}" = --yes ] && yes=1

if [ "${1:-}" = --remove ]; then
  rm -rf "$app" "$bin" "$share/applications/io.github.xmilon.ProtonPorter.desktop" \
    "$share/icons/hicolor/scalable/apps/io.github.xmilon.ProtonPorter.svg"
  echo "Removed. Backups stay in ~/.local/state/proton-porter."
  exit 0
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "Proton Porter needs Python 3.10 or newer (found: $(python3 --version 2>&1))." >&2
  exit 1
fi

sudo=sudo
[ "$(id -u)" = 0 ] && sudo=""  # already root (containers, minimal installs)

# Package names per package manager: what each needed piece is called there.
if command -v pacman >/dev/null; then
  pm=pacman; install=($sudo pacman -S --needed)
  pkg_vdf=python-vdf; pkg_gtk=(python-gobject gtk4 libadwaita); pkg_7z=7zip; pkg_zstd=zstd
elif command -v apt-get >/dev/null; then
  pm=apt; install=($sudo apt-get install)
  pkg_vdf=python3-vdf; pkg_gtk=(python3-gi gir1.2-gtk-4.0 gir1.2-adw-1); pkg_7z=7zip; pkg_zstd=zstd
elif command -v dnf >/dev/null; then
  pm=dnf; install=($sudo dnf install)
  pkg_vdf=python3-vdf; pkg_gtk=(python3-gobject gtk4 libadwaita); pkg_7z=7zip; pkg_zstd=zstd
elif command -v zypper >/dev/null; then
  pm=zypper; install=($sudo zypper install)
  pkg_vdf=python3-vdf; pkg_gtk=(python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1)
  pkg_7z=7zip; pkg_zstd=zstd
else
  pm=""
fi

missing=()
python3 -c 'import vdf' 2>/dev/null || missing+=("$pkg_vdf")
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
  || missing+=("${pkg_gtk[@]}")
command -v 7z >/dev/null || command -v 7zz >/dev/null || missing+=("$pkg_7z")
command -v zstd >/dev/null || missing+=("$pkg_zstd")

if [ ${#missing[@]} -gt 0 ]; then
  if [ -z "$pm" ]; then
    echo "Couldn't tell your package manager. Install these yourself: Python GObject (PyGObject)," \
         "GTK 4, libadwaita 1.x, python vdf, 7-Zip, zstd. Then run this again." >&2
    exit 1
  fi
  echo "Proton Porter needs these $pm packages: ${missing[*]}"
  if [ $yes = 0 ]; then
    read -r -p "Install them now${sudo:+ with sudo}? [Y/n] " answer
    case "$answer" in [nN]*) echo "Not installing. Run this again when they're installed."; exit 1 ;; esac
  fi
  # A package that doesn't exist on this release fails the install; the checks below catch what's missing.
  "${install[@]}" "${missing[@]}" || echo "Some packages couldn't be installed; checking what's still missing."
fi

rm -rf "$app"
mkdir -p "$app" "$(dirname "$bin")" "$share/applications" "$share/icons/hicolor/scalable/apps"
cp -r "$here/proton_porter" "$app/"

# vdf isn't packaged everywhere: fall back to a private copy from PyPI, next to the app.
if ! python3 -c 'import vdf' 2>/dev/null; then
  echo "python vdf isn't installed system-wide; putting a private copy next to the app."
  python3 -m pip install --quiet --target "$app" vdf \
    || { echo "Couldn't install vdf (install pip, or your distro's python3-vdf package)." >&2; exit 1; }
fi
if ! python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null; then
  echo "GTK 4 / libadwaita for Python are still missing. The command line works; the window won't." >&2
fi
command -v 7z >/dev/null || command -v 7zz >/dev/null \
  || echo "Note: no 7-Zip, so .rar/.7z/.zip archives can't be installed."

cat > "$bin" <<SH
#!/bin/sh
PYTHONPATH="$app\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m proton_porter "\$@"
SH
chmod +x "$bin"
# Full path in Exec=, so the launcher works even when ~/.local/bin isn't on PATH.
sed "s|^Exec=proton-porter|Exec=$bin|" "$here/data/io.github.xmilon.ProtonPorter.desktop" \
  > "$share/applications/io.github.xmilon.ProtonPorter.desktop"
cp "$here/data/io.github.xmilon.ProtonPorter.svg" "$share/icons/hicolor/scalable/apps/"
update-desktop-database -q "$share/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$share/icons/hicolor" 2>/dev/null || true
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: ~/.local/bin isn't on your PATH; the app launcher entry still works." ;; esac
echo "Installed. Open “Proton Porter” from the app launcher, or run: proton-porter"
