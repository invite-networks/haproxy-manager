#!/usr/bin/env bash
#
# mmdebstrap customize hook: turns a bare Debian 13 tree into the template.
# $1 is the root of the tree being built. Called by lxc/build.sh.

set -euo pipefail

ROOT="$1"

# -- the application, installed by the same script a server uses -----------
# From a copy of this checkout, so the template holds exactly what is here.
install -d "$ROOT/tmp/ham-src"
tar -C /src --exclude=./.git --exclude=./dist -cf - . | tar -C "$ROOT/tmp/ham-src" -xf -
chroot "$ROOT" env HAM_IMAGE=1 DEBIAN_FRONTEND=noninteractive \
    bash /tmp/ham-src/install.sh --yes
rm -rf "$ROOT/tmp/ham-src"

# -- the template's own units and scripts -----------------------------------
cp -R /src/lxc/rootfs/. "$ROOT/"
chmod 0755 "$ROOT/usr/local/sbin/ham-firstboot"
chroot "$ROOT" systemctl enable ham-firstboot.service ham-sysctl.service >/dev/null

# -- apt sources in the Debian 13 format ------------------------------------
rm -f "$ROOT/etc/apt/sources.list"
cat > "$ROOT/etc/apt/sources.list.d/debian.sources" <<'EOF'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF

# -- nothing that must be unique to a node stays in the template ------------
# Proxmox writes host keys, the root password, SSH keys, the hostname and the
# network when a container is created; ham-firstboot covers anything missing.
rm -f "$ROOT"/etc/ssh/ssh_host_*
# mmdebstrap copies these from the builder container; Proxmox writes its own.
echo haproxy-manager > "$ROOT/etc/hostname"
: > "$ROOT/etc/resolv.conf"
rm -f "$ROOT/var/lib/dbus/machine-id"
: > "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/haproxy-manager/"{config.json,admin-credentials.txt,api-key.txt}

# -- smaller -----------------------------------------------------------------
chroot "$ROOT" apt-get clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/debconf/*-old \
       "$ROOT"/var/log/*.log "$ROOT"/var/log/apt/* "$ROOT/root/.bash_history"
find "$ROOT/opt/haproxy-manager" -name __pycache__ -type d -prune -exec rm -rf {} +
