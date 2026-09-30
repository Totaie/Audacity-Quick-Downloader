"""Download audio from YouTube, SoundCloud, Bandcamp, Apple Music and 1,000+
other sites, and import it straight into Audacity.

Run it in a terminal for the full screen interface. Links given on the
command line are queued as soon as it opens. Use --plain (or pipe the
output somewhere) for simple line-by-line output instead.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import audacity
from jobs import CANCELLED, DONE, FAILED, RUNNING, Runner
from separation import PRESETS
from settings import AUDIO_FORMATS, BITRATES, settings_path
from settings import load as load_settings
from sources import identify, split_input
from updater import Updater

QUIT_WORDS = {"q", "quit", "exit"}


def ask_yes_no(question, default=False):
    """Ask a yes/no question, falling back to ``default`` when there is no console."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    suffix = "(Y/n)" if default else "(y/N)"
    try:
        answer = input(f"{question} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")


# --------------------------------------------------------------------------
# Plain mode: a line or two per download, no progress bars
# --------------------------------------------------------------------------


class PlainPrinter:
    """Prints what the jobs are doing, one short line at a time."""

    SYMBOLS = {DONE: "✓", FAILED: "✗", CANCELLED: "-"}

    def __init__(self, runner):
        self.runner = runner
        self.seen = {}

    def show(self):
        for job in self.runner.jobs:
            before = self.seen.get(job.id)
            track = (job.index, job.title) if job.total and job.total > 1 else None
            now = (job.state, track)
            if now == before:
                continue
            self.seen[job.id] = now
            if job.state == RUNNING and (before is None or before[0] != RUNNING):
                print(f"[{job.id}] {job.source.name}: {job.text}")
            if job.state == RUNNING and track and track[1]:
                print(f"[{job.id}]   {job.index}/{job.total}  {job.title}")
            if job.finished_state:
                symbol = self.SYMBOLS[job.state]
                print(f"[{job.id}] {symbol} {job.label}: {job.message}")
                if job.warning:
                    print(f"[{job.id}]   {job.warning}")

        # yt-dlp's warnings are mostly noise; anything that actually stops a
        # download is already in the job's result line.
        while not self.runner.logs.empty():
            self.runner.logs.get_nowait()

    def wait(self):
        """Keep printing until every job has finished."""
        while self.runner.active():
            self.show()
            time.sleep(0.2)
        self.show()


def plain_add(runner, text):
    source = identify(text)
    playlist = runner.settings.playlist
    if source.ambiguous_playlist and playlist is None:
        playlist = ask_yes_no("That link is part of a playlist. Download the whole playlist?")
    return runner.add(text, playlist)


def run_plain(settings, updater, urls):
    runner = Runner(settings, updater, ask=lambda question, default=False: ask_yes_no(question, default))
    printer = PlainPrinter(runner)
    try:
        if urls:
            for url in urls:
                for entry in split_input(url):
                    plain_add(runner, entry)
            printer.wait()
            return 1 if any(job.state != DONE for job in runner.jobs) else 0

        print("Paste a link or type a song name, or 'q' to quit.")
        while True:
            try:
                text = input("\n> ").strip()
            except EOFError:
                print()
                return 0
            if text.lower() in QUIT_WORDS:
                return 0
            for entry in split_input(text):
                plain_add(runner, entry)
            printer.wait()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    finally:
        runner.shutdown()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Saved defaults live in {settings_path()}. Change them from the "
        "Settings screen (Ctrl+S); the options above only apply to one run.",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="links (or song names to search for) to download straight away",
    )
    parser.add_argument("-o", "--output", type=Path, help="where to save downloads")
    parser.add_argument(
        "-f", "--format", choices=AUDIO_FORMATS, dest="audio_format", help="audio format to save"
    )
    parser.add_argument(
        "-q", "--quality", choices=BITRATES, dest="bitrate", help="bitrate in kbps for lossy formats"
    )
    parser.add_argument("--cookies", type=Path, help="Apple Music cookies file")
    parser.add_argument(
        "--site-cookies",
        "--youtube-cookies",
        dest="site_cookies",
        type=Path,
        help="cookies file for yt-dlp, e.g. for age restricted YouTube videos",
    )

    playlist = parser.add_mutually_exclusive_group()
    playlist.add_argument(
        "--playlist",
        dest="playlist",
        action="store_true",
        default=None,
        help="download the whole playlist without asking",
    )
    playlist.add_argument(
        "--no-playlist",
        dest="playlist",
        action="store_false",
        help="download only the linked video, even if it is part of a playlist",
    )

    separate = parser.add_mutually_exclusive_group()
    separate.add_argument(
        "--separate",
        dest="separate",
        action="store_true",
        default=None,
        help="split downloads into stems with UVR5's models",
    )
    separate.add_argument(
        "--no-separate", dest="separate", action="store_false", help="do not split into stems"
    )
    parser.add_argument("--preset", choices=PRESETS, help="which stems to split into")
    parser.add_argument("--stems-dir", type=Path, help="where to save separated stems")

    parser.add_argument("--no-launch", action="store_true", help="never start Audacity automatically")
    parser.add_argument("--no-import", action="store_true", help="do not touch Audacity at all")
    parser.add_argument(
        "--no-thumbnail", action="store_true", help="do not embed the thumbnail as cover art"
    )
    parser.add_argument(
        "--no-update", action="store_true", help="do not check for newer yt-dlp and gamdl releases"
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="simple line-by-line output instead of the full screen interface",
    )
    return parser.parse_args(argv)


def apply_arguments(settings, options):
    """Let command line options override the saved settings, for this run only."""
    overrides = {
        "download_dir": options.output,
        "audio_format": options.audio_format,
        "bitrate": options.bitrate,
        "apple_cookies": options.cookies,
        "site_cookies": options.site_cookies,
        "separate": options.separate,
        "separation_preset": options.preset,
        "separation_dir": options.stems_dir,
    }
    for name, value in overrides.items():
        if value is not None:
            setattr(settings, name, str(Path(value).expanduser().resolve()) if isinstance(value, Path) else value)
    if options.playlist is not None:
        settings.playlist_mode = "all" if options.playlist else "one"
    if options.no_launch:
        settings.launch_audacity = False
    if options.no_import:
        settings.import_to_audacity = False
    if options.no_thumbnail:
        settings.embed_thumbnail = False
    return settings


def main(argv=None):
    # Song titles can hold anything; never crash printing one to a console
    # or pipe that cannot show every character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    options = parse_arguments(argv)
    settings, first_run = load_settings()
    if first_run:
        # Start new users off with what their PC can actually do.
        settings.import_to_audacity = audacity.find_audacity() is not None
    settings = apply_arguments(settings, options)

    # Start checking for updates straight away; it runs in the background.
    skip_update = options.no_update or os.environ.get("AQD_SKIP_UPDATE")
    updater = Updater(enabled=not skip_update).start()

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not options.plain:
        try:
            import tui
        except ImportError as error:
            print(f"The full screen interface is unavailable ({error}).")
            print("Run: pip install -r requirements.txt")
            print("Falling back to plain output.\n")
        else:
            return tui.run(settings, updater, options.urls, first_run=first_run)

    if first_run:
        settings.save()  # plain mode has no settings screen; start from defaults
    return run_plain(settings, updater, options.urls)


if __name__ == "__main__":
    sys.exit(main())
