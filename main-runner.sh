#!/usr/bin/env bash
# Run the downloader on Linux (made for Arch, works on any distribution).
# The same job as main-runner.bat on Windows: build the virtual environment
# on first run, reinstall only when requirements.txt changes, then start the
# app. Links, song names, files or folders given as arguments are queued.
#
#   ./main-runner.sh
#   ./main-runner.sh "https://soundcloud.com/artist/track"
#
# Started from a file manager or app launcher (no terminal attached), it
# reopens itself in a terminal window, since the interface needs one.

set -u

# Resolve symlinks, so a link to this file placed anywhere still works.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    TARGET="$(readlink "$SOURCE")"
    case "$TARGET" in
        /*) SOURCE="$TARGET" ;;
        *) SOURCE="$(dirname "$SOURCE")/$TARGET" ;;
    esac
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "$SOURCE")"

VENV_DIR="$SCRIPT_DIR/.venv"
INSTALLED="$VENV_DIR/requirements.installed"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

say() { printf '[setup] %s\n' "$*"; }

# Keep the window open after a failure, so the message can be read.
finish() {
    local code="$1"
    if [ "$code" -ne 0 ] && [ -t 0 ]; then
        echo
        read -r -p "Press Enter to close." _
    fi
    exit "$code"
}

# --- No terminal? Reopen in one ---------------------------------------------
if [ ! -t 0 ] && [ ! -t 1 ] && [ -z "${AQD_IN_TERMINAL:-}" ]; then
    export AQD_IN_TERMINAL=1
    for terminal in "${TERMINAL:-}" kitty alacritty foot wezterm konsole gnome-terminal xfce4-terminal xterm; do
        [ -n "$terminal" ] && command -v "$terminal" >/dev/null 2>&1 || continue
        case "$terminal" in
            gnome-terminal) exec "$terminal" -- "$SELF" "$@" ;;
            wezterm) exec "$terminal" start -- "$SELF" "$@" ;;
            *) exec "$terminal" -e "$SELF" "$@" ;;
        esac
    done
    # No terminal emulator found: carry on, and get the plain output.
fi

# The venv's interpreter: bin/python on Linux (Scripts/python.exe if this
# is run from Git Bash on Windows).
venv_python() {
    if [ -x "$VENV_DIR/bin/python" ]; then
        echo "$VENV_DIR/bin/python"
    elif [ -x "$VENV_DIR/Scripts/python.exe" ]; then
        echo "$VENV_DIR/Scripts/python.exe"
    fi
}

# --- First run: build the virtual environment --------------------------------
FRESH=""
PYTHON="$(venv_python)"
if [ -z "$PYTHON" ]; then
    BOOTSTRAP=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            BOOTSTRAP="$candidate"
            break
        fi
    done
    if [ -z "$BOOTSTRAP" ]; then
        say "Python 3.10 or newer was not found."
        say "On Arch: sudo pacman -S python"
        finish 1
    fi

    say "Creating virtual environment in \"$VENV_DIR\" ..."
    if ! "$BOOTSTRAP" -m venv "$VENV_DIR"; then
        say "Could not create the virtual environment in \"$VENV_DIR\"."
        say "Delete that folder if it exists and try again."
        finish 1
    fi
    PYTHON="$(venv_python)"
    if [ -z "$PYTHON" ]; then
        say "The virtual environment was created without a Python in it."
        finish 1
    fi
    FRESH=1
fi

# --- Install again only when requirements.txt has changed ---------------------
# Keeping yt-dlp and gamdl current happens inside the app, in the background,
# so it never holds up start up.
if [ -n "$FRESH" ] || ! cmp -s "$REQUIREMENTS" "$INSTALLED"; then
    if [ -n "$FRESH" ]; then
        say "Installing dependencies, this takes a minute ..."
    else
        say "requirements.txt changed, installing the new dependencies ..."
    fi
    if ! "$PYTHON" -m pip install --upgrade --disable-pip-version-check --quiet -r "$REQUIREMENTS"; then
        say "Installing the dependencies failed. Check your internet"
        say "connection and run this file again."
        finish 1
    fi
    cp "$REQUIREMENTS" "$INSTALLED"
fi

# ffmpeg is a system package rather than a Python one; warn early.
if ! command -v ffmpeg >/dev/null 2>&1; then
    say "ffmpeg was not found. Downloads need it to convert audio."
    say "On Arch: sudo pacman -S ffmpeg"
fi

# --- Run it ------------------------------------------------------------------
cd "$SCRIPT_DIR" || finish 1
"$PYTHON" main.py "$@"
finish $?
