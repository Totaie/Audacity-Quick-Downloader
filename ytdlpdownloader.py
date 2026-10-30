"""Download audio or video with yt-dlp: YouTube, SoundCloud, Bandcamp and more.

yt-dlp is kept quiet; its progress comes back through a :class:`Reporter`
instead of being printed.
"""

import re
import shutil
from pathlib import Path

from formats import AUDIO_FORMATS, convert_audio
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
    "ExtractAudio": "Converting",
    "Metadata": "Tagging",
    "EmbedThumbnail": "Adding cover art",
    "Merger": "Joining video and audio",
    "VideoRemuxer": "Saving as MP4",
}

# The Videos tab's quality choices: the tallest video to download, in pixels.
VIDEO_HEIGHTS = {"best": None, "2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480}


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
    message = message.split("\n")[0].strip()
    # yt-dlp's advice names its own command line flags, which mean nothing
    # here. Point at our setting instead.
    if "--cookies-from-browser or --cookies" in message:
        message = message.split(". Use --cookies-from-browser")[0].rstrip(".") + (
            ". Sign in to the site in your browser, export its cookies to a "
            "cookies.txt file, and choose it under Settings > Cookies > "
            "Other sites' cookies (or pass --site-cookies)."
        )
    return message


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


def build_options(
    destination, quality="192", playlist=False, cookies=None, thumbnail=True, audio_format="mp3"
):
    """yt-dlp options that produce tagged audio files inside ``destination``."""
    output = AUDIO_FORMATS[audio_format]
    extract = {"key": "FFmpegExtractAudio", "preferredcodec": output["codec"]}
    if output["lossy"]:
        extract["preferredquality"] = str(quality)
    # WAV has nowhere to put cover art.
    thumbnail = thumbnail and audio_format != "wav"
    postprocessors = [
        extract,
        # Title/artist/album tags. The old "addmetadata" option did nothing:
        # yt-dlp only writes tags when this post-processor is present.
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if thumbnail:
        # Likewise, cover art needs both the post-processor and writethumbnail.
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    options = _common_options(destination, playlist, cookies)
    options.update(format="bestaudio/best", postprocessors=postprocessors, writethumbnail=thumbnail)
    return options


def build_video_options(destination, height=None, playlist=False, cookies=None, thumbnail=True):
    """yt-dlp options that produce tagged .mp4 videos inside ``destination``.

    Formats are ranked by resolution (up to ``height``), then H.264 video and
    AAC audio, which play everywhere: Windows' own apps, phones and video
    editors. Above 1080p YouTube only has VP9 and AV1, which the .mp4 still
    holds, but some older players cannot.
    """
    postprocessors = [
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if thumbnail:
        # As a .jpg, kept after embedding: the MP3 made from the video uses it too.
        postprocessors.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})
    options = _common_options(destination, playlist, cookies)
    options.update(
        format="bv*+ba/b",
        format_sort=[f"res:{height}" if height else "res", "vcodec:h264", "acodec:aac"],
        merge_output_format="mp4",
        postprocessors=postprocessors,
        writethumbnail=thumbnail,
    )
    return options


def _common_options(destination, playlist, cookies):
    options = {
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
    audio_format="mp3",
    skip=None,
):
    """Download ``url`` and return the audio files saved in ``output_dir``.

    Everything is downloaded into a scratch folder first so we know exactly
    which files this run produced, then moved into the output folder without
    overwriting anything that is already there.

    ``skip(title)`` can return True to leave a track out without downloading
    it, e.g. one that is already done.
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
            audio_format=audio_format,
        )
        if skip:
            # yt-dlp skips an entry when this returns a reason. It can ask
            # more than once about the same entry.
            def match_filter(info, *, incomplete=False):
                title = info.get("title")
                return "already done" if title and skip(title) else None

            options["match_filter"] = match_filter
        logger = _run(yt_dlp, options, url, reporter)

        tracks = collect_files(workspace, {AUDIO_FORMATS[audio_format]["extension"]})
        if not tracks:
            if logger.errors:
                raise DownloadError(logger.errors[-1])
            raise DownloadError(
                "Nothing was downloaded. The link may not contain any audio, "
                "or it may be private, removed or region locked."
            )

        return [move_into(track, output_dir) for track in tracks]


def download_video(
    url,
    output_dir=None,
    playlist=False,
    quality="1080",
    cookies=None,
    thumbnail=True,
    reporter=None,
    mp3=True,
    bitrate="192",
):
    """Download ``url`` as .mp4 video. Returns [(video, mp3 or None), ...].

    With ``mp3``, each video's sound is also saved as an MP3 next to it,
    carrying the same tags and cover art.
    """
    yt_dlp = _import_yt_dlp()
    reporter = reporter or Reporter()
    output_dir = Path(output_dir) if output_dir else default_downloads_dir() / "Videos"

    with temp_workspace(output_dir) as workspace:
        options = build_video_options(
            workspace, height=VIDEO_HEIGHTS.get(quality), playlist=playlist, cookies=cookies, thumbnail=thumbnail
        )
        logger = _run(yt_dlp, options, url, reporter)

        videos = collect_files(workspace, {".mp4"})
        if not videos:
            if logger.errors:
                raise DownloadError(logger.errors[-1])
            raise DownloadError(
                "Nothing was downloaded. The link may not contain a video, "
                "or it may be private, removed or region locked."
            )

        saved = []
        for number, video in enumerate(videos, 1):
            audio = None
            if mp3:
                reporter.check_cancelled()
                if len(videos) > 1:
                    reporter.item(number, len(videos), video.stem)
                reporter.stage("Saving the MP3")
                reporter.progress(None)
                cover = video.with_suffix(".jpg")
                audio = convert_audio(
                    video,
                    "mp3",
                    bitrate,
                    destination=video.with_suffix(".mp3"),
                    cover_art=thumbnail and cover.exists(),
                    cover_image=cover if cover.exists() else None,
                )
            saved.append((move_into(video, output_dir), move_into(audio, output_dir) if audio else None))
        return saved


def _run(yt_dlp, options, url, reporter):
    """Run yt-dlp with progress going to ``reporter``. Returns its logger."""
    logger = _Logger(reporter)
    on_progress, on_postprocess = _hooks(reporter, yt_dlp)
    options.update(logger=logger, progress_hooks=[on_progress], postprocessor_hooks=[on_postprocess])
    reporter.stage("Looking it up")
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
    except yt_dlp.utils.DownloadCancelled:
        raise Cancelled("Cancelled.")
    except yt_dlp.utils.DownloadError as error:
        raise DownloadError(logger.errors[-1] if logger.errors else tidy_error(strip_ansi(str(error))))
    reporter.check_cancelled()
    return logger
