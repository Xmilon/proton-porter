# Maintainer: Xmilon
pkgname=proton-porter
pkgver=1.0.0
pkgrel=1
pkgdesc="Add Windows games to Steam with Proton: loader DLLs, saves, collections sorted out"
arch=(any)
url="https://github.com/Xmilon/proton-porter"
license=(MIT)
depends=(python python-vdf python-gobject gtk4 libadwaita)
optdepends=('steam: the whole point' 'protontricks: extra Windows components')
source=("$pkgname-$pkgver.tar.gz::$url/releases/download/v$pkgver/$pkgname-$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
  cd "$pkgname-$pkgver"
  local site
  site=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
  install -d "$pkgdir$site"
  cp -r proton_porter "$pkgdir$site/"
  python -m compileall -q "$pkgdir$site/proton_porter"
  install -Dm755 /dev/stdin "$pkgdir/usr/bin/proton-porter" <<'SH'
#!/bin/sh
exec python3 -m proton_porter "$@"
SH
  install -Dm644 data/io.github.xmilon.ProtonPorter.desktop -t "$pkgdir/usr/share/applications"
  install -Dm644 data/io.github.xmilon.ProtonPorter.svg -t "$pkgdir/usr/share/icons/hicolor/scalable/apps"
  install -Dm644 LICENSE -t "$pkgdir/usr/share/licenses/$pkgname"
}
