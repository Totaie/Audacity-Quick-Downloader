"""Diagnose importing into Audacity.

Run it with the app's virtual environment, next to main.py:

    .venv/bin/python diagnose.py         (Linux / macOS)
    .venv/Scripts/python diagnose.py     (Windows)

It exercises the same code the app uses to find, start and talk to
Audacity, and says exactly where the chain breaks: the settings, the
executable, the scripting module, the process, and a live connection
test with a real command.
"""

import os
import sys

import audacity
from settings import load


def main():
    settings, _ = load()
    print("Settings")
    print(f"  import_to_audacity:  {settings.import_to_audacity}")
    print(f"  launch_audacity:     {settings.launch_audacity}")
    print()

    executable = audacity.find_audacity()
    sandboxed = audacity.sandboxed_audacity()
    print("Finding Audacity")
    print(f"  find_audacity():      {executable}")
    if sandboxed:
        print(f"  WARNING: a {sandboxed} Audacity is installed, but sandboxed")
        print("  builds cannot be scripted. Install the distribution's Audacity")
        print("  (Arch: sudo pacman -S audacity) or set AUDACITY_PATH.")
    print(f"  script_pipe_enabled(): {audacity.script_pipe_enabled()}")
    if sys.platform != "win32":
        fifos = sorted(p for p in os.listdir("/tmp") if p.startswith("audacity_script_pipe"))
        print(f"  script pipes in /tmp: {fifos or 'none'}")
    print()

    if executable is None:
        print("STOP: Audacity is not installed where the app looks.")
        print("  Install the distribution's Audacity (Arch: sudo pacman -S audacity),")
        print("  or set AUDACITY_PATH to the full path of the Audacity executable.")
        return 1

    running = audacity.is_running()
    print(f"Running: {running}")
    if not running:
        print("Starting Audacity ...")
        try:
            audacity.ensure_running(auto_launch=True, log=print)
        except audacity.AudacityError as error:
            print(f"  Could not start it: {error}")
            return 1
        print(f"Running now: {audacity.is_running()}")
    print()

    print("Opening the scripting connection (waits up to 90 s) ...")
    try:
        with audacity.connect(timeout=90) as session:
            reply = session.command("GetVersion")
            print(f"CONNECTION OK - Audacity replied: {reply.strip()}")
            if settings.import_to_audacity:
                print("The import chain is healthy. If a download is still not")
                print("imported, look at the download's own message in the app.")
            else:
                print("The connection works, but import_to_audacity is OFF.")
                print("Turn it on in Settings (Ctrl+S), Audacity > Import.")
            return 0
    except audacity.AudacityError as error:
        print(f"CONNECTION FAILED: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
