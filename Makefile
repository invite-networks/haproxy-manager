# Proxmox LXC template for HAProxy Cluster Manager. Needs Docker with Compose;
# works the same on a Mac (amd64 under emulation) and on a Linux host.
#
#   make build                          build the template and its sha256
#   make build EXTRA_PACKAGES="vim"     ... with more packages in it
#   make check                          boot the built template and test it
#   make clean                          remove dist/

COMPOSE := docker compose -f lxc/compose.yaml
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)
export EXTRA_PACKAGES

.PHONY: build check clean

build:
	mkdir -p dist
	$(COMPOSE) run --rm --build builder

check:
	bash lxc/check.sh

clean:
	rm -rf dist
