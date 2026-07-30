"""Download Apple Music tracks with gamdl and convert them to MP3."""

import subprocess
from pathlib import Path

from utils import (
    AUDIO_EXTENSIONS,
    DownloadError,
    MissingDependency,
    collect_files,
    default_downloads_dir,
    find_ffmpeg,
    module_available,
    move_into,
    python_executable,
    run_quiet,
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


def convert_to_mp3(source, destination, quality="2"):
    """Convert a downloaded track to MP3, keeping its tags and cover art."""
    ffmpeg = find_ffmpeg()
    base = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source)]
    encode = [
        "-codec:a",
        "libmp3lame",
        "-qscale:a",
        str(quality),
        "-map_metadata",
        "0",
        "-id3v2_version",
        "3",
        str(destination),
    ]

    # First try keeps the embedded cover art; the fallback drops anything the
    # MP3 container cannot hold (music videos, odd side streams).
    attempts = [
        base + ["-map", "0", "-codec:v", "copy"] + encode,
        base + ["-map", "0:a"] + encode,
    ]

    last_output = ""
    for command in attempts:
        result = run_quiet(command)
        if result.returncode == 0 and Path(destination).exists():
            return Path(destination)
        last_output = (result.stdout or "").strip()

    raise DownloadError(f"ffmpeg could not convert {Path(source).name}: {last_output}")


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


def download_apple_music(url, output_dir=None, cookies=None, quality="2"):
    """Download ``url`` from Apple Music and return the MP3 paths saved on disk."""
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
        try:
            # Output is left on the console so gamdl's progress stays visible.
            result = subprocess.run(command, check=False)
        except OSError as error:
            raise DownloadError(f"Could not run gamdl: {error}")

        downloaded = collect_files(workspace, AUDIO_EXTENSIONS)
        if not downloaded:
            raise DownloadError(
                "gamdl did not download anything (exit code "
                f"{result.returncode}). Common causes: expired cookies.txt, a "
                "track your subscription cannot play, or a gamdl that has "
                "fallen behind Apple's website - try "
                "'pip install --upgrade gamdl'."
            )
        if result.returncode != 0:
            print(
                f"gamdl reported an error (exit code {result.returncode}); "
                "importing the tracks it did manage to download."
            )

        tracks = []
        for source in downloaded:
            if source.suffix.lower() == ".mp3":
                tracks.append(move_into(source, output_dir))
                continue
            print(f"Converting {source.name} to MP3...")
            mp3 = convert_to_mp3(source, source.with_suffix(".mp3"), quality=quality)
            tracks.append(move_into(mp3, output_dir))

        return tracks


if __name__ == "__main__":
    for path in download_apple_music(input("Enter Apple Music URL: ").strip()):
        print(path)
