"""Start Audacity and talk to it over its mod-script-pipe named pipes.

When the mod-script-pipe module is enabled, Audacity exposes two named pipes
that accept scripting commands. This module finds the Audacity executable,
starts it if it is not already running, waits for those pipes to show up and
then sends commands over them with sensible timeouts so a stuck Audacity can
never hang the downloader forever.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# The pipes are used in binary mode on purpose. Opening them as text files
# turns every "\n" we write into "\r\n", which mangles the "\r\n\0" terminator
# Audacity expects: it then swallows the command and never answers.
if sys.platform == "win32":
    TO_PIPE = r"\\.\pipe\ToSrvPipe"
    FROM_PIPE = r"\\.\pipe\FromSrvPipe"
    EOL = b"\r\n\0"
else:
    TO_PIPE = f"/tmp/audacity_script_pipe.to.{os.getuid()}"
    FROM_PIPE = f"/tmp/audacity_script_pipe.from.{os.getuid()}"
    EOL = b"\n"

# Audacity can take a while to start when it rescans plug-ins.
STARTUP_TIMEOUT = 90
# Importing is quick, but leave headroom for very long recordings.
COMMAND_TIMEOUT = 60

ENABLE_INSTRUCTIONS = (
    "In Audacity, open Edit > Preferences > Modules, set mod-script-pipe to "
    "Enabled, then restart Audacity."
)


class AudacityError(RuntimeError):
    """Raised when Audacity cannot be reached or refuses a command."""


# --------------------------------------------------------------------------
# Finding, starting and inspecting Audacity
# --------------------------------------------------------------------------


def find_audacity():
    """Return the path to the Audacity executable, or None if we cannot find it."""
    override = os.environ.get("AUDACITY_PATH")
    if override and Path(override).exists():
        return Path(override)

    candidates = []
    if sys.platform == "win32":
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Audacity" / "Audacity.exe")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "Audacity" / "Audacity.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Audacity.app/Contents/MacOS/Audacity"))
    else:
        candidates.append(Path("/usr/bin/audacity"))
        candidates.append(Path("/usr/local/bin/audacity"))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    on_path = shutil.which("audacity")
    return Path(on_path) if on_path else None


def is_running():
    """True when an Audacity process is running, whether or not scripting is on.

    Note that this deliberately looks at the process list rather than at the
    pipes. On Windows, os.path.exists() on a named pipe opens a handle on it,
    and mod-script-pipe hands out a single pipe instance at a time: polling the
    pipes to see whether Audacity is ready makes it drop them altogether. The
    only safe way to test the pipes is to open them for real, which connect()
    does below.
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq audacity.exe", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
            )
            return "audacity.exe" in result.stdout.lower()
        return (
            subprocess.run(
                ["pgrep", "-x", "audacity"], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        # If we cannot tell, assume it is not running: the worst case is that
        # Audacity gets asked to start when it already is, which is harmless.
        return False


def launch(executable=None):
    """Start Audacity detached from this console and return the executable used."""
    executable = Path(executable) if executable else find_audacity()
    if executable is None:
        raise AudacityError(
            "Could not find Audacity. Install it from https://audacityteam.org, "
            "or set the AUDACITY_PATH environment variable to the full path of "
            "the Audacity executable."
        )

    options = {}
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        options["start_new_session"] = True

    try:
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **options,
        )
    except OSError as error:
        raise AudacityError(f"Could not start Audacity ({executable}): {error}")

    return executable


def config_path():
    """Where Audacity keeps audacity.cfg on this platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "audacity" / "audacity.cfg" if appdata else None
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "audacity"
            / "audacity.cfg"
        )
    return Path.home() / ".audacity-data" / "audacity.cfg"


def _read_config():
    """Return (lines, line ending) for audacity.cfg, or (None, None)."""
    path = config_path()
    if not path or not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    return text.splitlines(), "\r\n" if "\r\n" in text else "\n"


def script_pipe_enabled():
    """True/False if we can read Audacity's settings, None if we cannot tell.

    audacity.cfg starts with keys that sit outside any section, so it is parsed
    by hand rather than with configparser.
    """
    lines, _ = _read_config()
    if lines is None:
        return None

    section = None
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
        elif section == "module" and line.lower().startswith("mod-script-pipe="):
            return line.split("=", 1)[1].strip() == "1"
    return None


def enable_script_pipe():
    """Turn mod-script-pipe on in audacity.cfg. Audacity must not be running.

    Audacity rewrites this file when it exits, so changing it underneath a
    running instance would simply be undone.
    """
    path = config_path()
    lines, newline = _read_config()
    if path is None or lines is None:
        return False

    section = None
    module_section_at = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].lower()
            if section == "module":
                module_section_at = index
        elif section == "module" and stripped.lower().startswith("mod-script-pipe="):
            lines[index] = "mod-script-pipe=1"
            break
    else:
        if module_section_at is None:
            lines.extend(["[Module]", "mod-script-pipe=1"])
        else:
            lines.insert(module_section_at + 1, "mod-script-pipe=1")

    try:
        path.write_text(newline.join(lines) + newline, encoding="utf-8")
    except OSError:
        return False
    return True


def ensure_running(auto_launch=True, ask=None, log=print):
    """Make sure Audacity is up, starting it if needed.

    ``ask`` is an optional callable used to ask permission before changing
    Audacity's own settings. It is only consulted when mod-script-pipe is
    turned off and Audacity is not running. ``log`` receives status messages.
    """
    if is_running():
        return True

    if not auto_launch:
        log("Audacity is not running and auto-start is disabled.")
        return False

    if script_pipe_enabled() is False:
        question = (
            "Audacity's scripting module (mod-script-pipe) is turned off, so "
            "tracks cannot be imported. Turn it on for you?"
        )
        if ask and ask(question) and enable_script_pipe():
            log("Enabled mod-script-pipe in Audacity's settings.")
        else:
            log(ENABLE_INSTRUCTIONS)

    executable = launch()
    log(f"Started Audacity ({executable}).")
    return True


def connection_hint():
    """A message explaining why the pipes are missing, as best we can tell."""
    enabled = script_pipe_enabled()
    if enabled is False:
        return (
            "Audacity is running without the scripting module. " + ENABLE_INSTRUCTIONS
        )
    if is_running():
        return (
            "Audacity is running but is not answering on its scripting pipes. "
            "Close any other script that is using them, restart Audacity, and "
            f"check that mod-script-pipe is enabled. {ENABLE_INSTRUCTIONS}"
        )
    return f"Audacity does not appear to be running. {ENABLE_INSTRUCTIONS}"


# --------------------------------------------------------------------------
# The scripting connection itself
# --------------------------------------------------------------------------


class Audacity:
    """A live connection to Audacity's scripting pipes."""

    def __init__(self, command_timeout=COMMAND_TIMEOUT, on_wait=None):
        self.command_timeout = command_timeout
        self.on_wait = on_wait
        self._to_file = None
        self._from_file = None
        self._responses = queue.Queue()
        self._reader = None

    # -- connection handling ------------------------------------------------

    def open(self, timeout=STARTUP_TIMEOUT):
        """Open both pipes, retrying until Audacity has finished starting up."""
        deadline = time.monotonic() + timeout
        waiting = False

        while True:
            try:
                self._to_file = open(TO_PIPE, "wb", buffering=0)
                self._from_file = open(FROM_PIPE, "rb", buffering=0)
                break
            except OSError as error:
                self.close()
                if time.monotonic() >= deadline:
                    raise AudacityError(f"{connection_hint()} ({error.strerror})")
                if not waiting and self.on_wait:
                    self.on_wait()
                waiting = True
                time.sleep(1.5)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self

    def _read_loop(self):
        """Collect responses in the background so reads can never block forever.

        Audacity terminates each response with a blank line.
        """
        buffer = []
        try:
            while True:
                line = self._from_file.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if text.strip():
                    buffer.append(text)
                elif buffer:
                    self._responses.put("".join(buffer))
                    buffer = []
        except (OSError, ValueError):
            pass
        finally:
            # None means "the pipe closed"; it unblocks anyone waiting.
            self._responses.put(None)

    def close(self):
        for handle in (self._to_file, self._from_file):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._to_file = None
        self._from_file = None

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
        return False

    # -- commands -----------------------------------------------------------

    def command(self, text):
        """Send one scripting command and return Audacity's reply."""
        if self._to_file is None:
            raise AudacityError("Not connected to Audacity.")

        try:
            self._to_file.write(text.encode("utf-8") + EOL)
            self._to_file.flush()
        except OSError as error:
            raise AudacityError(f"Lost the connection to Audacity: {error}")

        try:
            response = self._responses.get(timeout=self.command_timeout)
        except queue.Empty:
            raise AudacityError(
                f"Audacity did not reply within {self.command_timeout} seconds. "
                "Check whether it is waiting on a dialog: an open Automatic "
                "Crash Recovery window blocks importing until you answer it."
            )

        if response is None:
            raise AudacityError(
                "Audacity closed the scripting connection. Was it quit mid-import?"
            )
        return response

    def import_file(self, path):
        """Import an audio file as a new track."""
        path = Path(path).resolve()
        if not path.exists():
            raise AudacityError(f"{path} no longer exists.")

        response = self.command(f'Import2: Filename="{path}"')
        if "failed" in response.lower():
            raise AudacityError(f"Audacity could not import {path.name}: {response.strip()}")
        return response


def connect(timeout=STARTUP_TIMEOUT, command_timeout=COMMAND_TIMEOUT, on_wait=None):
    """Return a connection to Audacity, waiting for it to start if necessary.

    ``on_wait`` is called once if Audacity is not ready straight away.
    """
    return Audacity(command_timeout, on_wait).open(timeout)
