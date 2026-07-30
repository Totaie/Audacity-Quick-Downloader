"""Download a YouTube video or Apple Music track and import it into Audacity.

Run it with URLs to download them straight away, or with no arguments to be
prompted for one URL after another.
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import audacity
from applemusicdownloader import DEFAULT_COOKIES, download_apple_music
from utils import DownloadError, default_downloads_dir, describe_path
from youtubedownloader import download_youtube_audio

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
APPLE_HOSTS = {
    "music.apple.com",
    "geo.music.apple.com",
    "itunes.apple.com",
    "beta.music.apple.com",
}

QUIT_WORDS = {"q", "quit", "exit"}


def classify_url(url):
    """Return "youtube", "apple" or None for a pasted URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host in YOUTUBE_HOSTS:
        return "youtube"
    if host in APPLE_HOSTS:
        return "apple"
    return None


def looks_like_playlist(url):
    """True when a YouTube URL points at a playlist as well as a video."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return "list" in parse_qs(parsed.query)


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


def download(url, kind, options):
    """Download one URL and return the files that were saved."""
    if kind == "apple":
        print("Downloading from Apple Music...")
        return download_apple_music(
            url, output_dir=options.output, cookies=options.cookies
        )

    playlist = options.playlist
    if playlist is None:
        playlist = looks_like_playlist(url) and ask_yes_no(
            "That link is part of a playlist. Download the whole playlist?"
        )

    print("Downloading the playlist..." if playlist else "Downloading from YouTube...")
    return download_youtube_audio(
        url,
        output_dir=options.output,
        playlist=playlist,
        quality=options.quality,
        cookies=options.youtube_cookies,
        thumbnail=not options.no_thumbnail,
    )


def import_into_audacity(tracks, options, timeout=audacity.STARTUP_TIMEOUT):
    """Import downloaded files into Audacity, without losing them if that fails."""
    if options.no_import:
        return

    try:
        with audacity.connect(timeout) as session:
            for track in tracks:
                session.import_file(track)
                print(f"Imported {track.name}")
    except audacity.AudacityError as error:
        print(f"\nCould not import into Audacity: {error}")
        print(f"Your files are safe in {describe_path(options.output)}.")


def handle(url, options):
    """Download one URL and import whatever came back."""
    kind = classify_url(url)
    if kind is None:
        print(f"{url} is not a YouTube or Apple Music link.")
        return False

    # Audacity is started before downloading so it has time to get going while
    # the download runs.
    audacity_expected = True
    if not options.no_import:
        try:
            audacity_expected = audacity.ensure_running(
                auto_launch=not options.no_launch, ask=ask_yes_no
            )
        except audacity.AudacityError as error:
            print(f"{error}\nCarrying on with the download anyway.")
            audacity_expected = False

    try:
        tracks = download(url, kind, options)
    except DownloadError as error:
        print(f"\nDownload failed: {error}")
        return False

    print(f"\nSaved {len(tracks)} file(s) to {describe_path(options.output)}:")
    for track in tracks:
        print(f"  {track.name}")

    # Only wait out the long startup timeout when Audacity is actually on its
    # way; otherwise give the user a moment to start it and move on.
    import_into_audacity(
        tracks, options, audacity.STARTUP_TIMEOUT if audacity_expected else 10
    )
    return True


def interactive(options):
    """Prompt for URLs until the user quits."""
    print("Paste a YouTube or Apple Music URL, or 'q' to quit.")
    while True:
        try:
            url = input("\nURL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not url:
            continue
        if url.lower() in QUIT_WORDS:
            return 0
        try:
            handle(url, options)
        except KeyboardInterrupt:
            print("\nCancelled.")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="YouTube or Apple Music URLs. Omit them to be prompted instead.",
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
        help="MP3 bitrate in kbps for YouTube downloads (default: 192)",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=DEFAULT_COOKIES,
        help="Apple Music cookies file (default: cookies.txt next to this script)",
    )
    parser.add_argument(
        "--youtube-cookies",
        type=Path,
        help="optional cookies file for age restricted YouTube videos",
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
        help="do not embed the video thumbnail as cover art",
    )
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_arguments(argv)
    options.output = Path(options.output).expanduser().resolve()

    if not options.urls:
        return interactive(options)

    failures = 0
    for url in options.urls:
        try:
            if not handle(url, options):
                failures += 1
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
