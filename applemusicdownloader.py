"""Download Apple Music tracks with gamdl and convert them to the chosen format.

gamdl runs as a separate process with its output captured. It goes through
gamdl_worker.py, which runs gamdl's ``rip`` command and prints a JSON line for
each track it starts, how far through it is and how it ended. Those become
progress for the interface, and everything else stays out of sight unless it
goes wrong.
"""

import json
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

HERE = Path(__file__).resolve().parent
DEFAULT_COOKIES = HERE / "cookies.txt"
WORKER = HERE / "gamdl_worker.py"

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

# What gamdl_worker.py puts in front of its event lines.
EVENT_PREFIX = "@@gamdl "
# gamdl's own messages: "✗ Song: reason", "Error: ...", "! warning".
ERROR_LINE = re.compile(r"^(?:✗|Error:|Errors \(\d+\):)")
WARNING_LINE = re.compile(r"^! ")

STAGES = {
    "fetching_tags": "Reading tags",
    "fetching_streams": "Finding streams",
    "fetching_decryption_keys": "Getting keys",
    "downloading": "Downloading",
    "muxing": "Muxing",
    "tagging": "Tagging",
}


def _gamdl_command(url, destination, cookies):
    """Build the gamdl call, running it through this interpreter.

    Running it with this interpreter rather than the gamdl executable means it
    works even when the virtual environment has not been activated.
    """
    if not module_available("gamdl"):
        raise MissingDependency(
            "gamdl is not installed. Run: pip install -r requirements.txt"
        )

    return [
        python_executable(),
        str(WORKER),
        "--auth-mode",
        "cookies",
        "--cookies-path",
        str(cookies),
        "--output-dir",
        str(destination),
        # AAC through the Widevine route cookies give; the web stream if not.
        "--song-codec-priority",
        "aac,aac-web",
        "--no-lyrics-file",
        # gamdl's media index would skip songs it remembers downloading to
        # an earlier scratch folder.
        "--no-database",
        # One track at a time, so progress reads as one track after another.
        "--concurrent-downloads",
        "1",
        "--no-progress",
        *FILE_TEMPLATES,
        url,
    ]


class _GamdlOutput:
    """Turns gamdl's console output into reporter calls."""

    def __init__(self, reporter):
        self.reporter = reporter
        self.tail = []  # the last few lines, to explain a failure
        self.errors = []  # why individual tracks failed
        self.total = None
        self.title = None

    def feed(self, line):
        line = strip_ansi(line).strip()
        if not line:
            return
        if line.startswith(EVENT_PREFIX):
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                return
            self._event(event)
            return

        self.tail = (self.tail + [line])[-15:]
        if ERROR_LINE.match(line):
            if not line.startswith("Errors ("):
                self.reporter.log(line.lstrip("✗").strip(), "error")
        elif WARNING_LINE.match(line):
            self.reporter.log(line[2:], "warning")

    def _event(self, event):
        kind = event.get("type")
        if kind == "discovered":
            if event.get("final"):
                self.total = event.get("count")
        elif kind == "track":
            self.title = event.get("title") or ""
            self.reporter.item(event.get("index", 1), self.total, self.title)
            self.reporter.stage("Looking it up")
            self.reporter.progress(None)
        elif kind == "stage":
            stage = event.get("stage", "")
            self.reporter.stage(STAGES.get(stage, stage.replace("_", " ").capitalize()))
            if stage == "downloading":
                self.reporter.progress(0.0)
        elif kind == "progress":
            percent = event.get("percent")
            if isinstance(percent, (int, float)):
                self.reporter.progress(min(max(percent / 100, 0.0), 1.0))
        elif kind == "finished":
            status = event.get("status")
            title = event.get("title") or self.title or "Track"
            if status == "failed":
                reason = event.get("message") or "failed"
                self.errors.append(f"{title}: {reason}")
                self.reporter.log(f"{title}: {reason}", "error")
            elif status == "skipped":
                reason = event.get("message")
                self.reporter.log(
                    f"Skipped {title}" + (f": {reason}" if reason else ""), "warning"
                )

    def explain(self):
        if self.errors:
            return self.errors[-1]
        errors = [line for line in self.tail if ERROR_LINE.match(line) and not line.startswith("Errors (")]
        return (errors or self.tail or ["(no output)"])[-1].lstrip("✗").strip()


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
