#!/usr/bin/env bash
# One-command setup: ./setup.sh makes the auto_apply environment (installing Miniforge first if there is no conda),
# then hands over to `python -m app.setup` for the rest. Safe to run again: whatever is done already is left alone.
# Plain bash that also runs on the bash 3.2 Macs ship.
set -eu

MINIFORGE_VERSION=26.9.4-1
MINIFORGE_URL=https://github.com/conda-forge/miniforge/releases/download/$MINIFORGE_VERSION
LINUX_SHA256=571b410272cc434f28bc6c5c49ca8ffd4a4d1bcba9f57f1b6baccdb46446cfcc
MAC_SHA256=63f8f5367cd3e52d1dba3e22d8ad91d10ea0e93675ba5b0822312a4342d962e0
ENV_NAME=auto_apply
MIN_FREE_KB=3145728 # 3 GB: Miniforge, the environment (about 1 GB) and conda's package cache
IMPORT_TEST="import sqlcipher3, app.setup, app.web.main"

stop() {
    printf '%s\n' "$1" >&2
    exit 1
}

ask() { # yes only for y or yes, in any capitals; Ctrl+D is no
    printf '%s [y/N] ' "$1"
    answer=
    read -r answer || echo
    case $answer in
    [yY] | [yY][eE][sS]) return 0 ;;
    esac
    return 1
}

need_space() { # only before something big: the Miniforge download, or building the environment
    # The "Available" column is the one before "Capacity" (the only one ending in %), so spaces in names don't matter.
    free_kb=$(df -Pk "$HOME" | awk 'NR == 2 { for (i = 2; i <= NF; i++) if ($i ~ /%$/) { print $(i - 1); exit } }')
    case $free_kb in
    '' | *[!0-9]*) stop "Could not check the free space in $HOME." ;;
    esac
    [ "$free_kb" -ge "$MIN_FREE_KB" ] ||
        stop "Setup needs at least 3 GB free in $HOME, which has $(awk "BEGIN { printf \"%.1f\", $free_kb / 1048576 }") GB free."
}

usage() {
    echo "Usage: ./setup.sh [--environment-only]" >&2
    exit 2
}

# Ctrl+C anywhere says so, instead of the failure line of a step it cut off (bash runs this once that step ends).
# It leaves through the EXIT trap, so a Miniforge download is still removed.
trap 'echo "Stopped. Nothing is undone; run ./setup.sh again to continue." >&2; exit 130' INT TERM

environment_only=no
case $# in
0) ;;
1) [ "$1" = --environment-only ] || usage; environment_only=yes ;;
*) usage ;;
esac
cd "$(dirname "$0")"

# 1. The system
system=$(uname -s)
machine=$(uname -m)
case $system in
Linux)
    [ "$machine" = x86_64 ] || stop "Only Linux on x86-64 processors is supported; this one is $machine."
    installer=Miniforge3-$MINIFORGE_VERSION-Linux-x86_64.sh
    fingerprint=$LINUX_SHA256
    download_size="130 MB"
    ;;
Darwin)
    version=$(sw_vers -productVersion)
    [ "${version%%.*}" -ge 11 ] || stop "This needs macOS 11 or newer; this Mac has macOS $version."
    if [ "$machine" = x86_64 ]; then
        if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
            stop "This Terminal runs under Rosetta: open Terminal without Rosetta (Get Info → untick Open using Rosetta) and run ./setup.sh again."
        fi
        stop "Intel Macs are not supported for now: one package has no Intel build at the pinned version."
    fi
    [ "$machine" = arm64 ] || stop "This Mac's processor ($machine) is not supported."
    echo "macOS is untested so far: please tell whoever gave you this how it went."
    installer=Miniforge3-$MINIFORGE_VERSION-MacOSX-arm64.sh
    fingerprint=$MAC_SHA256
    download_size="90 MB"
    ;;
*) stop "This system ($system) is not supported: setup runs on Linux (x86-64) and Apple-silicon Macs." ;;
esac

# 2. conda: an existing one is used as it is
conda=
if [ -n "${CONDA_EXE:-}" ] && [ -f "$CONDA_EXE" ] && [ -x "$CONDA_EXE" ]; then
    conda=$CONDA_EXE
else
    found=$(command -v conda || true)
    case $found in
    /*) conda=$found ;; # a program on the PATH; a shell function or alias gives a bare name instead
    esac
fi
for candidate in "$HOME/miniforge3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda"; do
    if [ -z "$conda" ] && [ -f "$candidate" ] && [ -x "$candidate" ]; then
        conda=$candidate
    fi
done

if [ -z "$conda" ]; then
    [ ! -e "$HOME/miniforge3" ] ||
        stop "$HOME/miniforge3 exists but has no conda in it (perhaps a cut-off install): move or remove that folder, then run ./setup.sh again."
    ask "Install Miniforge $MINIFORGE_VERSION (about $download_size download) in ~/miniforge3? It changes no shell settings." ||
        stop "Nothing installed. To install conda yourself, follow https://github.com/conda-forge/miniforge, then run ./setup.sh again."
    command -v curl >/dev/null || stop "curl is needed to download Miniforge: install curl, then run ./setup.sh again."
    need_space
    workdir=$(mktemp -d)
    trap 'rm -rf "$workdir"' EXIT
    curl -fL --proto '=https' --tlsv1.2 -o "$workdir/$installer" "$MINIFORGE_URL/$installer" ||
        stop "Downloading Miniforge failed: check the internet connection, then run ./setup.sh again."
    if command -v sha256sum >/dev/null; then
        actual=$(sha256sum "$workdir/$installer" | awk '{ print $1 }')
    else
        actual=$(shasum -a 256 "$workdir/$installer" | awk '{ print $1 }')
    fi
    if [ "$actual" != "$fingerprint" ]; then
        rm -f "$workdir/$installer"
        stop "The download's fingerprint is wrong; nothing was installed. Run ./setup.sh again later."
    fi
    bash "$workdir/$installer" -b -p "$HOME/miniforge3" || # -b: no questions, no shell changes
        stop "Installing Miniforge failed: remove the folder $HOME/miniforge3, then run ./setup.sh again."
    rm -rf "$workdir"
    conda=$HOME/miniforge3/bin/conda
fi

# 3. The environment, by name, so whichever one is active doesn't matter
works() { "$conda" run -n "$ENV_NAME" python -c "$IMPORT_TEST" >/dev/null 2>&1; }

if works; then
    echo "Environment $ENV_NAME: ready (left as it is)."
else
    need_space
    if "$conda" env list | awk -v name="$ENV_NAME" '$1 == name { found = 1 } END { exit !found }'; then
        ask "Environment $ENV_NAME is there but can't load the app (broken or half-built). Rebuild it? Your data folder is not touched." ||
            stop "Environment $ENV_NAME can't load the app. Run ./setup.sh again and answer yes to rebuild it."
        "$conda" env remove -y -n "$ENV_NAME" || stop "Removing the broken environment $ENV_NAME failed."
    fi
    failed="Building the environment failed: check the free space in $HOME and the internet connection, then run ./setup.sh again (it continues)."
    if [ "$system" = Linux ]; then
        # only conda-forge, as the lock file's addresses: Anaconda's channels (and their terms) are never consulted
        "$conda" create -y -n "$ENV_NAME" --override-channels -c conda-forge --file conda-linux-64.lock || stop "$failed"
        "$conda" run --no-capture-output -n "$ENV_NAME" pip install --require-hashes --no-deps -r requirements-pip.txt || stop "$failed"
    else
        echo "On a Mac the environment comes from environment.yml: its versions are pinned, but not exact like Linux's."
        "$conda" env create -f environment.yml || stop "$failed"
    fi
    works || stop "Environment $ENV_NAME was built but can't load the app. Run ./setup.sh again to rebuild it."
    echo "Environment $ENV_NAME: ready."
fi

# 4. Hand-over: the environment's own Python, with no activation
if [ "$environment_only" = yes ]; then
    echo "Environment ready; --environment-only, so the app's own setup was not run."
    exit 0
fi
python=$("$conda" run -n "$ENV_NAME" python -c "import sys; print(sys.executable)")
[ -x "$python" ] || stop "Could not find the Python of environment $ENV_NAME."
exec "$python" -m app.setup
