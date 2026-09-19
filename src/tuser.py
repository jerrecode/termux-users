#!/data/data/com.termux/files/usr/bin/python
"""Termux User Manager (TUM)

A non-privileged, non-destructive virtual user/profile manager for Termux.
It provides separate HOME/XDG/runtime/language environments while deliberately
not pretending to create kernel-level Android/Linux users.
"""
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

PACKAGE = "termux-user-manager"
VERSION = "0.2.0"
SCHEMA_VERSION = 1
DEFAULT_PREFIX = Path("/data/data/com.termux/files/usr")
NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED_NAMES = {"root", "system", "android", "shell", "nobody"}
PBKDF2_ITERATIONS = 310_000


class TUserError(RuntimeError):
    pass


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def prefix() -> Path:
    forced = os.environ.get("TUSER_PREFIX")
    if forced:
        return Path(forced).expanduser().resolve()
    env_prefix = os.environ.get("PREFIX") or os.environ.get("TERMUX__PREFIX")
    if env_prefix:
        p = Path(env_prefix)
        if p.is_absolute():
            return p
    here = Path(__file__).resolve()
    # Installed location: $PREFIX/libexec/termux-user-manager/tuser.py
    if here.parent.name == "termux-user-manager" and here.parent.parent.name == "libexec":
        return here.parent.parent.parent
    return DEFAULT_PREFIX


PREFIX = prefix()

def rootfs_dir() -> Path:
    forced = os.environ.get("TUSER_ROOTFS")
    if forced:
        p = Path(forced).expanduser()
        if not p.is_absolute():
            raise TUserError("TUSER_ROOTFS must be an absolute path")
        return p.resolve(strict=False)
    env_root = os.environ.get("TERMUX__ROOTFS_DIR")
    if env_root:
        p = Path(env_root)
        if p.is_absolute():
            return p.resolve(strict=False)
    # Standard Termux layout: $PREFIX == $TERMUX__ROOTFS_DIR/usr.
    return PREFIX.parent.resolve(strict=False)


ROOTFS_DIR = rootfs_dir()
ETC_DIR = PREFIX / "etc" / PACKAGE
STATE_DIR = PREFIX / "var" / "lib" / PACKAGE
RUN_DIR = PREFIX / "var" / "run" / PACKAGE
TMP_ROOT = PREFIX / "tmp" / PACKAGE
SHARE_DIR = PREFIX / "share" / PACKAGE
DB_PATH = STATE_DIR / "users.json"
LOCK_PATH = STATE_DIR / ".lock"
CONFIG_PATH = ETC_DIR / "config.json"
DEFAULT_HOME_ROOT = ROOTFS_DIR / "users"


def default_config() -> Dict[str, Any]:
    return {
        "schema": 1,
        "home_root": str(DEFAULT_HOME_ROOT),
        "default_shell": str(PREFIX / "bin" / "bash"),
        "default_python_venv": True,
        "enable_direnv": True,
        "clean_environment": True,
        "virtual_uid_start": 10000,
        "preserve_environment": [
            "TERM", "COLORTERM", "LANG", "LANGUAGE", "TZ",
            "DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER",
            "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK",
            "LD_PRELOAD", "LD_LIBRARY_PATH",
        ],
        "preserve_environment_prefixes": ["LC_", "TERMUX_", "ANDROID_"],
    }


def load_config() -> Dict[str, Any]:
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            incoming = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TUserError(f"cannot read {CONFIG_PATH}: {exc}") from exc
        if not isinstance(incoming, dict):
            raise TUserError(f"{CONFIG_PATH} must contain a JSON object")
        cfg.update(incoming)
    return cfg


def ensure_private_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass


def ensure_home_root(path: Path) -> None:
    """Ensure the configured virtual-home root exists without mutating existing data."""
    if path.exists():
        if not path.is_dir():
            raise TUserError(f"configured home_root is not a directory: {path}")
        return
    path.mkdir(parents=True, mode=0o700, exist_ok=False)


def ensure_state() -> None:
    ensure_private_dir(STATE_DIR)
    ensure_private_dir(RUN_DIR)
    ensure_private_dir(TMP_ROOT)
    ensure_home_root(Path(load_config()["home_root"]).expanduser())
    if not DB_PATH.exists():
        db = {
            "schema": SCHEMA_VERSION,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "next_virtual_uid": int(load_config().get("virtual_uid_start", 10000)),
            "users": {},
        }
        atomic_write_json(DB_PATH, db, mode=0o600)
        sync_compat_account_files(db)


def atomic_write_json(path: Path, obj: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def db_lock() -> Iterator[None]:
    ensure_private_dir(STATE_DIR)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_db() -> Dict[str, Any]:
    ensure_state()
    try:
        db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TUserError(f"cannot read account database {DB_PATH}: {exc}") from exc
    if not isinstance(db, dict) or db.get("schema") != SCHEMA_VERSION or not isinstance(db.get("users"), dict):
        raise TUserError(f"unsupported or malformed database: {DB_PATH}")
    return db


def sync_compat_account_files(db: Mapping[str, Any]) -> None:
    """Write passwd/group-shaped metadata files for Unix-like tooling and inspection.

    They are informational only: the Android/Linux kernel does not consult them.
    """
    passwd_lines: List[str] = []
    group_lines: List[str] = []
    for name, rec in sorted(db.get("users", {}).items()):
        gecos = str(rec.get("full_name", "")).replace(":", " ")
        home = str(rec.get("home", "")).replace(":", "_")
        shell = str(rec.get("shell", "")).replace(":", "_")
        uid = int(rec.get("virtual_uid", 0))
        gid = int(rec.get("virtual_gid", uid))
        passwd_lines.append(f"{name}:x:{uid}:{gid}:{gecos}:{home}:{shell}")
        group_lines.append(f"{name}:x:{gid}:{name}")
    for path, lines in ((STATE_DIR / "passwd", passwd_lines), (STATE_DIR / "group", group_lines)):
        content = "\n".join(lines) + ("\n" if lines else "")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def save_db(db: Dict[str, Any]) -> None:
    db["updated_at"] = utc_now()
    atomic_write_json(DB_PATH, db, mode=0o600)
    sync_compat_account_files(db)


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise TUserError("name must match ^[a-z_][a-z0-9_-]{0,31}$")
    if name in RESERVED_NAMES:
        raise TUserError(f"'{name}' is reserved; choose another virtual user name")
    return name


def require_user(db: Mapping[str, Any], name: str, allow_disabled: bool = True) -> Dict[str, Any]:
    users = db.get("users", {})
    rec = users.get(name)
    if rec is None:
        raise TUserError(f"virtual user '{name}' does not exist")
    if not allow_disabled and not rec.get("enabled", True):
        raise TUserError(f"virtual user '{name}' is disabled")
    return rec


def is_subpath(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def safe_mkdir(path: Path, mode: int = 0o700) -> None:
    if path.exists() and not path.is_dir():
        raise TUserError(f"expected a directory but found another object: {path}")
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass


def write_if_absent(path: Path, content: str, mode: int = 0o600) -> bool:
    """Create one file atomically, never replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def home_layout(home: Path) -> List[Tuple[Path, int]]:
    return [
        (home, 0o700),
        (home / ".config", 0o700),
        (home / ".config" / "npm", 0o700),
        (home / ".local", 0o700),
        (home / ".local" / "bin", 0o700),
        (home / ".local" / "share", 0o700),
        (home / ".local" / "state", 0o700),
        (home / ".cache", 0o700),
        (home / ".ssh", 0o700),
        (home / ".gnupg", 0o700),
        (home / ".venvs", 0o700),
        (home / "projects", 0o700),
    ]


def skeleton_files() -> Dict[str, Tuple[str, int]]:
    bash_profile = """# Generated by termux-user-manager. Safe to edit.\nif [ -r \"$HOME/.bashrc\" ]; then . \"$HOME/.bashrc\"; fi\n"""
    bashrc = """# Generated by termux-user-manager. Safe to edit.\n# Shared integration does not modify the normal Termux login environment.\nif [ -n \"${PREFIX:-}\" ] && [ -r \"$PREFIX/share/termux-user-manager/shell/bashrc\" ]; then\n    . \"$PREFIX/share/termux-user-manager/shell/bashrc\"\nfi\n"""
    profile = """# Generated by termux-user-manager. Safe to edit.\n# tlogin constructs HOME/XDG/PATH before starting this shell.\n"""
    npmrc = "prefix=${HOME}/.local\ncache=${HOME}/.cache/npm\n"
    gitignore = "# Per-profile global excludes file.\n"
    return {
        ".bash_profile": (bash_profile, 0o600),
        ".bashrc": (bashrc, 0o600),
        ".profile": (profile, 0o600),
        ".zshrc": ("# Generated by termux-user-manager. Safe to edit.\nif [ -n \"${PREFIX:-}\" ] && [ -r \"$PREFIX/share/termux-user-manager/shell/zshrc\" ]; then . \"$PREFIX/share/termux-user-manager/shell/zshrc\"; fi\n", 0o600),
        ".config/fish/config.fish": ("# Generated by termux-user-manager. Safe to edit.\nif test -n \"$PREFIX\"; and test -r \"$PREFIX/share/termux-user-manager/shell/fish.fish\"; source \"$PREFIX/share/termux-user-manager/shell/fish.fish\"; end\n", 0o600),
        ".config/npm/npmrc": (npmrc, 0o600),
        ".config/git/ignore": (gitignore, 0o600),
    }


def initialize_home(home: Path, adopt: bool = False) -> Dict[str, List[str]]:
    if home.exists() and any(home.iterdir()) and not adopt:
        raise TUserError(
            f"home already exists and is non-empty: {home}\n"
            "Use --adopt-home to use it without overwriting existing files."
        )
    created_dirs: List[str] = []
    created_files: List[str] = []
    for path, mode in home_layout(home):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))
            try:
                path.chmod(mode)
            except OSError:
                pass
    for rel, (content, mode) in skeleton_files().items():
        dest = home / rel
        if write_if_absent(dest, content, mode):
            created_files.append(str(dest))
    return {"directories": created_dirs, "files": created_files}


def find_executable(cmd: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    return shutil.which(cmd, path=(env or os.environ).get("PATH"))


def create_python_venv(home: Path, force: bool = False) -> Tuple[bool, str]:
    venv = home / ".venvs" / "default"
    python = PREFIX / "bin" / "python"
    if not python.exists():
        return False, f"Python not found at {python}; skipped venv creation"
    if venv.exists() and not force:
        return True, f"Python venv already exists: {venv}"
    if force and venv.exists():
        marker = venv / "pyvenv.cfg"
        if not marker.exists():
            return False, f"refusing to remove non-venv directory: {venv}"
        shutil.rmtree(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(python), "-m", "venv", str(venv)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode:
        return False, f"venv creation failed ({proc.returncode}): {proc.stdout.strip()}"
    return True, f"created Python venv: {venv}"


def hash_password(password: str) -> Dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {
        "scheme": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify_password(password: str, record: Mapping[str, Any]) -> bool:
    try:
        if record.get("scheme") != "pbkdf2-sha256":
            return False
        iterations = int(record["iterations"])
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
    except (KeyError, ValueError, TypeError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, expected)


def authenticate(rec: Mapping[str, Any]) -> None:
    pw = rec.get("password")
    if not pw:
        return
    # This is a profile lock, not a security boundary: all profiles share one OS UID.
    for _ in range(3):
        entered = getpass.getpass(f"Virtual password for {rec['name']}: ")
        if verify_password(entered, pw):
            return
        eprint("Authentication failed.")
    raise TUserError("too many failed authentication attempts")


def user_env(rec: Mapping[str, Any], cfg: Mapping[str, Any]) -> Dict[str, str]:
    home = Path(rec["home"])
    env: Dict[str, str] = {}
    clean = bool(cfg.get("clean_environment", True))
    if clean:
        preserve = set(str(x) for x in cfg.get("preserve_environment", []))
        prefixes = tuple(str(x) for x in cfg.get("preserve_environment_prefixes", []))
        for key, value in os.environ.items():
            if key in preserve or key.startswith(prefixes):
                env[key] = value
    else:
        env.update(os.environ)

    env.update({
        "PREFIX": str(PREFIX),
        "TERMUX_PREFIX": str(PREFIX),
        "HOME": str(home),
        "USER": str(rec["name"]),
        "LOGNAME": str(rec["name"]),
        "SHELL": str(rec["shell"]),
        "TERMUX_VIRTUAL_USER": str(rec["name"]),
        "TERMUX_VIRTUAL_UID": str(rec["virtual_uid"]),
        "TERMUX_VIRTUAL_GID": str(rec["virtual_gid"]),
        "TUSER_HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CARGO_HOME": str(home / ".cargo"),
        "RUSTUP_HOME": str(home / ".rustup"),
        "GRADLE_USER_HOME": str(home / ".gradle"),
        "GOPATH": str(home / "go"),
        "GOMODCACHE": str(home / "go" / "pkg" / "mod"),
        "NPM_CONFIG_USERCONFIG": str(home / ".config" / "npm" / "npmrc"),
        "npm_config_prefix": str(home / ".local"),
        "PYTHONUSERBASE": str(home / ".local"),
        "IPYTHONDIR": str(home / ".config" / "ipython"),
        "GNUPGHOME": str(home / ".gnupg"),
        "HISTFILE": str(home / ".bash_history"),
        "ZDOTDIR": str(home),
        "LESSHISTFILE": str(home / ".local" / "state" / "less" / "history"),
        "NODE_REPL_HISTORY": str(home / ".local" / "state" / "node_repl_history"),
        "SQLITE_HISTORY": str(home / ".local" / "state" / "sqlite_history"),
        "TUSER_DIRENV": "1" if cfg.get("enable_direnv", True) else "0",
    })

    runtime = RUN_DIR / rec["name"]
    tmp = TMP_ROOT / rec["name"]
    safe_mkdir(runtime, 0o700)
    safe_mkdir(tmp, 0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["TMPDIR"] = str(tmp)

    path_parts = [str(home / ".local" / "bin")]
    venv = home / ".venvs" / "default"
    if rec.get("python_venv", True) and (venv / "bin").is_dir():
        env["VIRTUAL_ENV"] = str(venv)
        env["VIRTUAL_ENV_PROMPT"] = f"({rec['name']}) "
        path_parts.append(str(venv / "bin"))
    path_parts.append(str(PREFIX / "bin"))
    env["PATH"] = ":".join(path_parts)

    # User-defined variables are explicit and cannot silently replace core identity.
    protected = {
        "PREFIX", "TERMUX_PREFIX", "HOME", "USER", "LOGNAME", "SHELL", "PATH",
        "TERMUX_VIRTUAL_USER", "TERMUX_VIRTUAL_UID", "TERMUX_VIRTUAL_GID",
    }
    for key, value in rec.get("env", {}).items():
        if key not in protected:
            env[str(key)] = str(value)
    return env


def shell_argv(shell: str) -> List[str]:
    base = Path(shell).name
    if base in {"bash", "zsh", "fish", "sh", "dash", "ksh"}:
        return [shell, "-l"]
    return [shell]


def exec_as_user(rec: Mapping[str, Any], command: Sequence[str], no_auth: bool = False) -> None:
    if not rec.get("enabled", True):
        raise TUserError(f"virtual user '{rec['name']}' is disabled")
    if not no_auth:
        authenticate(rec)
    cfg = load_config()
    env = user_env(rec, cfg)
    home = Path(rec["home"])
    if not home.is_dir():
        raise TUserError(f"home directory is missing: {home}")
    os.chdir(home)
    if command:
        exe = shutil.which(command[0], path=env.get("PATH"))
        if exe is None and os.path.isabs(command[0]) and os.access(command[0], os.X_OK):
            exe = command[0]
        if exe is None:
            raise TUserError(f"command not found in virtual user's PATH: {command[0]}")
        os.execve(exe, list(command), env)
    shell = str(rec["shell"])
    if not Path(shell).exists():
        raise TUserError(f"configured shell does not exist: {shell}")
    os.execve(shell, shell_argv(shell), env)


def cmd_add(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    cfg = load_config()
    with db_lock():
        db = load_db()
        if name in db["users"]:
            raise TUserError(f"virtual user '{name}' already exists")
        home_root = Path(cfg["home_root"]).expanduser()
        home = Path(args.home).expanduser() if args.home else home_root / name
        if not home.is_absolute():
            raise TUserError("home path must be absolute")
        # Default homes are kept under the manager-owned root. Custom homes are allowed,
        # but the program never recursively deletes them unless explicitly confirmed.
        shell = Path(args.shell or cfg["default_shell"])
        if not shell.is_absolute() or not shell.exists():
            raise TUserError(f"shell does not exist: {shell}")
        layout = initialize_home(home, adopt=args.adopt_home)
        uid = int(db.get("next_virtual_uid", cfg.get("virtual_uid_start", 10000)))
        used = {int(u.get("virtual_uid", -1)) for u in db["users"].values()}
        while uid in used:
            uid += 1
        db["next_virtual_uid"] = uid + 1
        rec = {
            "name": name,
            "full_name": args.full_name or "",
            "virtual_uid": uid,
            "virtual_gid": uid,
            "groups": sorted(set(args.groups.split(",") if args.groups else ["users"])),
            "home": str(home),
            "shell": str(shell),
            "enabled": True,
            "python_venv": not args.no_python_venv and bool(cfg.get("default_python_venv", True)),
            "env": {},
            "password": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        db["users"][name] = rec
        save_db(db)
    print(f"Created virtual user: {name}")
    print(f"  home: {home}")
    print(f"  virtual uid/gid: {uid}:{uid}")
    print(f"  shell: {shell}")
    if layout["files"]:
        print(f"  skeleton files created: {len(layout['files'])}")
    if rec["python_venv"]:
        ok, msg = create_python_venv(home)
        print(f"  {msg}")
        if not ok:
            print("  The account is still usable; run `tuser runtime python create NAME` later.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = load_db()
    rows = []
    for name, rec in sorted(db["users"].items()):
        rows.append({
            "name": name,
            "uid": rec["virtual_uid"],
            "gid": rec["virtual_gid"],
            "enabled": bool(rec.get("enabled", True)),
            "home": rec["home"],
            "shell": rec["shell"],
            "venv": bool(rec.get("python_venv", True)),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No virtual users.")
        return 0
    widths = {
        "name": max(4, *(len(r["name"]) for r in rows)),
        "uid": max(3, *(len(str(r["uid"])) for r in rows)),
    }
    print(f"{'NAME':<{widths['name']}}  {'VUID':>{widths['uid']}}  STATE     VENV  HOME")
    for r in rows:
        state = "enabled" if r["enabled"] else "disabled"
        venv = "yes" if r["venv"] else "no"
        print(f"{r['name']:<{widths['name']}}  {r['uid']:>{widths['uid']}}  {state:<8}  {venv:<4}  {r['home']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = load_db()
    rec = require_user(db, args.name)
    out = dict(rec)
    out["password"] = bool(rec.get("password"))
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for key, value in out.items():
            print(f"{key}: {value}")
    return 0


def cmd_modify(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    with db_lock():
        db = load_db()
        rec = require_user(db, name)
        changed = False
        if args.full_name is not None:
            rec["full_name"] = args.full_name
            changed = True
        if args.shell is not None:
            shell = Path(args.shell)
            if not shell.is_absolute() or not shell.exists():
                raise TUserError(f"shell does not exist: {shell}")
            rec["shell"] = str(shell)
            changed = True
        if args.enable:
            rec["enabled"] = True
            changed = True
        if args.disable:
            rec["enabled"] = False
            changed = True
        if args.groups is not None:
            rec["groups"] = sorted(set(x for x in args.groups.split(",") if x))
            changed = True
        if args.python_venv:
            rec["python_venv"] = True
            changed = True
        if args.no_python_venv:
            rec["python_venv"] = False
            changed = True
        if args.set_env:
            env = rec.setdefault("env", {})
            for item in args.set_env:
                if "=" not in item:
                    raise TUserError(f"--set-env requires NAME=VALUE, got: {item}")
                key, value = item.split("=", 1)
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise TUserError(f"invalid environment variable name: {key}")
                env[key] = value
            changed = True
        if args.unset_env:
            env = rec.setdefault("env", {})
            for key in args.unset_env:
                env.pop(key, None)
            changed = True
        if not changed:
            raise TUserError("no modification requested")
        rec["updated_at"] = utc_now()
        save_db(db)
    print(f"Updated virtual user: {name}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    with db_lock():
        db = load_db()
        rec = require_user(db, name)
        home = Path(rec["home"])
        if args.remove_home:
            if not args.force:
                raise TUserError("--remove-home requires --force; without it, no files are deleted")
            cfg_root = Path(load_config()["home_root"]).resolve(strict=False)
            resolved_home = home.resolve(strict=False)
            protected = {
                Path("/").resolve(),
                PREFIX.resolve(strict=False),
                ROOTFS_DIR.resolve(strict=False),
                cfg_root,
                (ROOTFS_DIR / "home").resolve(strict=False),
            }
            real_termux_home = os.environ.get("TERMUX__HOME")
            if real_termux_home:
                protected.add(Path(real_termux_home).resolve(strict=False))
            if resolved_home in protected:
                raise TUserError(f"refusing to recursively delete protected path: {resolved_home}")
            if not is_subpath(resolved_home, cfg_root) and not args.allow_custom_home_delete:
                raise TUserError(
                    f"refusing to recursively delete custom home {resolved_home}; add --allow-custom-home-delete as well"
                )
            if home.exists():
                shutil.rmtree(home)
                print(f"Removed home: {home}")
        else:
            print(f"Preserving home: {home}")
        del db["users"][name]
        save_db(db)
    runtime = RUN_DIR / name
    tmp = TMP_ROOT / name
    for transient in (runtime, tmp):
        if transient.exists() and is_subpath(transient, transient.parent):
            shutil.rmtree(transient, ignore_errors=True)
    print(f"Removed account record: {name}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    db = load_db()
    rec = require_user(db, validate_name(args.name), allow_disabled=False)
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    exec_as_user(rec, command, no_auth=args.no_auth)
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    name = os.environ.get("TERMUX_VIRTUAL_USER")
    if args.json:
        print(json.dumps({
            "virtual": bool(name),
            "name": name,
            "virtual_uid": os.environ.get("TERMUX_VIRTUAL_UID"),
            "virtual_gid": os.environ.get("TERMUX_VIRTUAL_GID"),
            "real_uid": os.getuid(),
            "home": os.environ.get("HOME"),
        }, indent=2))
    elif name:
        print(name)
    else:
        print(getpass.getuser())
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    with db_lock():
        db = load_db()
        rec = require_user(db, name)
        if args.delete:
            rec["password"] = None
            rec["updated_at"] = utc_now()
            save_db(db)
            print(f"Removed virtual password for {name}")
            return 0
        p1 = getpass.getpass(f"New virtual password for {name}: ")
        if not p1:
            raise TUserError("empty passwords are not accepted; use --delete for no password")
        p2 = getpass.getpass("Retype new virtual password: ")
        if p1 != p2:
            raise TUserError("passwords do not match")
        rec["password"] = hash_password(p1)
        rec["updated_at"] = utc_now()
        save_db(db)
    print(f"Updated virtual password for {name}")
    print("Note: this is a profile lock only; it is not a kernel-enforced security boundary.")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    db = load_db()
    rec = require_user(db, validate_name(args.name))
    env = user_env(rec, load_config())
    if args.json:
        print(json.dumps(env, indent=2, sort_keys=True))
    elif args.null:
        for key in sorted(env):
            sys.stdout.buffer.write(f"{key}={env[key]}".encode() + b"\0")
    elif args.export:
        for key in sorted(env):
            print(f"export {key}={shlex.quote(env[key])}")
    else:
        for key in sorted(env):
            print(f"{key}={env[key]}")
    return 0


def cmd_runtime_python(args: argparse.Namespace) -> int:
    db = load_db()
    rec = require_user(db, validate_name(args.name))
    home = Path(rec["home"])
    venv = home / ".venvs" / "default"
    if args.action == "create":
        ok, msg = create_python_venv(home, force=args.force)
        print(msg)
        if ok:
            with db_lock():
                db = load_db()
                db["users"][args.name]["python_venv"] = True
                db["users"][args.name]["updated_at"] = utc_now()
                save_db(db)
            return 0
        return 1
    if args.action == "disable":
        with db_lock():
            db = load_db()
            require_user(db, args.name)["python_venv"] = False
            save_db(db)
        print(f"Disabled automatic Python venv activation for {args.name}; files were preserved at {venv}")
        return 0
    if args.action == "enable":
        if not (venv / "bin").is_dir():
            raise TUserError(f"venv not found: {venv}; create it first")
        with db_lock():
            db = load_db()
            require_user(db, args.name)["python_venv"] = True
            save_db(db)
        print(f"Enabled automatic Python venv activation for {args.name}")
        return 0
    if args.action == "remove":
        if not args.force:
            raise TUserError("removing a venv deletes files and therefore requires --force")
        if venv.exists():
            if not (venv / "pyvenv.cfg").exists():
                raise TUserError(f"refusing to remove directory without pyvenv.cfg: {venv}")
            shutil.rmtree(venv)
        with db_lock():
            db = load_db()
            require_user(db, args.name)["python_venv"] = False
            save_db(db)
        print(f"Removed Python venv for {args.name}")
        return 0
    raise TUserError(f"unknown Python runtime action: {args.action}")


def cmd_proot_setup(args: argparse.Namespace) -> int:
    pd = shutil.which("proot-distro")
    if not pd:
        raise TUserError("proot-distro is not installed; install it with `pkg install proot-distro`")
    name = validate_name(args.name)
    db = load_db()
    rec = require_user(db, name, allow_disabled=False)
    authenticate(rec)
    guest_user = validate_name(args.guest_user or name)
    guest_home = args.guest_home or f"/home/{guest_user}"
    guest_shell = args.guest_shell or "/bin/bash"
    if not guest_home.startswith("/") or not guest_shell.startswith("/"):
        raise TUserError("guest home and shell must be absolute guest paths")
    script = r'''
set -eu
name=$1
home=$2
shell=$3
gecos=$4
case "$name" in *[!a-zA-Z0-9_-]*|'') echo "invalid guest user" >&2; exit 2;; esac
if grep -q "^${name}:" /etc/passwd 2>/dev/null; then
    echo "Guest account already exists: $name"
    exit 0
fi
command -v useradd >/dev/null 2>&1 || { echo "guest does not provide useradd" >&2; exit 3; }
if ! grep -q "^${name}:" /etc/group 2>/dev/null; then
    command -v groupadd >/dev/null 2>&1 || { echo "guest does not provide groupadd" >&2; exit 3; }
    groupadd "$name"
fi
if [ ! -x "$shell" ]; then shell=/bin/sh; fi
useradd -m -g "$name" -d "$home" -s "$shell" -c "$gecos" "$name"
echo "Created guest account: $name ($home, $shell)"
'''
    cmd = [
        pd, "login", args.distro, "--", "/bin/sh", "-c", script, "tuser-proot-setup",
        guest_user, guest_home, guest_shell, str(rec.get("full_name", "")),
    ]
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise TUserError(f"guest account setup failed with exit status {proc.returncode}")
    with db_lock():
        db = load_db()
        rec = require_user(db, name)
        mappings = rec.setdefault("proot", {})
        mappings[args.distro] = {"guest_user": guest_user, "guest_home": guest_home}
        rec["updated_at"] = utc_now()
        save_db(db)
    print(f"Saved proot mapping: {name} -> {args.distro}:{guest_user} ({guest_home})")
    return 0


def cmd_proot(args: argparse.Namespace) -> int:
    pd = shutil.which("proot-distro")
    if not pd:
        raise TUserError("proot-distro is not installed; install it with `pkg install proot-distro`")
    db = load_db()
    rec = require_user(db, validate_name(args.name), allow_disabled=False)
    authenticate(rec)
    home = Path(rec["home"])
    mapping = rec.get("proot", {}).get(args.distro, {})
    guest_user = args.guest_user or mapping.get("guest_user")
    guest_home = args.guest_home or mapping.get("guest_home") or f"/home/{guest_user or rec['name']}"
    cmd = [pd, "login", args.distro]
    if guest_user:
        cmd += ["--user", guest_user]
    cmd += ["--bind", f"{home}:{guest_home}", "--work-dir", guest_home]
    cmd += ["--env", f"TERMUX_VIRTUAL_USER={rec['name']}"]
    cmd += ["--env", f"TERMUX_VIRTUAL_UID={rec['virtual_uid']}"]
    cmd += ["--env", f"TERMUX_VIRTUAL_GID={rec['virtual_gid']}"]
    if args.shared_tmp:
        cmd.append("--shared-tmp")
    if args.shared_x11:
        cmd.append("--shared-x11")
    extra = list(args.command or [])
    if extra:
        cmd.append("--")
        cmd.extend(extra)
    os.execv(pd, cmd)
    return 0

def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    findings: List[Dict[str, Any]] = []

    def add(level: str, check: str, message: str) -> None:
        findings.append({"level": level, "check": check, "message": message})

    if PREFIX.is_dir():
        add("ok", "prefix", str(PREFIX))
    else:
        add("error", "prefix", f"missing: {PREFIX}")
    if (PREFIX / "bin").is_dir():
        add("ok", "bin", str(PREFIX / "bin"))
    else:
        add("error", "bin", "PREFIX/bin missing")
    try:
        db = load_db()
        add("ok", "database", f"{len(db['users'])} virtual user(s)")
    except TUserError as exc:
        db = {"users": {}}
        add("error", "database", str(exc))
    if shutil.which("direnv"):
        add("ok", "direnv", shutil.which("direnv") or "found")
    else:
        add("warn", "direnv", "not installed; project-scoped environments will not auto-load")
    if (PREFIX / "bin" / "python").exists():
        add("ok", "python", str(PREFIX / "bin" / "python"))
    else:
        add("warn", "python", "not found; Python venv integration unavailable")
    if shutil.which("proot-distro"):
        add("ok", "proot-distro", shutil.which("proot-distro") or "found")
    else:
        add("info", "proot-distro", "optional integration not installed")
    home_root = Path(cfg["home_root"])
    for name, rec in db.get("users", {}).items():
        home = Path(rec.get("home", ""))
        if not home.is_dir():
            add("error", f"user:{name}", f"home missing: {home}")
        elif not os.access(home, os.R_OK | os.W_OK | os.X_OK):
            add("error", f"user:{name}", f"home is not fully accessible: {home}")
        else:
            add("ok", f"user:{name}", f"home accessible: {home}")
        shell = Path(rec.get("shell", ""))
        if not shell.exists():
            add("error", f"user:{name}:shell", f"missing: {shell}")
        if args.repair:
            try:
                initialize_home(home, adopt=True)
            except TUserError as exc:
                add("error", f"user:{name}:repair", str(exc))
    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        for item in findings:
            print(f"[{item['level'].upper():5}] {item['check']}: {item['message']}")
    return 1 if any(x["level"] == "error" for x in findings) else 0


def cmd_paths(args: argparse.Namespace) -> int:
    data = {
        "prefix": str(PREFIX),
        "rootfs": str(ROOTFS_DIR),
        "config": str(CONFIG_PATH),
        "state": str(STATE_DIR),
        "database": str(DB_PATH),
        "virtual_passwd": str(STATE_DIR / "passwd"),
        "virtual_group": str(STATE_DIR / "group"),
        "runtime": str(RUN_DIR),
        "temporary": str(TMP_ROOT),
        "share": str(SHARE_DIR),
        "home_root": str(Path(load_config()["home_root"])),
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for k, v in data.items():
            print(f"{k}: {v}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = load_db()
    if args.name:
        rec = dict(require_user(db, validate_name(args.name)))
        rec["password"] = None
        payload = {"schema": SCHEMA_VERSION, "users": {args.name: rec}}
    else:
        payload = json.loads(json.dumps(db))
        for rec in payload.get("users", {}).values():
            rec["password"] = None
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output).expanduser()
        if path.exists() and not args.force:
            raise TUserError(f"refusing to overwrite existing file: {path}; add --force")
        path.write_text(text, encoding="utf-8")
        print(f"Exported metadata (password hashes omitted): {path}")
    else:
        print(text, end="")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"{PACKAGE} {VERSION}")
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tuser",
        description="Non-destructive virtual user/profile management for Termux.",
        epilog="Virtual users share Termux's real Android/Linux UID; this is environment/config separation, not a security boundary.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="subcmd", required=True)

    q = sub.add_parser("add", aliases=["useradd"], help="create a virtual user")
    q.add_argument("name")
    q.add_argument("--full-name", default="")
    q.add_argument("--home")
    q.add_argument("--shell")
    q.add_argument("--groups", help="comma-separated virtual metadata groups")
    q.add_argument("--adopt-home", action="store_true", help="use an existing home without overwriting its files")
    q.add_argument("--no-python-venv", action="store_true")
    q.set_defaults(func=cmd_add)

    q = sub.add_parser("list", aliases=["users"], help="list virtual users")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_list)

    q = sub.add_parser("show", help="show one account")
    q.add_argument("name")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_show)

    q = sub.add_parser("modify", aliases=["usermod"], help="modify an account without moving/deleting its home")
    q.add_argument("name")
    q.add_argument("--full-name")
    q.add_argument("--shell")
    q.add_argument("--groups")
    state = q.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    py = q.add_mutually_exclusive_group()
    py.add_argument("--python-venv", action="store_true")
    py.add_argument("--no-python-venv", action="store_true")
    q.add_argument("--set-env", action="append", metavar="NAME=VALUE")
    q.add_argument("--unset-env", action="append", metavar="NAME")
    q.set_defaults(func=cmd_modify)

    q = sub.add_parser("delete", aliases=["userdel"], help="remove account metadata; preserve home by default")
    q.add_argument("name")
    q.add_argument("--remove-home", action="store_true")
    q.add_argument("--force", action="store_true")
    q.add_argument("--allow-custom-home-delete", action="store_true")
    q.set_defaults(func=cmd_delete)

    q = sub.add_parser("login", help="enter a virtual user's shell or execute a command")
    q.add_argument("name")
    q.add_argument("--no-auth", action="store_true", help="skip optional profile password check")
    q.add_argument("command", nargs="*")
    q.set_defaults(func=cmd_login)

    q = sub.add_parser("whoami", help="show current virtual identity")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_whoami)

    q = sub.add_parser("passwd", help="set/delete an optional virtual profile password")
    q.add_argument("name")
    q.add_argument("--delete", action="store_true")
    q.set_defaults(func=cmd_passwd)

    q = sub.add_parser("env", help="show the constructed environment for a user")
    q.add_argument("name")
    form = q.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true")
    form.add_argument("--export", action="store_true", help="emit shell export statements")
    form.add_argument("--null", action="store_true", help="NUL-delimited NAME=VALUE records")
    q.set_defaults(func=cmd_env)

    q = sub.add_parser("runtime", help="manage per-user language runtimes")
    rsub = q.add_subparsers(dest="runtime", required=True)
    r = rsub.add_parser("python", help="manage the default per-user Python venv")
    r.add_argument("action", choices=["create", "enable", "disable", "remove"])
    r.add_argument("name")
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_runtime_python)

    q = sub.add_parser("proot-setup", help="create/map a normal guest account inside an installed proot-distro")
    q.add_argument("name")
    q.add_argument("distro")
    q.add_argument("--guest-user")
    q.add_argument("--guest-home")
    q.add_argument("--guest-shell", default="/bin/bash")
    q.set_defaults(func=cmd_proot_setup)

    q = sub.add_parser("proot", help="bridge a virtual user into an installed proot-distro")
    q.add_argument("name")
    q.add_argument("distro")
    q.add_argument("--guest-user", help="existing account inside the distro; passed to proot-distro --user")
    q.add_argument("--guest-home", help="bind destination; default /home/<virtual-user>")
    q.add_argument("--shared-tmp", action="store_true")
    q.add_argument("--shared-x11", action="store_true")
    q.add_argument("command", nargs="*")
    q.set_defaults(func=cmd_proot)

    q = sub.add_parser("doctor", help="validate installation/accounts; optionally create missing skeleton items")
    q.add_argument("--json", action="store_true")
    q.add_argument("--repair", action="store_true", help="only create missing directories/skeleton files; never overwrite")
    q.set_defaults(func=cmd_doctor)

    q = sub.add_parser("paths", help="show manager paths")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_paths)

    q = sub.add_parser("export", help="export account metadata; password hashes are always omitted")
    q.add_argument("name", nargs="?")
    q.add_argument("--output")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_export)

    return p


def dispatch_compat(argv: List[str]) -> List[str]:
    """Map multicall wrapper names to tuser subcommands."""
    invoked = Path(sys.argv[0]).name
    mapping = {
        "tuseradd": "add",
        "tuserdel": "delete",
        "tusermod": "modify",
        "tusers": "list",
        "tlogin": "login",
        "tsu": "login",
        "twhoami": "whoami",
        "tpasswd": "passwd",
        "tuserenv": "env",
        "tuserdoctor": "doctor",
    }
    if invoked in mapping:
        return [mapping[invoked], *argv]
    return argv


def main(argv: Optional[Sequence[str]] = None) -> int:
    argsv = list(argv if argv is not None else sys.argv[1:])
    argsv = dispatch_compat(argsv)
    parser = make_parser()
    try:
        args = parser.parse_args(argsv)
        return int(args.func(args))
    except TUserError as exc:
        eprint(f"tuser: error: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("tuser: interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
