#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:-$(awk -F'"' '/^version = / { print $2; exit }' pyproject.toml)}"
[[ -n "$VERSION" ]] || { echo "Could not determine package version" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python -m pip install -e '.[build]'
pyinstaller --clean --noconfirm packaging/visualsyslog.spec
PKG="$ROOT/dist/visualsyslog-deb"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/applications"
cp "$ROOT/dist/VisualSyslog" "$PKG/usr/bin/visualsyslog"
chmod 755 "$PKG/usr/bin/visualsyslog"
cat > "$PKG/DEBIAN/control" <<EOF
Package: visualsyslog
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Maintainer: VisualSyslog contributors
Depends: libc6
Description: Cross-platform visual Syslog collector
 Python Syslog receiver with a local web dashboard and SQLite retention.
EOF
cat > "$PKG/usr/share/applications/visualsyslog.desktop" <<'EOF'
[Desktop Entry]
Name=VisualSyslog
Comment=Syslog server and dashboard
Exec=visualsyslog
Terminal=true
Type=Application
Categories=Network;Monitor;
EOF
dpkg-deb --root-owner-group --build "$PKG" "$ROOT/dist/visualsyslog_${VERSION}_${ARCH}.deb"
