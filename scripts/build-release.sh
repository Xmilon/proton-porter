#!/usr/bin/env bash
# Build dist/proton-porter-<version>.tar.gz and dist/proton-porter.pyz
set -euo pipefail
cd "$(dirname "$0")/.."
ver=$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+)\"", open("proton_porter/__init__.py").read())[1])')
rm -rf dist && mkdir -p dist/stage/proton-porter-$ver
cp -r proton_porter data install.sh LICENSE README.md PKGBUILD dist/stage/proton-porter-$ver/
find dist/stage -name __pycache__ -prune -exec rm -rf {} +
tar -C dist/stage -czf dist/proton-porter-$ver.tar.gz proton-porter-$ver
mkdir dist/pyz && cp -r proton_porter dist/pyz/ && find dist/pyz -name __pycache__ -prune -exec rm -rf {} +
python3 -m zipapp dist/pyz -m "proton_porter.cli:_entry" -p "/usr/bin/env python3" -o dist/proton-porter.pyz
rm -rf dist/stage dist/pyz
ls -la dist
