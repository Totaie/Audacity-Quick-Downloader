"""Make a mashup: two songs, separated, brought to one tempo, key and bar line.

The Mashup tab takes two songs. Both are downloaded losslessly, and each is
analysed (tempo.py for its BPM, then here):

    beat grid   the tempo fine-tuned to a hundredth of a BPM, and where the
                first beat falls
    downbeat    which beat of the grid is the "one" of a bar, from where the
                chords change and the bass hits hardest
    key         the song's pitch-class profile (how much of each of the 12
                notes it plays) and the key that fits it best

Each is split into vocals and instrumental with the separation engine. Then
every stem is time-stretched to a shared tempo, the vocals song is moved by
the number of semitones that best fits the other song's notes (pitch only,
with the singer's formants kept, so they do not sound chipmunked), the vocals
are brought to the same level against the new instrumental as they had
against their own, and a little silence is put in front of one song so both
start on the "one" of a bar together. The results land in Audacity on top of
each other, ready to mix.

Which tempo they meet at is a choice:

    "beat"     the song that gives the instrumental keeps its tempo, and only
               the vocals are stretched (vocals stretch the most gracefully)
    "halfway"  both are stretched to the tempo in between, so neither moves far

Tempos that are about double or half of each other (70 against 140 BPM) are
treated as the same feel, so nothing gets stretched to twice its speed.

Stretching and pitch shifting use ffmpeg's rubberband filter when ffmpeg has
it (most full builds do), which keeps vocals natural, and its atempo and
asetrate filters otherwise.
"""

import functools
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import tempo
from utils import Cancelled, DownloadError, find_ffmpeg

TEMPO_MODES = {"beat": "Instrumental's tempo", "halfway": "Meet halfway"}
VOCALS_FROM = {"first": "Song 1", "second": "Song 2"}

# Beyond this much stretching (either way) it starts to sound stretched.
COMFORTABLE_STRETCH = 0.12
# How far either side of the measured tempo the beat grid is fitted.
GRID_SEARCH = 0.006
GRID_STEPS = 61
# The onset envelope runs ahead of the audio: a frame covers the N_FFT
# samples after its start, and an onset shows as soon as it enters them.
ENVELOPE_LATENCY = tempo.N_FFT / tempo.SAMPLE_RATE
BEATS_PER_BAR = 4

# Key: the spectrum is folded into 12 pitch classes between these frequencies
# (the bass up to the top of most melodies), in frames of CHROMA_FFT samples.
CHROMA_FFT, CHROMA_HOP = 4096, 1024
CHROMA_LOW, CHROMA_HIGH = 55.0, 2000.0
BASS_LOW, BASS_HIGH = 30.0, 150.0
BASS_NOTES_HIGH = 260.0  # the bass line's notes, for telling relative keys apart
RELATIVE_KEY_MARGIN = 0.05
# A shift has to fit this much better than none to be worth the artefacts,
# and each semitone moved costs a little, so the smallest good shift wins.
MIN_KEY_GAIN = 0.03
KEY_SHIFT_COST = 0.004
MAX_KEY_SHIFT = 6

# Levels: never boost or cut the vocals by more than this, and leave the
# peaks this far below full scale.
MAX_GAIN_DB = 12.0
PEAK_CEILING = 10 ** (-1.0 / 20)  # -1 dBFS

NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# Krumhansl and Kessler's key profiles: how strongly each note of the scale
# belongs to a major and a minor key, starting from the key's own note.
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text, limit=60):
    """``text`` made safe for a file or folder name, and not too long."""
    name = UNSAFE.sub("", " ".join(str(text).split())).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .-")
    return name or "Song"


# --------------------------------------------------------------------------
# Choosing the tempo
# --------------------------------------------------------------------------


def same_feel(bpm, target):
    """``bpm``, doubled or halved if that brings it closer to ``target``."""
    return min((bpm * factor for factor in (0.5, 1, 2)), key=lambda value: abs(math.log(target / value)))


def target_tempo(vocals_bpm, beat_bpm, mode="beat"):
    """The tempo the two meet at, given their tempos as counted (see plan)."""
    if mode == "halfway":
        return math.sqrt(vocals_bpm * beat_bpm)  # the same stretch, either way
    return beat_bpm


def plan(vocals_bpm, beat_bpm, mode="beat"):
    """Work out the shared tempo.

    Returns (target BPM, vocals' BPM as counted, beat's BPM as counted). The
    counted tempos may be doubled or halved to match the other song's feel;
    each song is stretched by target / counted.
    """
    vocals_bpm = same_feel(vocals_bpm, beat_bpm)
    return target_tempo(vocals_bpm, beat_bpm, mode), vocals_bpm, beat_bpm


def describe_stretch(ratio):
    """e.g. "8% faster", or "unchanged"."""
    change = ratio - 1
    if abs(change) < 0.005:
        return "unchanged"
    return f"{abs(change):.0%} {'faster' if change > 0 else 'slower'}"


# --------------------------------------------------------------------------
# Analysing a song
# --------------------------------------------------------------------------


@dataclass
class Analysis:
    bpm: float  # fine-tuned
    downbeat: float  # seconds: the "one" of the first bar once the music starts
    profile: np.ndarray  # how much of each pitch class, C first, summing to 1
    key: str  # e.g. "A minor", or None if the song has no clear key
    bass: np.ndarray = None  # the pitch classes of the bass alone, the same way


def analyse(path, bpm):
    """Fit the beat grid, find the downbeat and the key of an audio file."""
    audio = tempo._decode(path)
    if audio is None or not len(audio):
        raise DownloadError(f"Could not read {Path(path).name}.")
    bpm, first = _fit_grid(audio, bpm)
    chroma, bass, bass_chroma = _features(audio)
    shift = _downbeat_phase(chroma, bass, bpm, first)
    profile, bass_profile = (_normalised(values.sum(axis=0)) for values in (chroma, bass_chroma))
    return Analysis(bpm, first + shift * 60 / bpm, profile, key_name(profile, bass_profile), bass_profile)


def _normalised(profile):
    return profile / profile.sum() if profile.sum() > 0 else profile


def _music_start(audio):
    """When the music starts, in seconds: the first moment within 30 dB of the loudest."""
    frames = len(audio) // tempo.HOP
    if frames == 0:
        return 0.0
    rms = np.sqrt(np.mean(audio[: frames * tempo.HOP].reshape(frames, tempo.HOP) ** 2, axis=1))
    loud = np.nonzero(rms > rms.max() * 10 ** (-30 / 20))[0]
    return (int(loud[0]) if len(loud) else 0) * tempo.HOP / tempo.SAMPLE_RATE


def _fit_grid(audio, bpm):
    """Fit a steady beat grid. Returns (BPM, first beat in seconds).

    tempo.detect() is accurate to a fraction of a BPM, but over a whole song
    even 0.1 BPM drifts a beat apart by tens of milliseconds. So the tempo is
    fine-tuned here together with the grid's phase: of all the grids within
    GRID_SEARCH of ``bpm``, the one whose beats land on the most onsets over
    the whole song wins. The first beat is that grid's first one once the
    music has started.
    """
    fps = tempo.SAMPLE_RATE / tempo.HOP
    start = _music_start(audio)
    envelope = tempo.onset_envelope(audio)
    if len(envelope) < fps * 60 / bpm * 8:
        return bpm, start
    # Softened a little, so a beat a frame or two off still counts, but the
    # best fitting grid still stands out.
    envelope = np.convolve(envelope, [0.25, 0.5, 1.0, 0.5, 0.25], mode="same")

    best_score, best_bpm, best_phase = -1.0, bpm, 0.0
    for candidate in bpm * np.linspace(1 - GRID_SEARCH, 1 + GRID_SEARCH, GRID_STEPS):
        period = fps * 60 / candidate
        beats = np.arange(0, len(envelope) - period, period)
        phases = np.arange(0, period, 0.5)
        positions = np.round(phases[:, None] + beats[None, :]).astype(int)
        scores = envelope[np.minimum(positions, len(envelope) - 1)].mean(axis=1)
        index = int(np.argmax(scores))
        if scores[index] > best_score:
            best_score, best_bpm, best_phase = scores[index], candidate, phases[index]

    period = 60 / best_bpm
    first = best_phase / fps + ENVELOPE_LATENCY
    # The first beat of the grid at (or just before) the start of the music.
    beats_in = max(0, math.ceil((start - first) / period - 0.25))
    return float(best_bpm), float(first + beats_in * period)


@functools.lru_cache(maxsize=1)
def _chroma_bank():
    """Spectrum bins → pitch classes (all of them, and the bass's), and which bins are bass."""
    freqs = np.fft.rfftfreq(CHROMA_FFT, 1 / tempo.SAMPLE_RATE)
    bank = np.zeros((len(freqs), 12), np.float32)
    bass_bank = np.zeros((len(freqs), 12), np.float32)
    for index, freq in enumerate(freqs):
        if CHROMA_LOW <= freq <= CHROMA_HIGH:
            midi = 69 + 12 * math.log2(freq / 440)
            bank[index, int(round(midi)) % 12] = 1
            if freq <= BASS_NOTES_HIGH:
                bass_bank[index, int(round(midi)) % 12] = 1
    bass = ((freqs >= BASS_LOW) & (freqs <= BASS_HIGH)).astype(np.float32)
    return bank, bass_bank, bass


def _features(audio):
    """Per frame (CHROMA_HOP apart): a 12 note chroma, the bass energy, and the bass's chroma."""
    if len(audio) < CHROMA_FFT * 2:
        return np.zeros((0, 12), np.float32), np.zeros(0, np.float32), np.zeros((0, 12), np.float32)
    window = np.hanning(CHROMA_FFT).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, CHROMA_FFT)[::CHROMA_HOP]
    bank, bass_bank, bass_bins = _chroma_bank()
    chroma, bass, bass_chroma = [], [], []
    for start in range(0, len(frames), 1024):  # in chunks, to keep memory small
        spectrum = np.abs(np.fft.rfft(frames[start:start + 1024] * window, axis=1)).astype(np.float32)
        # Square root: loud notes still count most, without drowning the rest.
        root = np.sqrt(spectrum)
        chroma.append(root @ bank)
        bass_chroma.append(root @ bass_bank)
        bass.append((spectrum ** 2) @ bass_bins)
    return np.concatenate(chroma), np.concatenate(bass), np.concatenate(bass_chroma)


def _downbeat_phase(chroma, bass, bpm, first):
    """Which of the first BEATS_PER_BAR beats is the "one", counted from ``first``.

    Two things mark the start of a bar in most music: the chords change
    there, and the kick and bass land hardest there. Each beat is scored on
    both, and the position in the bar that scores highest over the whole
    song wins.
    """
    fps = tempo.SAMPLE_RATE / CHROMA_HOP
    offset = CHROMA_FFT / 2 / tempo.SAMPLE_RATE  # a frame's centre, after its start
    period = 60 / bpm
    count = int((len(chroma) / fps - first) / period) - 1
    if count < BEATS_PER_BAR * 4:
        return 0

    log_bass = np.log1p(bass / (bass.mean() + 1e-9) * 10)
    beat_chroma, onsets = [], []
    for k in range(count):
        a = int(round((first + k * period - offset) * fps))
        b = int(round((first + (k + 1) * period - offset) * fps))
        a, b = max(a, 0), max(b, a + 1)
        beat_chroma.append(chroma[a:b].mean(axis=0))
        # How much the bass jumps at the beat, against just before it.
        before = log_bass[max(a - 3, 0):max(a, 1)].mean()
        onsets.append(log_bass[a:a + 2].max() - before if a + 2 <= len(log_bass) else 0.0)
    beat_chroma = np.array(beat_chroma)
    norms = np.linalg.norm(beat_chroma, axis=1) + 1e-9
    similarity = (beat_chroma[1:] * beat_chroma[:-1]).sum(axis=1) / (norms[1:] * norms[:-1])
    change = np.concatenate([[np.nan], 1 - similarity])
    onsets = np.array(onsets)

    def standard(values):
        spread = np.nanstd(values)
        return (values - np.nanmean(values)) / spread if spread > 0 else np.zeros_like(values)

    score = standard(change) + standard(onsets)
    positions = np.arange(count) % BEATS_PER_BAR
    totals = [np.nanmean(score[positions == p]) for p in range(BEATS_PER_BAR)]
    return int(np.nanargmax(totals))


def key_name(profile, bass=None):
    """The key whose Krumhansl profile fits ``profile`` best, e.g. "A minor".

    A major key and its relative minor (C major, A minor) use the same notes,
    so their profiles can fit almost equally well. When they are within
    RELATIVE_KEY_MARGIN, the bass decides: songs keep coming back to their
    home note in the bass.
    """
    if not np.any(profile):
        return None
    fits = {}
    for root in range(12):
        for mode, shape in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            fits[root, mode] = np.corrcoef(profile, np.roll(shape, root))[0, 1]
    (root, mode), best = max(fits.items(), key=lambda item: item[1])
    if best <= 0.3:
        return None
    relative = ((root + 9) % 12, "minor") if mode == "major" else ((root + 3) % 12, "major")
    if bass is not None and np.any(bass) and best - fits[relative] < RELATIVE_KEY_MARGIN:
        if bass[relative[0]] > bass[root]:
            root, mode = relative
    return f"{NOTE_NAMES[root]} {mode}"


def key_shift(moving, fixed):
    """Semitones to move ``moving``'s notes by so they fit ``fixed``'s best (0 if none helps).

    The two songs' note profiles are compared directly, rather than their
    key names, so relative keys (C major and A minor share their notes) are
    left alone, and a song with a wobbly key estimate does not get moved on
    a guess.
    """
    if not np.any(moving) or not np.any(fixed):
        return 0

    def fit(shift):
        return np.corrcoef(np.roll(moving, shift), fixed)[0, 1]

    unmoved = fit(0)
    best, best_score = 0, unmoved
    for shift in range(-MAX_KEY_SHIFT + 1, MAX_KEY_SHIFT + 1):
        score = fit(shift) - KEY_SHIFT_COST * abs(shift)
        if score > best_score:
            best, best_score = shift, score
    return best if best_score - unmoved >= MIN_KEY_GAIN else 0


def shift_key(key, semitones):
    """``key`` moved by ``semitones``, e.g. ("A minor", 2) → "B minor"."""
    if not key or not semitones:
        return key
    root, mode = key.split(" ", 1)
    return f"{NOTE_NAMES[(NOTE_NAMES.index(root) + semitones) % 12]} {mode}"


def describe_shift(semitones):
    """e.g. "up 2 semitones"."""
    if not semitones:
        return "unchanged"
    count = abs(semitones)
    return f"{'up' if semitones > 0 else 'down'} {count} semitone{'' if count == 1 else 's'}"


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------


def levels(path):
    """(RMS, peak) of an audio file, from its loudest channel's samples."""
    command = [
        find_ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "2",
        "-ar", "44100", "-t", str(tempo.MAX_SECONDS), "-f", "f32le", "-",
    ]
    result = subprocess.run(command, capture_output=True, **_no_window())
    if result.returncode != 0 or not result.stdout:
        raise DownloadError(f"Could not read {Path(path).name}.")
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))), float(np.abs(samples).max())


def vocal_gain(own_instrumental, new_instrumental, vocals_peak):
    """dB to change the vocals by, so they sit over the new instrumental as over their own.

    The vocals keep the same level against the instrumental under them as
    they had in their own song. Kept within MAX_GAIN_DB, and never so loud
    that the peaks clip.
    """
    if own_instrumental <= 0 or new_instrumental <= 0:
        return 0.0
    gain = 20 * math.log10(new_instrumental / own_instrumental)
    gain = max(-MAX_GAIN_DB, min(MAX_GAIN_DB, gain))
    if vocals_peak > 0:
        gain = min(gain, 20 * math.log10(PEAK_CEILING / vocals_peak))
    return gain


# --------------------------------------------------------------------------
# Stretching
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def has_rubberband():
    """True when this ffmpeg was built with the rubberband filter."""
    try:
        result = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-filters"],
            capture_output=True, text=True, errors="replace", timeout=20, **_no_window(),
        )
    except (OSError, subprocess.SubprocessError, DownloadError):
        return False
    return any(line.split()[1:2] == ["rubberband"] for line in result.stdout.splitlines())


def _no_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _atempo_chain(ratio):
    """atempo filters for ``ratio``; each one is kept within 0.5-2 for older ffmpegs."""
    filters = []
    while ratio > 2.0:
        filters.append("atempo=2.0")
        ratio /= 2.0
    while ratio < 0.5:
        filters.append("atempo=0.5")
        ratio /= 0.5
    filters.append(f"atempo={ratio:.6f}")
    return filters


def stretch_filters(ratio, semitones=0, gain_db=0.0, delay=0.0, vocals=False, rubberband=None):
    """The ffmpeg filters for stretch(), as a list."""
    rubberband = has_rubberband() if rubberband is None else rubberband
    pitch = 2 ** (semitones / 12)
    filters = []
    if abs(ratio - 1) > 1e-4 or semitones:
        if rubberband:
            # Smooth transients suit singing; crisp keeps drums punchy. The
            # formants (the shape of a voice) stay put when the pitch moves,
            # so a singer moved up sounds like themselves, not a chipmunk.
            options = [f"tempo={ratio:.6f}", "pitchq=quality", f"transients={'smooth' if vocals else 'crisp'}"]
            if semitones:
                options.insert(1, f"pitch={pitch:.6f}")
                if vocals:
                    options.append("formant=preserved")
            filters.append("rubberband=" + ":".join(options))
        else:
            if semitones:
                # Playing it back faster raises the pitch, and speeds it up,
                # which the atempo below takes back out.
                filters += ["aresample=48000", f"asetrate={int(round(48000 * pitch))}", "aresample=48000"]
            filters += _atempo_chain(ratio / pitch)
    if abs(gain_db) > 0.05:
        filters.append(f"volume={gain_db:.2f}dB")
    if delay > 0.0005:
        filters.append(f"adelay=delays={delay * 1000:.1f}:all=1")
    return filters


def stretch(source, destination, ratio, delay=0.0, vocals=False, cancel_event=None, semitones=0, gain_db=0.0):
    """Save ``source`` sped up by ``ratio`` and moved by ``semitones``, after ``delay`` seconds of silence.

    ``gain_db`` changes its level. Saved as FLAC: lossless, for mixing, at
    about half the size of WAV.
    """
    filters = stretch_filters(ratio, semitones, gain_db, delay, vocals)
    command = [find_ffmpeg(), "-hide_banner", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0"]
    if filters:
        command += ["-af", ",".join(filters)]
    command += ["-c:a", "flac", str(destination)]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **_no_window()
    )
    while True:
        try:
            _, errors = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.communicate()
                Path(destination).unlink(missing_ok=True)
                raise Cancelled("Cancelled.")
    if process.returncode != 0:
        Path(destination).unlink(missing_ok=True)
        detail = errors.decode("utf-8", errors="replace").strip().splitlines()
        raise DownloadError(f"Could not stretch {Path(source).name}: {detail[-1] if detail else 'ffmpeg failed'}")
    return Path(destination)
