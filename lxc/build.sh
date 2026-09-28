#!/usr/bin/env bash
#
# Builds the Proxmox LXC template: Debian 13 (trixie), amd64, with HAProxy
# Cluster Manager installed by install.sh exactly as on a server.
#
# Runs inside the builder container (lxc/compose.yaml), with the repository
# at /src and the output directory at /dist. Use `make build`, not this.

set -euo pipefail

SUITE=trixie
ARCH=amd64
MIRROR=http://deb.debian.org/debian
SECURITY=http://security.debian.org/debian-security

VERSION="$(tr -d ' \n' < /src/VERSION)"
NAME="debian-13-haproxy-manager_${VERSION}_${ARCH}"
OUT="/dist/$NAME.tar.zst"

# What an operator needs on the box, plus what Proxmox needs to configure it:
# ifupdown for /etc/network/interfaces, which is what Proxmox writes for a
# Debian container. The manager's own dependencies are installed here too, so
# install.sh finds them already present.
PACKAGES=(
    # base system
    systemd systemd-sysv dbus ifupdown isc-dhcp-client ca-certificates tzdata
    # login is not Essential in Debian 13, so a minimal bootstrap leaves it out
    login openssh-server
    # operator tools
    curl traceroute mtr-tiny htop tcpdump iputils-ping iproute2 procps
    # haproxy-manager
    python3 python3-flask python3-requests python3-waitress
    haproxy keepalived openssl socat iputils-arping tar
)
# shellcheck disable=SC2206 # a space-separated list, split on purpose
[ -n "${EXTRA_PACKAGES:-}" ] && PACKAGES+=(${EXTRA_PACKAGES})

include="$(IFS=,; echo "${PACKAGES[*]}")"

echo ">> Building $NAME"
mkdir -p /dist
rm -f "$OUT" "$OUT.sha256"

# zstd reads its level from the environment; mmdebstrap picks it from the
# file name. 19 keeps the template small; it is written once and read often.
export ZSTD_CLEVEL=19 ZSTD_NBTHREADS=0

mmdebstrap \
    --mode=root \
    --variant=minbase \
    --architectures="$ARCH" \
    --include="$include" \
    --customize-hook='/src/lxc/customize.sh "$1"' \
    "$SUITE" "$OUT" \
    "deb $MIRROR $SUITE main" \
    "deb $MIRROR $SUITE-updates main" \
    "deb $SECURITY $SUITE-security main"

# Proxmox's own templates carry a checksum beside them; the file name is
# relative so `sha256sum -c` works wherever the pair is copied.
( cd /dist && sha256sum "$NAME.tar.zst" > "$NAME.tar.zst.sha256" )
chown "${HOST_UID:-0}:${HOST_GID:-0}" "$OUT" "$OUT.sha256"

echo
echo ">> $OUT"
echo "   $(du -h "$OUT" | cut -f1), sha256 $(cut -d' ' -f1 "$OUT.sha256")"
