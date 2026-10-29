#!/usr/bin/env bash
# Build dist/clip-keeper_<version>_all.deb from this checkout (no debhelper needed).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import clip_keeper; print(clip_keeper.__version__)")"
STAGE="$ROOT/build/clip-keeper_${VERSION}_all"
PKG="$ROOT/packaging"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/usr/lib/clip-keeper" "$STAGE/usr/bin" \
  "$STAGE/usr/lib/systemd/user" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/doc/clip-keeper"

sed "s/@VERSION@/$VERSION/" "$PKG/debian/control" > "$STAGE/DEBIAN/control"
install -m 755 "$PKG/debian/postinst" "$PKG/debian/prerm" "$PKG/debian/postrm" "$STAGE/DEBIAN/"

( cd "$ROOT" && find clip_keeper -type f \( -name '*.py' -o -name '*.svg' \) -print0 ) |
  while IFS= read -r -d '' f; do
    mode=644; [[ "$f" == *.sh ]] && mode=755
    install -D -m "$mode" "$ROOT/$f" "$STAGE/usr/lib/clip-keeper/$f"
  done

install -m 755 "$PKG/bin/clip-keeper" "$PKG/bin/clip-keeper-tray" "$PKG/bin/clip-keeper-popup" "$STAGE/usr/bin/"
install -m 644 "$PKG/clip-keeper.service" "$STAGE/usr/lib/systemd/user/"
install -m 644 "$PKG/clip-keeper.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/usr/share/doc/clip-keeper/"
install -D -m 644 "$PKG/udev/70-clip-keeper-uinput.rules" "$STAGE/usr/lib/udev/rules.d/70-clip-keeper-uinput.rules"
install -D -m 644 /dev/stdin "$STAGE/usr/lib/modules-load.d/clip-keeper.conf" <<<"uinput"

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/clip-keeper_${VERSION}_all.deb"
