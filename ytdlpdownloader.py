"""Download audio as MP3 with yt-dlp: YouTube, SoundCloud, Bandcamp and more.

yt-dlp is kept quiet; its progress comes back through a :class:`Reporter`
instead of being printed.
"""

import re
import shutil
from pathlib import Path

from utils import (
    Cancelled,
    DownloadError,
    MissingDependency,
    Reporter,
    collect_files,
    default_downloads_dir,
    find_ffmpeg,
    move_into,
    strip_ansi,
    temp_workspace,
)

# What each yt-dlp post-processor is doing, in words.
POSTPROCESSOR_STAGES = {
    "ExtractAudio": "Converting to MP3",
    "Metadata": "Tagging",
    "EmbedThumbnail": "Adding cover art",
}


def _import_yt_dlp():
    # Imported on first use rather than at start up, so a background update
    # can finish swapping the package out before it is loaded.
    try:
        import yt_dlp
    except ImportError:
        raise MissingDependency(
            "yt-dlp is not installed. Run: pip install -r requirements.txt"
        )
    return yt_dlp


class _Logger:
    """Sends yt-dlp's warnings and errors to the reporter instead of stderr."""

    def __init__(self, reporter):
        self.reporter = reporter
        self.errors = []

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        self.reporter.log(strip_ansi(message).removeprefix("WARNING: "), "warning")

    def error(self, message):
        message = strip_ansi(message).removeprefix("ERROR: ")
        self.reporter.log(message, "error")
        self.errors.append(tidy_error(message))


def tidy_error(message):
    """Trim yt-dlp's error down to the part a person needs to read.

    "[generic] abc123: Unable to download webpage: HTTP Error 404: Not Found
    (caused by <HTTPError 404: Not Found>)" becomes
    "Unable to download webpage: HTTP Error 404: Not Found".
    """
    message = re.sub(r"^\[[\w:-]+\] [^:]+: ", "", message.strip())
    message = re.sub(r"\s*\(caused by .*\)$", "", message, flags=re.S)
    return message.split("\n")[0].strip()


def find_deno():
    """Deno, which yt-dlp needs to run YouTube's player JavaScript.

    The "deno" package in requirements.txt puts deno.exe in the virtual
    environment's Scripts folder. That folder is not on PATH when the venv's
    Python is run directly (as the .bat does), so yt-dlp would not find it
    by itself.
    """
    try:
        from deno import find_deno_bin

        return find_deno_bin()
    except (ImportError, OSError):
        return shutil.which("deno")


def build_options(destination, quality="192", playlist=False, cookies=None, thumbnail=True):
    """yt-dlp options that produce tagged MP3s inside ``destination``."""
    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(quality),
        },
        # Title/artist/album tags. The old "addmetadata" option did nothing:
        # yt-dlp only writes tags when this post-processor is present.
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if thumbnail:
        # Likewise, cover art needs both the post-processor and writethumbnail.
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    options = {
        "format": "bestaudio/best",
        "postprocessors": postprocessors,
        "writethumbnail": thumbnail,
        "outtmpl": str(Path(destination) / "%(title)s.%(ext)s"),
        "noplaylist": not playlist,
        "ffmpeg_location": find_ffmpeg(),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "consoletitle": False,
        # Keep going when a playlist contains a private or deleted video.
        "ignoreerrors": "only_download" if playlist else False,
    }
    if cookies:
        options["cookiefile"] = str(cookies)
    deno = find_deno()
    if deno:
        options["js_runtimes"] = {"deno": {"path": deno}}
    return options


def _hooks(reporter, yt_dlp):
    """Progress and post-processing hooks that feed ``reporter``."""

    def check():
        if reporter.cancelled:
            # yt-dlp lets this one through its error handling untouched.
            raise yt_dlp.utils.DownloadCancelled("Cancelled.")

    def describe(info):
        index = info.get("playlist_index")
        total = info.get("n_entries") or info.get("playlist_count")
        collection = info.get("playlist_title") or info.get("playlist")
        reporter.item(index, total, info.get("title"), collection)

    def on_progress(status):
        check()
        describe(status.get("info_dict") or {})
        if status["status"] == "downloading":
            reporter.stage("Downloading")
            done = status.get("downloaded_bytes") or 0
            size = status.get("total_bytes") or status.get("total_bytes_estimate")
            fraction = None
            if size:
                fraction = done / size
            elif status.get("fragment_count"):
                fraction = (status.get("fragment_index") or 0) / status["fragment_count"]
            reporter.progress(fraction, status.get("speed"), status.get("eta"))
        elif status["status"] == "finished":
            reporter.progress(1.0)

    def on_postprocess(status):
        check()
        stage = POSTPROCESSOR_STAGES.get(status.get("postprocessor"))
        if stage and status["status"] == "started":
            reporter.stage(stage)
            reporter.progress(None)

    return on_progress, on_postprocess


def download_audio(
    url,
    output_dir=None,
    playlist=False,
    quality="192",
    cookies=None,
    thumbnail=True,
    reporter=None,
):
    """Download ``url`` and return the MP3 paths saved in ``output_dir``.

    Everything is downloaded into a scratch folder first so we know exactly
    which files this run produced, then moved into the output folder without
    overwriting anything that is already there.
    """
    yt_dlp = _import_yt_dlp()
    reporter = reporter or Reporter()
    output_dir = Path(output_dir) if output_dir else default_downloads_dir()

    with temp_workspace(output_dir) as workspace:
        options = build_options(
            workspace,
            quality=quality,
            playlist=playlist,
            cookies=cookies,
            thumbnail=thumbnail,
        )
        logger = _Logger(reporter)
        on_progress, on_postprocess = _hooks(reporter, yt_dlp)
        options.update(
            logger=logger,
            progress_hooks=[on_progress],
            postprocessor_hooks=[on_postprocess],
        )

        reporter.stage("Looking it up")
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except yt_dlp.utils.DownloadCancelled:
            raise Cancelled("Cancelled.")
        except yt_dlp.utils.DownloadError as error:
            raise DownloadError(logger.errors[-1] if logger.errors else tidy_error(strip_ansi(str(error))))
        reporter.check_cancelled()

        tracks = collect_files(workspace, {".mp3"})
        if not tracks:
            if logger.errors:
                raise DownloadError(logger.errors[-1])
            raise DownloadError(
                "Nothing was downloaded. The link may not contain any audio, "
                "or it may be private, removed or region locked."
            )

        return [move_into(track, output_dir) for track in tracks]
