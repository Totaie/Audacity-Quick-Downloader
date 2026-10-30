"""Work out a song's tempo (BPM) and save it in the file's tags.

The method is the classic one librosa uses: an onset strength envelope (how
much new sound starts in each moment), autocorrelated to find the beat period
that repeats most, weighted towards tempos near 120 BPM to settle the usual
half/double tempo ambiguity. It runs on numpy alone, decoding with ffmpeg, and
takes about a second per song.

The tag is TBPM in MP3s, tmpo in M4A/MP4, and BPM in FLAC, Ogg and Opus.
WAV has no standard place for it.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

from utils import find_ffmpeg

SAMPLE_RATE = 22050
N_FFT = 2048
HOP = 128  # 172 frames a second: fine enough to tell 128 from 129 BPM
MIN_BPM, MAX_BPM = 50, 220
PRIOR_BPM, PRIOR_OCTAVES = 120, 1.0  # librosa's default prior
MAX_SECONDS = 360
# Below this (the beat's autocorrelation against the whole signal's) there is
# no steady beat to speak of: speech, ambient music, rubato.
MIN_STRENGTH = 0.02


def _decode(path):
    """The first audio stream as mono float32 samples, or None."""
    command = [
        find_ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
        "-ar", str(SAMPLE_RATE), "-t", str(MAX_SECONDS), "-f", "f32le", "-",
    ]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    result = subprocess.run(command, capture_output=True, **options)
    if result.returncode != 0 or not result.stdout:
        return None
    return np.frombuffer(result.stdout, dtype=np.float32)


def _band_filters(count=48, low=30.0, high=11000.0):
    """Triangular filters on a log frequency scale, like a mel filter bank."""
    freqs = np.fft.rfftfreq(N_FFT, 1 / SAMPLE_RATE)
    edges = np.geomspace(low, high, count + 2)
    bank = np.zeros((count, len(freqs)), np.float32)
    for index in range(count):
        lo, mid, hi = edges[index:index + 3]
        bank[index] = np.clip(np.minimum((freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)), 0, None)
    return bank


def onset_envelope(audio):
    """Spectral flux of a log-compressed band spectrogram, one value per frame."""
    if len(audio) < N_FFT * 4:
        return np.zeros(0, np.float32)
    window = np.hanning(N_FFT).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, N_FFT)[::HOP]
    bank = _band_filters()
    bands = []
    for start in range(0, len(frames), 2048):  # in chunks, to keep memory small
        spectrum = np.abs(np.fft.rfft(frames[start:start + 2048] * window, axis=1)).astype(np.float32)
        bands.append(spectrum @ bank.T)
    log = np.log1p(1000 * np.concatenate(bands))
    flux = np.maximum(np.diff(log, axis=0), 0).sum(axis=1)
    # Take away the slow swell (verses getting louder), keeping the onsets.
    width = int(SAMPLE_RATE / HOP / 2)
    trend = np.convolve(flux, np.ones(width) / width, mode="same")
    return np.maximum(flux - trend, 0)


def estimate(envelope):
    """The tempo of an onset envelope in BPM, or None if there is no steady beat."""
    fps = SAMPLE_RATE / HOP
    min_lag, max_lag = int(fps * 60 / MAX_BPM), int(fps * 60 / MIN_BPM) + 2
    if len(envelope) < max_lag * 4:
        return None
    centred = envelope - envelope.mean()
    size = 1 << (2 * len(centred) - 1).bit_length()
    spectrum = np.fft.rfft(centred, size)
    autocorrelation = np.fft.irfft(spectrum * np.conj(spectrum))[: len(centred)]
    if autocorrelation[0] <= 0:
        return None
    # Unbiased, so longer lags (slower tempos) are not penalised for overlapping less.
    autocorrelation = autocorrelation / autocorrelation[0] * len(centred) / (len(centred) - np.arange(len(centred)))

    lags = np.arange(min_lag, max_lag)
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * ((np.log2(bpms) - np.log2(PRIOR_BPM)) / PRIOR_OCTAVES) ** 2)
    score = np.clip(autocorrelation[lags], 0, None) * prior
    best = int(np.argmax(score))
    lag = lags[best]
    if autocorrelation[lag] < MIN_STRENGTH:
        return None
    # Between two lags: fit a parabola through the peak and its neighbours.
    left, middle, right = autocorrelation[lag - 1], autocorrelation[lag], autocorrelation[lag + 1]
    curve = left - 2 * middle + right
    offset = 0.5 * (left - right) / curve if curve < 0 else 0.0
    return 60 * fps / (lag + float(np.clip(offset, -0.5, 0.5)))


def detect(path):
    """The tempo of an audio or video file in BPM, or None if it has no steady beat."""
    audio = _decode(path)
    if audio is None:
        return None
    return estimate(onset_envelope(audio))


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def save(path, bpm):
    """Write ``bpm`` (rounded, as tags hold it) into the file. False if it cannot hold one."""
    path = Path(path)
    value = int(round(bpm))
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        from mutagen.id3 import ID3, TBPM, ID3NoHeaderError

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.setall("TBPM", [TBPM(encoding=3, text=[str(value)])])
        tags.save(path, v2_version=3)  # ID3v2.3, as the rest of the app writes
        return True
    if suffix in (".m4a", ".mp4", ".m4v"):
        from mutagen.mp4 import MP4

        audio = MP4(path)
        audio["tmpo"] = [value]
        audio.save()
        return True
    if suffix in (".flac", ".ogg", ".opus"):
        import mutagen

        audio = mutagen.File(path)
        if audio is None:
            return False
        audio["BPM"] = [str(value)]
        audio.save()
        return True
    return False


def describe(bpms):
    """A short label for one tempo or a range of them, e.g. "128 BPM" or "92–140 BPM"."""
    values = sorted(int(round(bpm)) for bpm in bpms)
    if not values:
        return None
    if values[0] == values[-1]:
        return f"{values[0]} BPM"
    return f"{values[0]}–{values[-1]} BPM"
