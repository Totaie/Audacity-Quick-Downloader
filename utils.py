"""Small helpers shared by the downloaders."""

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# Extensions the downloaders may end up with before anything is converted.
AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".m4v",
    ".aac",
    ".alac",
    ".flac",
    ".opus",
    ".ogg",
    ".wav",
}


class DownloadError(RuntimeError):
    """Raised when a download cannot be completed."""


class MissingDependency(DownloadError):
    """Raised when an external program the app relies on is not installed."""


class Cancelled(DownloadError):
    """Raised when the user cancels a download part way through."""


# Colour codes that yt-dlp and gamdl sprinkle through their messages.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text):
    return ANSI_ESCAPE.sub("", text)


class Reporter:
    """Where the downloaders send progress instead of printing it.

    This base class ignores everything, so the downloaders can be used on
    their own; the job runner passes a subclass that feeds the interface.
    """

    def __init__(self):
        self.cancel_event = threading.Event()

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def check_cancelled(self):
        if self.cancelled:
            raise Cancelled("Cancelled.")

    def stage(self, text):
        """What is happening right now, e.g. "Converting to MP3"."""

    def item(self, index, total, title, collection=None):
        """Which track is being worked on, for playlists and albums."""

    def progress(self, fraction, speed=None, eta=None):
        """How far through the current track we are (0-1, or None if unknown)."""

    def log(self, text, level="info"):
        """A line for the activity log."""


def find_ffmpeg():
    """Return the path to ffmpeg, or raise if it is not installed."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        if sys.platform == "win32":
            how = (
                "Install it from https://ffmpeg.org and make sure the folder "
                "holding ffmpeg.exe is on your PATH."
            )
        elif sys.platform == "darwin":
            how = "Install it with: brew install ffmpeg"
        else:
            how = "Install it with your package manager (on Arch: sudo pacman -S ffmpeg)."
        raise MissingDependency(f"ffmpeg was not found on your PATH. {how}")
    return ffmpeg


def run_quiet(command):
    """Run a command, hiding its output unless it fails."""
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )


@contextlib.contextmanager
def temp_workspace(parent):
    """Yield a scratch folder inside ``parent`` that is removed afterwards.

    Downloading into a fresh folder means we always know exactly which files
    belong to this run, and the folder is cleaned up even if something fails.
    """
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=".downloading-", dir=str(parent)))
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def unique_path(destination):
    """Return a path that does not exist yet, adding " (1)", " (2)"... if needed."""
    destination = Path(destination)
    if not destination.exists():
        return destination

    stem, suffix, parent = destination.stem, destination.suffix, destination.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_into(source, destination_dir):
    """Move a file into a folder without ever overwriting an existing file."""
    source = Path(source)
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    destination = unique_path(destination_dir / source.name)
    shutil.move(str(source), str(destination))
    return destination


def collect_files(directory, extensions):
    """Return files under ``directory`` with one of ``extensions``, oldest first.

    Sorting by modification time keeps playlists in the order they downloaded,
    which is also the order they get imported into Audacity.
    """
    directory = Path(directory)
    matches = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    ]
    return sorted(matches, key=lambda path: path.stat().st_mtime)


def default_downloads_dir():
    """The Downloads folder that ships with the project, regardless of the cwd."""
    return Path(__file__).resolve().parent / "Downloads"


def module_available(name):
    """True if ``name`` can be imported without actually importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def describe_path(path):
    """Shorten a path for display when it lives under the current directory."""
    path = Path(path)
    try:
        return str(path.relative_to(Path(os.getcwd())))
    except ValueError:
        return str(path)


def python_executable():
    """The interpreter running this script, used to shell out to gamdl and pip."""
    return sys.executable or "python"


def reveal(path):
    """Show a file (selected) or a folder in the system file manager."""
    path = Path(path)
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except OSError:
        return False
    return True
