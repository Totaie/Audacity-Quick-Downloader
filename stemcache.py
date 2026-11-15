"""Remember separations, so the same song is never put through a model twice.

Separating is by far the slowest thing the app does: a minute or more a song
on a GPU, much longer on a CPU. But the same song often comes round again: a
mashup tried with a different partner, the Downloads tab after the Mashup
tab, a voice dataset from songs already split into stems. So every model's
output is kept here, and the next time that model would run on that song,
its saved output is used instead.

Songs are recognised by how they sound, not by their file: the Downloads
tab saves MP3s, the Mashup tab FLACs and the BPM tag changes the file's
bytes, but they are still the same song. Each song gets an audio fingerprint
(the classic Haitsma-Kalker kind: in each 46 ms frame, whether the energy
difference between neighbouring frequency bands rose or fell, as 32 bits).
Re-encoding the same recording barely changes those bits; a different
recording, or a different version of the song, changes about half of them.
Matches also have to line up in time and be the same length, so stems are
only reused where they fit the new file exactly.

What is cached is each model's raw output, keyed by the chain of models
that led to it, so presets that start the same way share the work: the
vocals the Mashup tab separated are the first step of a voice dataset too.

The cache lives next to the models (in the user's local app data) and is
kept under a size limit, dropping the songs used least recently first.
"""

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from utils import find_ffmpeg

RATE = 11025
FRAME, HOP = 2048, 512  # 46 ms frames
BAND_LOW, BAND_HIGH, BANDS = 300.0, 2000.0, 33  # 33 bands → 32 bits a frame
MAX_SECONDS = 900
# Same recording, re-encoded: a few percent of bits differ. Different songs
# (or versions): about half. Well clear of both.
MATCH_BER = 0.15
LENGTH_SLACK = 0.3  # seconds: encoders can pad a little
OFFSETS = range(-3, 4)  # frames either way, to tell "the same, but shifted" apart

LIMITS_GB = {2: "2 GB", 5: "5 GB", 10: "10 GB", 20: "20 GB", 50: "50 GB"}


def default_dir():
    """Next to the models, in the user's local app data (see separation._model_cache)."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "AudacityQuickDownloader" / "stems"


def _no_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _bands():
    freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
    edges = np.geomspace(BAND_LOW, BAND_HIGH, BANDS + 1)
    bank = np.zeros((len(freqs), BANDS), np.float32)
    for band in range(BANDS):
        bank[(freqs >= edges[band]) & (freqs < edges[band + 1]), band] = 1
    return bank


def fingerprint(path):
    """(fingerprint, length in seconds) of an audio file, or (None, None) if it cannot be read.

    The fingerprint is one 32 bit number per frame.
    """
    command = [
        find_ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
        "-ar", str(RATE), "-t", str(MAX_SECONDS), "-f", "f32le", "-",
    ]
    result = subprocess.run(command, capture_output=True, **_no_window())
    if result.returncode != 0 or not result.stdout:
        return None, None
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    seconds = len(audio) / RATE
    if len(audio) < FRAME * 4:
        return None, None
    window = np.hanning(FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, FRAME)[::HOP]
    energy = []
    for start in range(0, len(frames), 2048):
        spectrum = np.abs(np.fft.rfft(frames[start:start + 2048] * window, axis=1)) ** 2
        energy.append(spectrum.astype(np.float32) @ _bands())
    energy = np.concatenate(energy)
    across = energy[:, :-1] - energy[:, 1:]  # neighbouring bands
    bits = (across[1:] - across[:-1]) > 0  # and how that changed since the last frame
    weights = (1 << np.arange(32, dtype=np.uint64)).astype(np.uint64)
    return (bits.astype(np.uint64) @ weights).astype(np.uint32), seconds


def bit_error_rate(a, b, offset=0):
    """The share of bits that differ between two fingerprints, ``b`` shifted by ``offset`` frames."""
    if offset > 0:
        a = a[offset:]
    elif offset < 0:
        b = b[-offset:]
    length = min(len(a), len(b))
    if length < 100:
        return 1.0
    differ = np.bitwise_xor(a[:length], b[:length])
    return float(np.unpackbits(differ.view(np.uint8)).sum()) / (32 * length)


def same_recording(a, b):
    """True when two fingerprints are the same recording, lined up in time."""
    rates = {offset: bit_error_rate(a, b, offset) for offset in OFFSETS}
    best = min(rates, key=rates.get)
    return best == 0 and rates[0] < MATCH_BER


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


@dataclass
class Song:
    """A song in the cache (or about to be)."""

    id: str
    folder: Path
    seconds: float
    fingerprint: np.ndarray
    known: bool  # True if it was already in the cache
    name: str = ""


def step_key(source_key, model, overlap):
    """The key for a model run on an input that itself has ``source_key``."""
    text = json.dumps([source_key, model, overlap])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _file_name(key, model_stem):
    return f"{key}-{re.sub(r'[^A-Za-z0-9]+', '_', model_stem)}.flac"


class StemCache:
    def __init__(self, folder=None, limit_gb=10):
        self.folder = Path(folder) if folder else default_dir()
        self.limit_gb = limit_gb
        self._lock = threading.Lock()

    @property
    def _index_path(self):
        return self.folder / "index.json"

    def _read_index(self):
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            return data if isinstance(data.get("songs"), dict) else {"songs": {}}
        except (OSError, ValueError, AttributeError):
            return {"songs": {}}

    def _write_index(self, index):
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self._index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(index, indent=1), encoding="utf-8")
        os.replace(temporary, self._index_path)

    # -- looking songs up -----------------------------------------------------

    def identify(self, path):
        """The cached song that sounds like ``path``, or a new one. None if it cannot be read."""
        print_, seconds = fingerprint(path)
        if print_ is None:
            return None
        with self._lock:
            songs = self._read_index()["songs"]
        for song_id, info in songs.items():
            if abs(info.get("seconds", -1e9) - seconds) > LENGTH_SLACK:
                continue
            try:
                known = np.load(self.folder / song_id / "fingerprint.npy")
            except (OSError, ValueError):
                continue
            if same_recording(print_, known):
                return Song(song_id, self.folder / song_id, seconds, print_, True, info.get("name", ""))
        song_id = uuid.uuid4().hex[:12]
        return Song(song_id, self.folder / song_id, seconds, print_, False, Path(path).stem)

    def find(self, song, key, model_stems):
        """{model stem: saved file} for a model's run on ``song``, or None if it is not all there."""
        if not song.known:
            return None
        files = {stem: song.folder / _file_name(key, stem) for stem in model_stems}
        return files if all(path.exists() for path in files.values()) else None

    def targets(self, song, key, model_stems):
        """Where to save a model's output for ``song``: {model stem: path}."""
        song.folder.mkdir(parents=True, exist_ok=True)
        return {stem: song.folder / _file_name(key, stem) for stem in model_stems}

    # -- remembering and forgetting -------------------------------------------

    def commit(self, song):
        """Record that ``song`` was used now (and, if new, that it is in the cache)."""
        with self._lock:
            index = self._read_index()
            info = index["songs"].get(song.id)
            if info is None:
                if not song.folder.is_dir() or not any(song.folder.glob("*.flac")):
                    return  # nothing was saved for it
                np.save(song.folder / "fingerprint.npy", song.fingerprint)
                info = index["songs"][song.id] = {
                    "seconds": round(song.seconds, 3), "name": song.name, "created": time.time(),
                }
            info["used"] = time.time()
            song.known = True
            self._write_index(index)
            self._evict(index)

    def size(self):
        """How much the cache takes up, in bytes."""
        if not self.folder.is_dir():
            return 0
        return sum(path.stat().st_size for path in self.folder.rglob("*") if path.is_file())

    def count(self):
        with self._lock:
            return len(self._read_index()["songs"])

    def clear(self):
        with self._lock:
            shutil.rmtree(self.folder, ignore_errors=True)

    def _evict(self, index):
        """Drop folders nothing points to, then the least recently used songs over the limit."""
        songs = index["songs"]
        for folder in self.folder.iterdir():
            if folder.is_dir() and folder.name not in songs:
                # Left behind by a separation that failed or was cancelled,
                # unless one is writing to it right now.
                if time.time() - folder.stat().st_mtime > 6 * 3600:
                    shutil.rmtree(folder, ignore_errors=True)
        if not self.limit_gb:
            return
        sizes = {
            song_id: sum(p.stat().st_size for p in (self.folder / song_id).glob("*") if p.is_file())
            for song_id in songs
        }
        total, limit = sum(sizes.values()), self.limit_gb * 1024 ** 3
        changed = False
        for song_id in sorted(songs, key=lambda s: songs[s].get("used", 0)):
            if total <= limit or len(songs) <= 1:
                break
            shutil.rmtree(self.folder / song_id, ignore_errors=True)
            total -= sizes.get(song_id, 0)
            del songs[song_id]
            changed = True
        if changed:
            self._write_index(index)


def describe_size(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    return f"{size / 1024 ** 2:.0f} MB"
