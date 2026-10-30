"""Build voice datasets: just an artist's dry lead vocals, for training RVC.

The Voice tab downloads songs (a playlist, an album, a single link or a
search) and keeps only the singing, with the backing vocals and the reverb
taken out, in one folder per artist. Point RVC's training at that folder.

Every song goes through the separation engine (separation.py) one model
after another:

    the song        → vocals          (BS-Roformer Leap, as in the Stems presets)
    vocals          → lead vocals     (BS-Roformer Karaoke; drops harmonies and ad-libs)
    lead vocals     → dry vocals      (anvuew's BS-Roformer De-Reverb; takes out echo too)

then the stretches of silence between phrases are cut out and the result is
saved as a mono 16-bit WAV, which is what RVC's preprocessing wants anyway.
The downloaded songs are thrown away; only the vocals are kept.
"""

import re
import unicodedata
import wave
from pathlib import Path

from separation import DEREVERB, DEREVERB_FAST, INST_HQ_3, KARA_2, KARAOKE, LEAP
from separation import Preset, Step
from utils import DownloadError, find_ffmpeg, run_quiet

# Pauses longer than this are cut down to KEEP_SILENCE. RVC slices its
# training audio at pauses anyway; this just stops the folder (and the minutes
# counter) filling up with instrumental breaks.
SILENCE_THRESHOLD_DB = -45
MIN_SILENCE = 1.0
KEEP_SILENCE = 0.4
# Anything shorter than this after trimming is not worth training on: an
# instrumental, or a song the artist barely sings in.
MIN_SECONDS = 5

# Characters Windows does not allow in a folder name.
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def preset(lead_only=True, reverb="reverb"):
    """The separation steps for a voice dataset, as a Preset.

    The one stem it keeps is called "Vocals"; everything else is an
    intermediate ("_" names) that the engine throws away.
    """
    dry = reverb == "reverb"
    # Each model's output is the next one's input, and the last one's is kept.
    best, fast = [], []
    out = "_vocals" if lead_only or dry else "Vocals"
    best.append(Step(LEAP, "song", {"Vocals": out, "Other": "_instrumental"}))
    fast.append(Step(INST_HQ_3, "song", {"Vocals": out, "Instrumental": "_instrumental"}))
    source = out

    if lead_only:
        # A karaoke model run on vocals alone splits them into the lead (its
        # "Vocals") and the backing vocals (its "Instrumental").
        out = "_lead" if dry else "Vocals"
        best.append(Step(KARAOKE, source, {"Vocals": out, "Instrumental": "_backing"}))
        fast.append(Step(KARA_2, source, {"Vocals": out, "Instrumental": "_backing"}))
        source = out

    if dry:
        best.append(Step(DEREVERB, source, {"noreverb": "Vocals", "reverb": "_reverb"}))
        # FoxJoy's VR model handles both reverb and echo, several times faster.
        fast.append(Step(DEREVERB_FAST, source, {"No Reverb": "Vocals", "Reverb": "_reverb"}))

    return Preset("Voice dataset", "Voice", "Dry lead vocals for training a voice model.", tuple(best), tuple(fast))


# --------------------------------------------------------------------------
# The artist's folder
# --------------------------------------------------------------------------


def folder_name(artist):
    """``artist`` made safe to use as a folder name, or "" if nothing is left."""
    name = UNSAFE.sub("", " ".join(artist.split()))
    return name.strip(" .")


def artist_folders(root):
    """The artists that already have a folder under ``root``, by name."""
    try:
        return sorted((p.name for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.lower)
    except OSError:
        return []


def match_key(title):
    """A loose form of a song title, for spotting songs that are already done.

    yt-dlp and Windows both swap awkward characters in file names (a "?"
    becomes a full width "？", for instance), so names are compared by their
    letters and digits only.
    """
    title = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[\W_]+", "", title)


def existing_keys(folder):
    """match_key() of every song already in ``folder``."""
    try:
        return {match_key(path.stem) for path in Path(folder).glob("*.wav")}
    except OSError:
        return set()


def wav_seconds(path):
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / float(audio.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return 0.0


_stats_cache = {}


def stats(folder):
    """(number of songs, seconds of vocals) in ``folder``.

    Only the WAV headers are read, and the answer is kept until the folder
    changes, so this is cheap enough to call on every screen refresh.
    """
    folder = Path(folder)
    try:
        stamp = folder.stat().st_mtime_ns
    except OSError:
        return 0, 0.0
    cached = _stats_cache.get(folder)
    if cached and cached[0] == stamp:
        return cached[1]
    files = list(folder.glob("*.wav"))
    result = (len(files), sum(wav_seconds(path) for path in files))
    _stats_cache[folder] = (stamp, result)
    return result


def format_minutes(seconds):
    minutes = seconds / 60
    if minutes < 1:
        return f"{seconds:.0f} s"
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours, minutes = divmod(round(minutes), 60)
    return f"{hours} h {minutes:02} min"


# --------------------------------------------------------------------------
# Finishing a song's vocals
# --------------------------------------------------------------------------


def finish(vocals, destination, trim_silence=True):
    """Save ``vocals`` as a mono 16-bit WAV at ``destination``, pauses trimmed.

    Returns the seconds of audio saved. Raises DownloadError if ffmpeg fails,
    or if there is too little singing left to be worth keeping (the file is
    not saved then).
    """
    destination = Path(destination)
    command = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(vocals)]
    if trim_silence:
        threshold = f"{SILENCE_THRESHOLD_DB}dB"
        command += [
            "-af",
            f"silenceremove=start_periods=1:start_threshold={threshold}"
            f":stop_periods=-1:stop_duration={MIN_SILENCE}:stop_threshold={threshold}"
            f":stop_silence={KEEP_SILENCE}",
        ]
    command += ["-ac", "1", "-codec:a", "pcm_s16le", "-map_metadata", "-1", str(destination)]
    result = run_quiet(command)
    if result.returncode != 0 or not destination.exists():
        destination.unlink(missing_ok=True)
        raise DownloadError(f"ffmpeg could not save the vocals: {(result.stdout or '').strip()[-300:]}")

    seconds = wav_seconds(destination)
    if seconds < MIN_SECONDS:
        destination.unlink(missing_ok=True)
        raise DownloadError("hardly any singing was found, so it was left out")
    return seconds
