# Termux Users

`termux-user-manager` adds an opt-in, Unix-like **virtual user/profile system** to Termux without replacing Termux's normal login environment, modifying the existing Termux `$HOME`, or requiring root.

Each virtual user gets a separate home directory, XDG directories, shell state, SSH/GnuPG state, Git configuration, language/runtime state and optional Python virtual environment. The shared Termux installation under `$PREFIX` remains common to all profiles.

> **Important:** these are virtual users, not kernel-enforced Linux users. All profiles still run under the Android/Linux UID of the Termux app. The package is intended for configuration, workflow and development-environment separation, not hostile-user security isolation.

## Highlights

- Separate virtual `HOME` directories under `$TERMUX__ROOTFS_DIR/users/<name>`.
- Leaves the normal Termux home at `$TERMUX__ROOTFS_DIR/home` untouched.
- Separate XDG config, data, state, cache and runtime directories.
- Separate shell dotfiles and history, `~/.ssh`, `~/.gnupg`, `~/.gitconfig`, npm, Cargo/Rustup, Gradle, Go, IPython and Python user state.
- Optional per-user default Python `venv` at `$HOME/.venvs/default`.
- `direnv` integration for project-specific environments below the virtual-user layer.
- Clean child login environments built from a controlled allowlist instead of mutating the parent Termux shell.
- Virtual UID/GID/group metadata and generated passwd/group-shaped compatibility files.
- Optional profile passwords using PBKDF2-HMAC-SHA256. These are convenience locks, not security boundaries.
- Optional `proot-distro` bridge for conventional guest-side `/etc/passwd` and `/home/<user>` semantics.
- Non-destructive account deletion by default: deleting an account preserves its home unless explicit destructive flags are supplied.
- Diagnostic and repair command: `tuser doctor` / `tuser doctor --repair`.
- Unix-like convenience commands: `tuseradd`, `tuserdel`, `tusermod`, `tusers`, `tlogin`, `tsu`, `twhoami`, `tpasswd`, `tuserenv`, and `tuserdoctor`.

## Filesystem model

A standard Termux installation normally resembles:

```text
/data/data/com.termux/files/
├── home/                  # existing native Termux HOME — untouched
├── users/                 # virtual-user homes managed by this project
│   ├── dev/
│   ├── work/
│   └── ...
└── usr/                   # Termux $PREFIX — shared package-managed system
    ├── bin/
    ├── etc/
    ├── lib/
    └── var/lib/termux-user-manager/
        ├── users.json
        ├── passwd
        └── group
```

The implementation uses `$TERMUX__ROOTFS_DIR` when available instead of assuming that exact path. Existing account records retain their stored home paths across upgrades; upgrading does not automatically move or delete user homes.

## Requirements

A normal Termux installation with `apt` is expected. The Debian package depends on:

```text
python
bash
coreutils
direnv
```

`proot-distro` is recommended but optional. It is only needed for the PRoot integration described below.

## Install from GitHub

The repository includes the current installable `.deb` under `dist/`.

### Option A: `curl`

Run in Termux:

```sh
cd "$HOME"
curl -fL -o termux-user-manager_0.2.0-2_all.deb \
  https://raw.githubusercontent.com/jerrecode/termux-users/main/dist/termux-user-manager_0.2.0-2_all.deb
apt install ./termux-user-manager_0.2.0-2_all.deb
```

### Option B: `wget`

```sh
cd "$HOME"
wget -O termux-user-manager_0.2.0-2_all.deb \
  https://raw.githubusercontent.com/jerrecode/termux-users/main/dist/termux-user-manager_0.2.0-2_all.deb
apt install ./termux-user-manager_0.2.0-2_all.deb
```

`apt install ./...deb` is preferred over plain `dpkg -i` because `apt` can resolve package dependencies.

After installation, validate the setup:

```sh
tuser doctor
tuser paths
```

On the standard `com.termux` installation, `tuser paths` should report a virtual-user home root similar to:

```text
/data/data/com.termux/files/users
```

while the existing Termux home remains:

```text
/data/data/com.termux/files/home
```

## Install from a cloned source tree

If you prefer to inspect and build the package yourself:

```sh
pkg install git python bash coreutils direnv
pkg install dpkg

git clone https://github.com/jerrecode/termux-users.git
cd termux-users
./tests/run-tests.sh
./build-deb.sh
apt install ./dist/termux-user-manager_0.2.0-2_all.deb
```

The build script also writes a SHA-256 checksum next to the package.

## Quick start

Create a virtual user:

```sh
tuseradd dev --full-name "Development"
```

List profiles:

```sh
tusers
```

Enter the profile:

```sh
tlogin dev
```

or:

```sh
tsu dev
```

Inside the virtual shell:

```sh
twhoami
echo "$HOME"
echo "$XDG_CONFIG_HOME"
echo "$XDG_DATA_HOME"
echo "$VIRTUAL_ENV"
which python
id -u
```

For a default installation, the logical identity and paths should resemble:

```text
twhoami
→ dev

HOME
→ /data/data/com.termux/files/users/dev

XDG_CONFIG_HOME
→ /data/data/com.termux/files/users/dev/.config

VIRTUAL_ENV
→ /data/data/com.termux/files/users/dev/.venvs/default
```

`id -u` will still return the real Android/Linux UID of the Termux application. That is expected.

Leave the virtual user and return to the ordinary Termux shell with:

```sh
exit
```

## Main commands

```text
tuser add NAME [options]
tuser list [--json]
tuser show NAME [--json]
tuser modify NAME [options]
tuser delete NAME [--remove-home --force]
tuser login NAME [-- COMMAND ...]
tuser passwd NAME [--delete]
tuser env NAME [--json|--export|--null]
tuser runtime python {create,enable,disable,remove} NAME
tuser proot-setup NAME DISTRO [options]
tuser proot NAME DISTRO [options] [-- COMMAND ...]
tuser whoami [--json]
tuser doctor [--json] [--repair]
tuser paths [--json]
tuser export [NAME] [--output FILE]
```

Convenience multicall names map to the same manager:

```text
tuseradd       tuser add
tuserdel       tuser delete
tusermod       tuser modify
tusers         tuser list
tlogin         tuser login
tsu            tuser login
twhoami        tuser whoami
tpasswd        tuser passwd
tuserenv       tuser env
tuserdoctor    tuser doctor
```

## User homes and existing data

New virtual users default to:

```text
$TERMUX__ROOTFS_DIR/users/<name>
```

The existing native Termux home at `$TERMUX__ROOTFS_DIR/home` is not renamed, repurposed, populated or used as the parent directory for virtual users.

The manager refuses to initialize an already non-empty custom home unless adoption is explicitly requested:

```sh
tuser add oldprofile --home /absolute/path --adopt-home
```

Even in adoption mode, existing files are preserved. The initializer only creates missing skeleton content and does not rewrite existing `.bashrc` or similar files.

## Environment separation

A virtual login constructs profile-specific values for variables including:

```text
HOME
USER
LOGNAME
XDG_CONFIG_HOME
XDG_DATA_HOME
XDG_STATE_HOME
XDG_CACHE_HOME
XDG_RUNTIME_DIR
TMPDIR
PATH
VIRTUAL_ENV
CARGO_HOME
RUSTUP_HOME
GRADLE_USER_HOME
GOPATH
GOMODCACHE
```

Persistent custom variables can be added to one profile:

```sh
tusermod dev --set-env EDITOR=nano
tusermod dev --set-env DEVELOPMENT_MODE=1
```

Remove one with:

```sh
tusermod dev --unset-env DEVELOPMENT_MODE
```

Inspect the exact generated environment:

```sh
tuserenv dev
```

or machine-readably:

```sh
tuser env dev --json
```

Core identity variables such as `HOME`, `USER`, `LOGNAME`, `PREFIX`, `PATH` and `SHELL` cannot be replaced through per-user custom environment metadata.

## Python virtual environments

By default, a virtual user gets:

```text
$HOME/.venvs/default
```

and that environment is activated through `PATH` for the profile.

Disable activation without deleting the environment:

```sh
tuser runtime python disable dev
```

Enable it again:

```sh
tuser runtime python enable dev
```

Recreate it explicitly:

```sh
tuser runtime python create dev --force
```

Remove it explicitly:

```sh
tuser runtime python remove dev --force
```

Destructive venv operations contain guards intended to prevent arbitrary-directory deletion.

## direnv

`direnv` is deliberately a project layer rather than the user-management mechanism itself:

```text
shared Termux system
        ↓
virtual user/profile
        ↓
direnv project environment
```

Generated Bash profiles enable the normal `direnv` hook when `direnv` is installed. Project `.envrc` files therefore work normally after the user approves them with `direnv allow`.

## Enable and disable profiles

Disable a profile without deleting it:

```sh
tusermod dev --disable
```

Re-enable it:

```sh
tusermod dev --enable
```

A disabled profile cannot be entered through `tlogin` until it is enabled again.

## Profile passwords

Set a convenience password:

```sh
tpasswd dev
```

Remove it:

```sh
tpasswd dev --delete
```

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. They protect the normal `tlogin` workflow but are **not equivalent to Unix account passwords**, because all virtual profiles ultimately run under the same real Termux UID and share the same underlying Android application security context.

## proot-distro integration

For a more conventional Linux userspace, install `proot-distro` and create a guest-side user mapping:

```sh
pkg install proot-distro
tuser proot-setup dev debian
```

This can create and remember a normal guest account inside the PRoot distribution. Then:

```sh
tuser proot dev debian
```

binds the Termux virtual home to the mapped guest home and requests login through `proot-distro --user`.

A different guest username/home can be selected explicitly:

```sh
tuser proot-setup development debian \
  --guest-user developer \
  --guest-home /home/developer

tuser proot development debian
```

This gives much more conventional `/etc/passwd` and `/home/<user>` behavior inside the guest, but PRoot is still userspace virtualization and does not create a separate Android/kernel security identity.

## Safe deletion

Deleting an account normally preserves its home and data:

```sh
tuserdel dev
```

To remove the managed home as well, the destructive intent must be explicit:

```sh
tuserdel dev --remove-home --force
```

Deleting a custom home outside the configured managed home root requires an additional explicit flag:

```sh
tuserdel dev --remove-home --force --allow-custom-home-delete
```

Important Termux/system paths are protected from recursive deletion even when force flags are used.

## Upgrade

Download the newer `.deb` and install it with `apt`:

```sh
apt install ./termux-user-manager_NEW_VERSION_all.deb
```

Existing virtual-account metadata and user homes are runtime state and are not package payload. Upgrades are designed to preserve them, including accounts whose home paths came from earlier versions.

## Uninstall

Remove the package itself with:

```sh
apt remove termux-user-manager
```

The package deliberately does not include uninstall scripts that recursively delete virtual-user homes or the manager's persistent state. This avoids turning package removal into data loss. If you want those files removed, inspect and remove them explicitly after uninstalling.

## Safety model

Installation places static package files under Termux `$PREFIX`. It does **not** modify the ordinary Termux `$HOME`, `$HOME/.bashrc`, `$HOME/.bash_profile`, `$PREFIX/etc/profile`, or `$PREFIX/etc/bash.bashrc`.

Virtual homes are created only when the user explicitly creates a profile. Account deletion preserves the home by default. Recursive deletion requires explicit destructive flags. Adopting an existing home preserves existing files.

`tlogin` also does not mutate the parent shell. It builds a child environment and starts a login shell or explicit command inside it. Exiting naturally returns to the original Termux environment.

## What this package intentionally does not do

It does not:

- create real kernel Unix UIDs/GIDs;
- provide kernel-enforced filesystem ownership isolation between profiles;
- change Android permissions;
- require root;
- modify SELinux;
- replace Termux `$PREFIX`;
- replace the existing Termux login environment;
- claim profile passwords are security boundaries.

Use separate Android/Linux users, containers with an appropriate security boundary, or native Linux accounts when hostile-user isolation is required.

## Development and tests

Run the current automated test suite from the repository root:

```sh
./tests/run-tests.sh
```

Build the Debian package:

```sh
./build-deb.sh
```

The resulting package and SHA-256 file are written to `dist/` by default.

## License

MIT. See [`LICENSE`](LICENSE).
