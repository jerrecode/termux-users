#!/usr/bin/env bash
set -euo pipefail
VERSION=0.2.0-2
PKG=termux-user-manager
PREFIX_PATH=/data/data/com.termux/files/usr
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
chmod 755 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
P="$STAGE$PREFIX_PATH"
mkdir -p "$STAGE/DEBIAN" "$P/bin" "$P/libexec/$PKG" "$P/etc/$PKG" \
         "$P/share/$PKG/shell" "$P/share/doc/$PKG" \
         "$P/share/bash-completion/completions" "$P/share/man/man1"
install -m 755 "$ROOT/src/tuser.py" "$P/libexec/$PKG/tuser.py"
install -m 644 "$ROOT/packaging/config.json" "$P/etc/$PKG/config.json"
install -m 644 "$ROOT/shell/bashrc" "$P/share/$PKG/shell/bashrc"
install -m 644 "$ROOT/shell/zshrc" "$P/share/$PKG/shell/zshrc"
install -m 644 "$ROOT/shell/fish.fish" "$P/share/$PKG/shell/fish.fish"
install -m 644 "$ROOT/README.md" "$P/share/doc/$PKG/README.md"
install -m 644 "$ROOT/docs/ARCHITECTURE.md" "$P/share/doc/$PKG/ARCHITECTURE.md"
install -m 644 "$ROOT/completion/tuser.bash" "$P/share/bash-completion/completions/tuser"
gzip -9cn "$ROOT/docs/tuser.1" > "$P/share/man/man1/tuser.1.gz"
for name in tuser tuseradd tuserdel tusermod tusers tlogin tsu twhoami tpasswd tuserenv tuserdoctor; do
    ln -s "../libexec/$PKG/tuser.py" "$P/bin/$name"
done
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: jerrecode
Homepage: https://github.com/jerrecode/termux-users
Depends: python, bash, coreutils, direnv
Recommends: proot-distro
Section: utils
Priority: optional
Description: Non-destructive virtual user/profile manager for Termux
 Provides separate HOME/XDG/runtime/language environments, optional Python
 venvs, direnv integration, virtual UID/GID metadata and proot-distro bridging.
 Virtual users share Termux's real Android/Linux UID and are not a security
 boundary. Installation does not modify the normal Termux HOME or login files.
CONTROL
cat > "$STAGE/DEBIAN/conffiles" <<CONFFILES
$PREFIX_PATH/etc/$PKG/config.json
CONFFILES
install -m 755 "$ROOT/packaging/preinst" "$STAGE/DEBIAN/preinst"
mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/${PKG}_${VERSION}_all.deb"
sha256sum "$OUT/${PKG}_${VERSION}_all.deb" > "$OUT/${PKG}_${VERSION}_all.deb.sha256"
echo "$OUT/${PKG}_${VERSION}_all.deb"
