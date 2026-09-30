"""The audio formats the app saves, and converting between them with ffmpeg.

Downloads and separated stems both go through here, so a stem ends up in the
same format, at the same bitrate, and with the same tags and cover art as the
song it came from.
"""

from pathlib import Path

from utils import DownloadError, find_ffmpeg, run_quiet

# The value is what yt-dlp and ffmpeg call the codec; the extension is what
# ends up on disk.
AUDIO_FORMATS = {
    "mp3": {"label": "MP3", "codec": "mp3", "extension": ".mp3", "lossy": True},
    "m4a": {"label": "M4A (AAC)", "codec": "m4a", "extension": ".m4a", "lossy": True},
    "opus": {"label": "Opus", "codec": "opus", "extension": ".opus", "lossy": True},
    "ogg": {"label": "Ogg Vorbis", "codec": "vorbis", "extension": ".ogg", "lossy": True},
    "flac": {"label": "FLAC (lossless)", "codec": "flac", "extension": ".flac", "lossy": False},
    "wav": {"label": "WAV (lossless)", "codec": "wav", "extension": ".wav", "lossy": False},
}
BITRATES = ("128", "192", "256", "320")

# ffmpeg encoder settings for each output format. {bitrate} is filled in.
ENCODERS = {
    "mp3": ["-codec:a", "libmp3lame", "-b:a", "{bitrate}k", "-id3v2_version", "3"],
    "m4a": ["-codec:a", "aac", "-b:a", "{bitrate}k"],
    "opus": ["-codec:a", "libopus", "-b:a", "{bitrate}k"],
    "ogg": ["-codec:a", "libvorbis", "-b:a", "{bitrate}k"],
    "flac": ["-codec:a", "flac"],
    "wav": ["-codec:a", "pcm_s16le"],
}
# Formats that can carry the artwork along as an attached picture.
COVER_ART = {"mp3", "m4a", "flac"}


def extension(audio_format):
    return AUDIO_FORMATS[audio_format]["extension"]


def convert_audio(
    source,
    audio_format="mp3",
    bitrate="192",
    destination=None,
    tags_from=None,
    title=None,
    cover_art=True,
):
    """Convert ``source`` to ``audio_format`` and return the new file.

    Tags (and cover art, where the format allows) are copied from
    ``tags_from``, or from ``source`` itself. ``title`` replaces the title
    tag. Without ``destination`` the file is written next to ``source``.
    """
    source = Path(source)
    tags_from = Path(tags_from) if tags_from else source
    if destination is None:
        destination = source.with_suffix(extension(audio_format))
    destination = Path(destination)
    in_place = destination == source
    if in_place:
        destination = source.with_name(f"{source.stem}.converted{source.suffix}")

    ffmpeg = find_ffmpeg()
    inputs = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source)]
    tags = 0
    if tags_from != source:
        inputs += ["-i", str(tags_from)]
        tags = 1
    encode = [part.format(bitrate=bitrate) for part in ENCODERS[audio_format]]
    encode += ["-map_metadata", str(tags)]
    if title:
        encode += ["-metadata", f"title={title}"]
    encode.append(str(destination))

    # First try keeps the cover art; the fallback drops anything the container
    # cannot hold (artwork in WAV, music videos, odd side streams). The "?"
    # makes the picture optional, for sources that have none.
    attempts = [inputs + ["-map", "0:a"] + encode]
    if cover_art and audio_format in COVER_ART:
        attempts.insert(
            0,
            inputs
            + ["-map", "0:a", "-map", f"{tags}:v?", "-codec:v", "copy", "-disposition:v", "attached_pic"]
            + encode,
        )

    last_output = ""
    for command in attempts:
        result = run_quiet(command)
        if result.returncode == 0 and destination.exists():
            if in_place:
                destination = destination.replace(source)
            return destination
        last_output = (result.stdout or "").strip()

    raise DownloadError(f"ffmpeg could not convert {source.name}: {last_output}")
