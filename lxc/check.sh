#!/usr/bin/env bash
#
# Checks the built template. Run with `make check` after `make build`.
#
# On an amd64 host it boots the template with systemd as PID 1, the way Proxmox
# will, and checks first boot, the services, the UI and the tools.
#
# Elsewhere (an Apple Silicon Mac) the template can only run under emulation,
# where systemd cannot start any service: it launches each one through a helper
# the emulator cannot execute, so even journald fails. There the same checks
# run on the parts directly -- first boot, the sysctl, the UI, sign-in, the
# tools -- and the result says the boot itself was not tested.
#
#   KEEP=1 make check    leave the container running to look around

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(tr -d ' \n' < VERSION)"
IMAGE="dist/debian-13-haproxy-manager_${VERSION}_amd64.tar.zst"
COMPOSE=(docker compose --progress quiet -f lxc/compose.yaml)

[ -f "$IMAGE" ] || { echo "no $IMAGE -- run make build first" >&2; exit 1; }

case "$(docker info --format '{{.Architecture}}')" in
    x86_64|amd64) BOOT=1 ;;
    *)            BOOT=0 ;;
esac

echo ">> Verifying the checksum"
( cd dist && shasum -a 256 -c "$(basename "$IMAGE").sha256" )

# The image build context needs a plain tar: Docker's ADD does not unpack zstd.
rm -rf dist/check && mkdir -p dist/check
zstd -qdc "$IMAGE" > dist/check/rootfs.tar

# Nothing that must be unique to a node may be in the template itself.
echo ">> Checking the template carries nothing node-specific"
static() {
    local what="$1" member="$2"
    if tar -tf dist/check/rootfs.tar "$member" >/dev/null 2>&1 \
       && [ -n "$(tar -xOf dist/check/rootfs.tar "$member" 2>/dev/null)" ]; then
        printf '   FAIL  %s\n' "$what"; STATIC_FAIL=1
    else
        printf '   ok    %s\n' "$what"
    fi
}
STATIC_FAIL=0
static "no machine-id"          ./etc/machine-id
static "no SSH host key"        ./etc/ssh/ssh_host_ed25519_key
static "no manager config"      ./var/lib/haproxy-manager/config.json
static "no admin password"      ./var/lib/haproxy-manager/admin-credentials.txt
static "no resolver of its own" ./etc/resolv.conf

cleanup() {
    [ -n "${KEEP:-}" ] && { echo ">> Left running: docker compose -f lxc/compose.yaml exec check bash"; return; }
    "${COMPOSE[@]}" down --remove-orphans --timeout 5 >/dev/null 2>&1 || true
    rm -rf dist/check
}
trap cleanup EXIT

run() { "${COMPOSE[@]}" exec -T check "$@"; }
fail=$STATIC_FAIL
ok()  { printf '   ok    %s\n' "$1"; }
bad() { printf '   FAIL  %s\n' "$1"; fail=1; }
expect() { local what="$1"; shift; if run "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }

if [ "$BOOT" = 1 ]; then
    echo ">> Booting the template with systemd"
    CHECK_CMD=/sbin/init "${COMPOSE[@]}" up -d --build check >/dev/null
    state=""
    for _ in $(seq 90); do
        state="$(run systemctl is-system-running 2>/dev/null || true)"
        case "$state" in running|degraded) break ;; esac
        sleep 2
    done
    echo "   system: $state"
    expect "ham-firstboot ran"            systemctl is-active ham-firstboot
    expect "ham-sysctl ran"               systemctl is-active ham-sysctl
    expect "sshd is running"              systemctl is-active ssh
    expect "haproxy-manager is running"   systemctl is-active haproxy-manager
else
    echo ">> Emulated amd64: checking the parts without booting systemd"
    CHECK_CMD="sleep infinity" "${COMPOSE[@]}" up -d --build check >/dev/null
    expect "ham-firstboot runs"           /usr/local/sbin/ham-firstboot
    expect "the ham-sysctl command runs"  /usr/sbin/sysctl -q -e -p /etc/sysctl.d/60-haproxy-manager.conf
    run sh -c 'nohup /usr/bin/python3 /opt/haproxy-manager/app.py >/var/log/ham-check.log 2>&1 &'
fi

expect "the units are enabled" sh -c '
    for u in ham-firstboot ham-sysctl haproxy-manager haproxy keepalived ssh; do
        systemctl is-enabled "$u" >/dev/null || exit 1
    done'
expect "ip_nonlocal_bind is on"         sh -c '[ "$(cat /proc/sys/net/ipv4/ip_nonlocal_bind)" = 1 ]'
expect "SSH host keys were generated"   test -s /etc/ssh/ssh_host_ed25519_key
expect "the admin login was seeded"     test -s /var/lib/haproxy-manager/admin-credentials.txt
expect "the API key was seeded"         test -s /var/lib/haproxy-manager/api-key.txt

answered=0
for _ in $(seq 30); do
    if run curl -fs -o /dev/null http://127.0.0.1:8080/api/whoami 2>/dev/null; then answered=1; break; fi
    sleep 2
done
if [ "$answered" = 1 ]; then ok "the UI answers on 8080"; else bad "the UI answers on 8080"; fi

expect "sign-in with the seeded login" sh -c '
    pw=$(awk "/^password:/{print \$2}" /var/lib/haproxy-manager/admin-credentials.txt)
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8080/api/login \
           -H "Content-Type: application/json" -d "{\"username\":\"admin\",\"password\":\"$pw\"}")
    [ "$code" = 200 ]'
expect "updates come from invite-networks" \
    grep -q '"HAM_REPO", "invite-networks/haproxy-manager"' /opt/haproxy-manager/ham/base.py

for tool in ssh sshd traceroute mtr htop curl login tcpdump ping; do
    expect "$tool is installed" sh -c "command -v $tool"
done

if [ "$fail" = 1 ]; then
    echo ">> Failed checks"
    if [ "$BOOT" = 1 ]; then run journalctl -b --no-pager -p warning | tail -40 || true
    else run tail -20 /var/log/ham-check.log || true; fi
    exit 1
fi
if [ "$BOOT" = 1 ]; then
    echo ">> The template works"
else
    echo ">> The template's parts work. The systemd boot was not tested here: run"
    echo "   make check on an amd64 Linux host, or create a container in Proxmox."
fi
