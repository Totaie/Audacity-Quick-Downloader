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

from applemusicdownloader import DEFAULT_COOKIES
from jobs import CANCELLED, DONE, FAILED, RUNNING, Runner
from sources import identify, split_input
from updater import Updater
from utils import default_downloads_dir

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
    playlist = runner.options.playlist
    if source.ambiguous_playlist and playlist is None:
        playlist = ask_yes_no("That link is part of a playlist. Download the whole playlist?")
    return runner.add(text, playlist)


def run_plain(options, updater):
    runner = Runner(options, updater, ask=lambda question, default=False: ask_yes_no(question, default))
    printer = PlainPrinter(runner)
    try:
        if options.urls:
            for url in options.urls:
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
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="links (or song names to search for) to download straight away",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_downloads_dir(),
        help="where to save the MP3s (default: the Downloads folder next to this script)",
    )
    parser.add_argument(
        "-q",
        "--quality",
        default="192",
        help="MP3 bitrate in kbps for everything except Apple Music (default: 192)",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=DEFAULT_COOKIES,
        help="Apple Music cookies file (default: cookies.txt next to this script)",
    )
    parser.add_argument(
        "--site-cookies",
        "--youtube-cookies",
        dest="site_cookies",
        type=Path,
        help="optional cookies file for yt-dlp, e.g. for age restricted YouTube "
        "videos or SoundCloud Go+ tracks",
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

    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="never start Audacity automatically",
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help="just download; do not touch Audacity at all",
    )
    parser.add_argument(
        "--no-thumbnail",
        action="store_true",
        help="do not embed the thumbnail as cover art",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="do not check for newer yt-dlp and gamdl releases",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="simple line-by-line output instead of the full screen interface",
    )
    return parser.parse_args(argv)


def main(argv=None):
    # Song titles can hold anything; never crash printing one to a console
    # or pipe that cannot show every character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    options = parse_arguments(argv)
    options.output = Path(options.output).expanduser().resolve()

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
            return tui.run(options, updater, options.urls)

    return run_plain(options, updater)


if __name__ == "__main__":
    sys.exit(main())
