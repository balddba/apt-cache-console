#!/bin/sh
# Prepare the Ubuntu host's Docker signing key, then start the proxy stack.
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

if [ ! -r /etc/os-release ]; then
    printf '%s\n' 'Cannot detect the operating system: /etc/os-release is missing.' >&2
    exit 1
fi
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *' ubuntu '*) ;;
    *)
        printf '%s\n' 'Server setup requires Ubuntu or an Ubuntu-based distribution.' >&2
        exit 1
        ;;
esac

if [ ! -f .env ]; then
    printf '%s\n' 'Copy .env.example to .env and configure the passwords before server setup.' >&2
    exit 1
fi
for command in curl docker; do
    if ! command -v "$command" > /dev/null 2>&1; then
        printf '%s\n' "Server setup requires $command to be installed first." >&2
        exit 1
    fi
done

# Run host configuration and Docker commands as root, using sudo when needed.
run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

run_as_root docker compose version > /dev/null

# Download first so a failed request cannot truncate an existing keyring.
key_file=$(mktemp)
trap 'rm -f "$key_file"' EXIT
trap 'exit 1' HUP INT TERM
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$key_file"
if [ ! -s "$key_file" ]; then
    printf '%s\n' 'Docker signing key download was empty; server setup stopped.' >&2
    exit 1
fi
run_as_root install -m 0755 -d /etc/apt/keyrings
run_as_root install -m 0644 "$key_file" /etc/apt/keyrings/docker.asc
run_as_root apt-get update
run_as_root docker compose up -d --build
