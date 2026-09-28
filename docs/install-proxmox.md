# Running in Proxmox (LXC template)

The repository builds a Proxmox container template: Debian 13, amd64, with
HAProxy Cluster Manager installed by `install.sh` exactly as on a server. Every
container made from it is an ordinary systemd node, so the watchdog, the logs
and one-click updates all work as described in the rest of the documentation.

## Building

You need Docker with Compose. The build runs the same way on a Mac and on a
Linux host; on Apple Silicon the amd64 builder runs under emulation, which is
slower (a few minutes) but produces the same template.

```bash
make build
```

The output is two files in `dist/`:

```
debian-13-haproxy-manager_<VERSION>_amd64.tar.zst          the template
debian-13-haproxy-manager_<VERSION>_amd64.tar.zst.sha256   its SHA-256
```

`<VERSION>` is the contents of `VERSION`, so the template's name matches the
version the app reports.

To add packages, list them when you build:

```bash
make build EXTRA_PACKAGES="vim less"
```

### Checking a build

```bash
make check
```

This verifies the checksum, confirms the template carries nothing that must be
unique to a node (machine-id, SSH host keys, the manager's login), then runs it
and checks that first boot seeds a login, the UI answers, sign-in works and the
tools are present.

On an amd64 Linux host the check boots the template with systemd as PID 1, the
way Proxmox does. On an Apple Silicon Mac it cannot: under emulation systemd
fails to start any service, so the check runs each part directly and says the
boot itself was not tested. Creating a container in Proxmox covers that.

`KEEP=1 make check` leaves the test container running so you can look around.

## Uploading to Proxmox

Copy both files to the host and verify the checksum there:

```bash
scp dist/debian-13-haproxy-manager_*_amd64.tar.zst* root@pve:/var/lib/vz/template/cache/
ssh root@pve 'cd /var/lib/vz/template/cache && sha256sum -c debian-13-haproxy-manager_*.sha256'
```

Or upload the `.tar.zst` under **Datacenter > (node) > local > CT Templates >
Upload** and compare the hash with the `.sha256` file.

## Creating a container

Create it **unprivileged**. Everything the manager does works there, and root
inside the container is not root on the host.

```bash
pct create 120 local:vztmpl/debian-13-haproxy-manager_1.95.0_amd64.tar.zst \
  --hostname proxy1 \
  --unprivileged 1 --features nesting=1 \
  --cores 2 --memory 1024 --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.0.2.11/24,gw=192.0.2.1 \
  --ssh-public-keys ~/.ssh/authorized_keys \
  --password \
  --start 1
```

`nesting=1` is what the Proxmox UI sets by default for unprivileged containers.
The systemd in Debian 13 needs it for the sandboxing some services use.

When Proxmox creates the container it writes the hostname, the network, the
root password, your SSH keys and fresh SSH host keys. On first boot the
container then generates its own manager login and API key.

### Signing in

The UI is at `http://<container address>:8080`. The login is in the container:

```bash
pct exec 120 -- cat /var/lib/haproxy-manager/admin-credentials.txt
```

Change it from the gear beside your name at the foot of the menu, and put the
UI behind TLS or restrict who can reach port 8080.

### SSH

SSH follows Debian's default: root can sign in with a key, not a password. The
password from `--password` works on the Proxmox console (`pct enter 120`).

## Several IP addresses

A container has its own network stack, so it can hold as many addresses as you
give it, and an unprivileged container is allowed to change its own addresses.

- **Addresses the container always holds** go on `net0` (and further `netN`
  interfaces) in Proxmox, or are added by the manager.
- **The shared virtual IP** is added and removed by Keepalived as the node
  becomes active or passive. VRRP and the gratuitous ARP that announces a
  failover both work unprivileged.
- **Binding an address the node does not hold yet**, which HAProxy on the
  passive node must do for the VIP, needs `net.ipv4.ip_nonlocal_bind`. In an
  LXC container `systemd-sysctl` skips itself, so the template applies it with
  its own `ham-sysctl.service` on every boot.

If the Proxmox firewall is on for the container, leave **IP filter** off on its
interface, or list every address there. With it on, traffic from any address
Proxmox was not told about, including the VIP, is dropped.

## What is in the template

| | |
| --- | --- |
| Base | Debian 13 minimal, systemd, ifupdown (Proxmox writes `/etc/network/interfaces`) |
| Access | `openssh-server`, `login` |
| Tools | `curl`, `traceroute`, `mtr-tiny`, `htop`, `tcpdump`, `iputils-ping`, `iproute2` |
| Manager | HAProxy, Keepalived, `acme.sh`, and the app in `/opt/haproxy-manager` |

Two units are the template's own:

- **`ham-firstboot.service`** runs before SSH and the manager on every boot. It
  creates any missing SSH host keys and, if the node has no login yet, seeds the
  administrator and API key with `install.sh --first-boot`. Once those exist it
  changes nothing.
- **`ham-sysctl.service`** applies the non-local bind setting described above.

## Updating

A container is installer-managed, so **Settings > Updates** updates it in place
from `invite-networks/haproxy-manager`, the same as a server. Build a new
template only for new containers.
