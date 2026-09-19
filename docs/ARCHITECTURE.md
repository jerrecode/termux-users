# Architecture

The system uses three layers:

1. **Shared Termux system** — `$PREFIX` remains the one package-managed system.
2. **Virtual account** — a separate HOME, XDG hierarchy and language/runtime
   roots are constructed by `tlogin`.
3. **Project** — `direnv` optionally applies project-local environment changes.

The mutable account database is stored at
`$PREFIX/var/lib/termux-user-manager/users.json`. Passwd/group-shaped compatibility metadata is regenerated beside it as `passwd` and `group`; these files are informational and are not consulted by the kernel. Virtual homes default to `$TERMUX__ROOTFS_DIR/users/<name>` (normally `/data/data/com.termux/files/users/<name>`), deliberately outside the package-managed `$PREFIX`. The existing native Termux `$HOME` at `$TERMUX__ROOTFS_DIR/home` remains untouched. Existing account records retain their stored home paths across upgrades. The package configuration is stored at
`$PREFIX/etc/termux-user-manager/config.json`.

`tlogin` does not mutate the parent shell. It builds a new environment and
`execve()`s a child login shell (or explicit command). This means exiting the
virtual shell naturally returns to the ordinary Termux environment.

Virtual UID/GID values are metadata only. `id -u` still returns Termux's real
Android application UID.
