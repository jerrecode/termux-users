TERMUX_PKG_HOMEPAGE=https://github.com/jerrecode/termux-users
TERMUX_PKG_DESCRIPTION="Non-destructive virtual user/profile manager for Termux"
TERMUX_PKG_LICENSE="MIT"
TERMUX_PKG_MAINTAINER="jerrecode"
TERMUX_PKG_VERSION=0.2.0
TERMUX_PKG_REVISION=2
TERMUX_PKG_DEPENDS="python, bash, coreutils, direnv"
TERMUX_PKG_RECOMMENDS="proot-distro"
TERMUX_PKG_PLATFORM_INDEPENDENT=true
TERMUX_PKG_CONFFILES="etc/termux-user-manager/config.json"
TERMUX_PKG_SKIP_SRC_EXTRACT=true

termux_step_make_install() {
    install -Dm755 "$TERMUX_PKG_BUILDER_DIR/src/tuser.py" "$TERMUX_PREFIX/libexec/termux-user-manager/tuser.py"
    install -Dm600 "$TERMUX_PKG_BUILDER_DIR/packaging/config.json" "$TERMUX_PREFIX/etc/termux-user-manager/config.json"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/shell/bashrc" "$TERMUX_PREFIX/share/termux-user-manager/shell/bashrc"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/shell/zshrc" "$TERMUX_PREFIX/share/termux-user-manager/shell/zshrc"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/shell/fish.fish" "$TERMUX_PREFIX/share/termux-user-manager/shell/fish.fish"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/README.md" "$TERMUX_PREFIX/share/doc/termux-user-manager/README.md"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/docs/ARCHITECTURE.md" "$TERMUX_PREFIX/share/doc/termux-user-manager/ARCHITECTURE.md"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/completion/tuser.bash" "$TERMUX_PREFIX/share/bash-completion/completions/tuser"
    install -Dm644 "$TERMUX_PKG_BUILDER_DIR/docs/tuser.1" "$TERMUX_PREFIX/share/man/man1/tuser.1"
    local n
    for n in tuser tuseradd tuserdel tusermod tusers tlogin tsu twhoami tpasswd tuserenv tuserdoctor; do
        ln -s "../libexec/termux-user-manager/tuser.py" "$TERMUX_PREFIX/bin/$n"
    done
}
