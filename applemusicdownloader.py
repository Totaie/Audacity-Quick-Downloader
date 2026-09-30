"""Download Apple Music tracks with gamdl and convert them to the chosen format.

gamdl runs as a separate process with its output captured: the lines saying
which track it is on and how far through it is are turned into progress for
the interface, and everything else stays out of sight unless it goes wrong.
"""

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from formats import AUDIO_FORMATS, convert_audio
from utils import (
    AUDIO_EXTENSIONS,
    Cancelled,
    DownloadError,
    MissingDependency,
    Reporter,
    collect_files,
    default_downloads_dir,
    find_ffmpeg,
    module_available,
    move_into,
    python_executable,
    strip_ansi,
    temp_workspace,
)

DEFAULT_COOKIES = Path(__file__).resolve().parent / "cookies.txt"

# gamdl names files "01 Title" by default. Asking for plain titles up front is
# tidier than trying to strip leading track numbers back off afterwards.
FILE_TEMPLATES = [
    "--single-disc-file-template",
    "{title}",
    "--multi-disc-file-template",
    "{title}",
    "--no-album-file-template",
    "{title}",
]

# [INFO     12:00:00] [Track   3/12 ] Downloading "Song name"
TRACK_LINE = re.compile(r'\[Track\s+(\d+)\s*/\s*(\d+|-)\s*\]\s+Downloading "(.*)"')
# [download]  45.3% of 5.00MiB at 1.20MiB/s ETA 00:03, from yt-dlp inside gamdl
PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
LEVEL = re.compile(r"^\[(WARNING|ERROR|CRITICAL)\b")


def _gamdl_command(url, destination, cookies):
    """Build the gamdl call, running it through this interpreter.

    Using "-m gamdl" rather than the gamdl executable means it works even when
    the virtual environment has not been activated.
    """
    if not module_available("gamdl"):
        raise MissingDependency(
            "gamdl is not installed. Run: pip install -r requirements.txt"
        )

    return [
        python_executable(),
        "-m",
        "gamdl",
        "--cookies-path",
        str(cookies),
        "--output-path",
        str(destination),
        "--no-synced-lyrics",
        *FILE_TEMPLATES,
        url,
    ]


class _GamdlOutput:
    """Turns gamdl's console output into reporter calls."""

    def __init__(self, reporter):
        self.reporter = reporter
        self.tail = []  # the last few lines, to explain a failure

    def feed(self, line):
        line = strip_ansi(line).strip()
        if not line:
            return
        self.tail = (self.tail + [line])[-15:]

        track = TRACK_LINE.search(line)
        if track:
            index, total, title = track.groups()
            self.reporter.item(int(index), int(total) if total.isdigit() else None, title)
            self.reporter.stage("Downloading")
            self.reporter.progress(0.0)
            return

        if line.startswith("[download]"):
            percent = PERCENT.search(line)
            if percent:
                self.reporter.progress(min(float(percent.group(1)) / 100, 1.0))
            return

        level = LEVEL.match(line)
        if level:
            message = line.split("]", 2)[-1].strip() if "]" in line else line
            self.reporter.log(message, "warning" if level.group(1) == "WARNING" else "error")

    def explain(self):
        errors = [line for line in self.tail if LEVEL.match(line)]
        return (errors or self.tail or ["(no output)"])[-1]


def _run_gamdl(command, reporter):
    """Run gamdl, feeding its output to ``reporter``. Returns (exit code, parser)."""
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            **options,
        )
    except OSError as error:
        raise DownloadError(f"Could not run gamdl: {error}")

    output = _GamdlOutput(reporter)

    def pump():
        # yt-dlp redraws its progress line with carriage returns, so split
        # on those as well as on newlines.
        pending = b""
        while True:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            pending += chunk
            *lines, pending = re.split(rb"[\r\n]", pending)
            for line in lines:
                output.feed(line.decode("utf-8", errors="replace"))
        output.feed(pending.decode("utf-8", errors="replace"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            if reporter.cancel_event.wait(0.2):
                process.kill()
                process.wait()
                raise Cancelled("Cancelled.")
    finally:
        reader.join(timeout=5)
    return process.returncode, output


def download_apple_music(
    url, output_dir=None, cookies=None, reporter=None, audio_format="mp3", bitrate="192"
):
    """Download ``url`` from Apple Music and return the audio files saved on disk.

    gamdl saves AAC in .m4a files, which are kept as they are when M4A is the
    chosen format and converted otherwise.
    """
    reporter = reporter or Reporter()
    output_dir = Path(output_dir) if output_dir else default_downloads_dir()
    cookies = Path(cookies) if cookies else DEFAULT_COOKIES

    if not cookies.exists():
        raise MissingDependency(
            f"No Apple Music cookies file at {cookies}. Export your cookies in "
            "Netscape format while signed in to music.apple.com and save them "
            "there (see the README)."
        )

    find_ffmpeg()  # fail early rather than after a long download

    with temp_workspace(output_dir) as workspace:
        command = _gamdl_command(url, workspace, cookies)
        reporter.stage("Looking it up")
        returncode, output = _run_gamdl(command, reporter)

        downloaded = collect_files(workspace, AUDIO_EXTENSIONS)
        if not downloaded:
            raise DownloadError(
                f"gamdl did not download anything: {output.explain()} Common "
                "causes: expired cookies.txt, a track your subscription cannot "
                "play, or a gamdl that has fallen behind Apple's website."
            )
        if returncode != 0:
            reporter.log(
                f"gamdl reported an error (exit code {returncode}); importing "
                "the tracks it did manage to download.",
                "warning",
            )

        tracks = []
        for number, source in enumerate(downloaded, 1):
            reporter.check_cancelled()
            if source.suffix.lower() == AUDIO_FORMATS[audio_format]["extension"]:
                tracks.append(move_into(source, output_dir))
                continue
            reporter.item(number, len(downloaded), source.stem)
            reporter.stage(f"Converting to {AUDIO_FORMATS[audio_format]['label'].split()[0]}")
            reporter.progress(None)
            converted = convert_audio(source, audio_format, bitrate)
            tracks.append(move_into(converted, output_dir))

        return tracks
