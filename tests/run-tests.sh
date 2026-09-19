#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TUSER="$ROOT/src/tuser.py"
TEST="$(mktemp -d)"
ADOPT="$(mktemp -d)"
MOCK="$(mktemp -d)"
trap 'rm -rf "$TEST" "$ADOPT" "$MOCK" /tmp/tuser-test-proot-argv.$$' EXIT
mkdir -p "$TEST/bin"

export TUSER_PREFIX="$TEST"
export TUSER_ROOTFS="$TEST/rootfs"
python3 "$TUSER" add dev --shell /bin/bash --no-python-venv >/dev/null
python3 "$TUSER" list --json | python3 -c 'import json,sys; x=json.load(sys.stdin); assert len(x)==1 and x[0]["name"]=="dev"'
python3 "$TUSER" env dev --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["USER"]=="dev" and d["HOME"].endswith("/rootfs/users/dev") and d["XDG_CONFIG_HOME"].endswith("/.config")'
python3 "$TUSER" login dev -- /bin/sh -c '[ "$USER" = dev ] && [ "$TERMUX_VIRTUAL_USER" = dev ]'

grep -q '^dev:x:10000:10000:' "$TEST/var/lib/termux-user-manager/passwd"
grep -q '^dev:x:10000:dev$' "$TEST/var/lib/termux-user-manager/group"

printf 'CUSTOM\n' > "$ADOPT/.bashrc"
chmod 0755 "$ADOPT"
python3 "$TUSER" add adopt --home "$ADOPT" --adopt-home --shell /bin/bash --no-python-venv >/dev/null
grep -qx CUSTOM "$ADOPT/.bashrc"
[ "$(stat -c %a "$ADOPT")" = 755 ]
python3 "$TUSER" paths --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["rootfs"].endswith("/rootfs") and d["home_root"].endswith("/rootfs/users")' 
python3 "$TUSER" delete adopt >/dev/null
grep -qx CUSTOM "$ADOPT/.bashrc"

python3 "$TUSER" modify dev --set-env EDITOR=nano --disable >/dev/null
if python3 "$TUSER" login dev -- /bin/true >/dev/null 2>&1; then
    echo "disabled login unexpectedly succeeded" >&2; exit 1
fi
python3 "$TUSER" modify dev --enable >/dev/null

cat > "$MOCK/proot-distro" <<MOCKSCRIPT
#!/bin/sh
printf '%s\n' "\$@" > /tmp/tuser-test-proot-argv.$$
exit 0
MOCKSCRIPT
chmod +x "$MOCK/proot-distro"
PATH="$MOCK:$PATH" python3 "$TUSER" proot-setup dev debian --guest-user developer --guest-home /home/developer >/dev/null
PATH="$MOCK:$PATH" python3 "$TUSER" proot dev debian -- /bin/echo ok
python3 - <<PY
p='/tmp/tuser-test-proot-argv.$$'
a=open(p).read().splitlines()
assert '--user' in a and a[a.index('--user')+1]=='developer'
assert '--work-dir' in a and a[a.index('--work-dir')+1]=='/home/developer'
PY

# Account deletion keeps home by default.
HOME_PATH="$TEST/rootfs/users/dev"
printf 'KEEP\n' > "$HOME_PATH/keep.txt"
python3 "$TUSER" delete dev >/dev/null
test -f "$HOME_PATH/keep.txt"

echo "All termux-user-manager tests passed."
