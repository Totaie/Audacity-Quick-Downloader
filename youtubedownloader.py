"""Download YouTube audio as MP3 using yt-dlp."""

from pathlib import Path

from utils import (
    DownloadError,
    MissingDependency,
    collect_files,
    default_downloads_dir,
    find_ffmpeg,
    move_into,
    temp_workspace,
)

try:
    import yt_dlp
except ImportError:  # pragma: no cover - only hit when dependencies are missing
    yt_dlp = None


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
        "noprogress": False,
        "consoletitle": False,
        # Keep going when a playlist contains a private or deleted video.
        "ignoreerrors": "only_download" if playlist else False,
    }
    if cookies:
        options["cookiefile"] = str(cookies)
    return options


def download_youtube_audio(
    url,
    output_dir=None,
    playlist=False,
    quality="192",
    cookies=None,
    thumbnail=True,
):
    """Download ``url`` and return the MP3 paths saved in ``output_dir``.

    Everything is downloaded into a scratch folder first so we know exactly
    which files this run produced, then moved into the output folder without
    overwriting anything that is already there.
    """
    if yt_dlp is None:
        raise MissingDependency(
            "yt-dlp is not installed. Run: pip install -r requirements.txt"
        )

    output_dir = Path(output_dir) if output_dir else default_downloads_dir()

    with temp_workspace(output_dir) as workspace:
        options = build_options(
            workspace,
            quality=quality,
            playlist=playlist,
            cookies=cookies,
            thumbnail=thumbnail,
        )

        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except yt_dlp.utils.DownloadError as error:
            raise DownloadError(str(error))

        tracks = collect_files(workspace, {".mp3"})
        if not tracks:
            raise DownloadError(
                "yt-dlp did not produce any MP3 files. The video may be "
                "unavailable, private or region locked."
            )

        return [move_into(track, output_dir) for track in tracks]
