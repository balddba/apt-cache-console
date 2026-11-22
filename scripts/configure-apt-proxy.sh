#!/bin/sh
# Configure package caching on Ubuntu and Ubuntu-based client machines.
set -eu

if [ ! -r /etc/os-release ]; then
    printf '%s\n' 'Cannot detect the operating system: /etc/os-release is missing.' >&2
    exit 1
fi
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *' ubuntu '*) ;;
    *)
        printf '%s\n' 'This setup script requires Ubuntu or an Ubuntu-based distribution.' >&2
        exit 1
        ;;
esac

# Run system configuration commands as root, using sudo when needed.
run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

printf '%s\n' 'Acquire::http::Proxy "http://vmlxutil01.aaronslab.net:8000/";' |
    run_as_root tee /etc/apt/apt.conf.d/01squid-deb-proxy > /dev/null
# Refresh Docker's signing key only on clients with its Ubuntu repository.
if grep -Rqs 'https://download.docker.com/linux/ubuntu' /etc/apt/sources.list /etc/apt/sources.list.d; then
    run_as_root install -m 0755 -d /etc/apt/keyrings
    run_as_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    run_as_root chmod a+r /etc/apt/keyrings/docker.asc
fi
run_as_root apt-get update
