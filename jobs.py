"""Queue downloads, run a few at once, separate them, and import the results.

Worker threads update plain :class:`Job` objects; the interface simply reads
them a few times a second. That keeps the threads and the interface
decoupled, and means a flood of progress updates can never swamp the screen.

Each job keeps its own copy of the settings from when it was queued, so
changing a setting affects new downloads without upsetting running ones.
"""

import copy
import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import audacity
import separation
from applemusicdownloader import DEFAULT_COOKIES, download_apple_music
from sources import identify
from utils import Cancelled, DownloadError, Reporter, describe_path
from ytdlpdownloader import download_audio

WORKERS = 3

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = {DONE, FAILED, CANCELLED}


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


@dataclass
class Job:
    id: int
    text: str
    settings: object  # a copy of settings.Settings
    playlist: bool = False

    state: str = QUEUED
    stage: str = "Queued"
    title: str = None
    collection: str = None  # playlist or album name
    index: int = None
    total: int = None
    step: str = None  # which separation model is running
    fraction: float = None
    speed: float = None
    eta: float = None
    files: list = field(default_factory=list)
    stems: list = field(default_factory=list)  # every stem file made
    imported: int = 0
    message: str = None  # the result, or why it failed
    warnings: list = field(default_factory=list)  # went wrong, but not fatally
    started: float = None
    finished: float = None
    revision: int = 0

    def __post_init__(self):
        self.source = identify(self.text)
        self.cancel_event = threading.Event()

    def touch(self):
        self.revision += 1

    @property
    def warning(self):
        return "\n".join(self.warnings) if self.warnings else None

    @property
    def import_into_audacity(self):
        s = self.settings
        return s.import_to_audacity and (s.import_original or (s.separate and s.import_stems))

    @property
    def label(self):
        """The best name we have for what is being downloaded."""
        if self.collection and self.total and self.total > 1:
            return self.collection
        if self.title:
            return self.title
        if self.source.kind == "search":
            return self.text
        return self.source.url

    @property
    def finished_state(self):
        return self.state in FINISHED


class JobReporter(Reporter):
    """Copies what the downloaders report onto a :class:`Job`."""

    def __init__(self, job, log_sink=None):
        super().__init__()
        self.job = job
        self.cancel_event = job.cancel_event
        self.log_sink = log_sink

    def stage(self, text):
        if self.job.stage != text:
            self.job.stage = text
            self.job.touch()

    def item(self, index, total, title, collection=None):
        job = self.job
        if (index, total, title) != (job.index, job.total, job.title) or (
            collection and collection != job.collection
        ):
            job.index, job.total = index, total
            job.title = title or job.title
            job.collection = collection or job.collection
            job.touch()

    def progress(self, fraction, speed=None, eta=None):
        job = self.job
        job.fraction, job.speed, job.eta = fraction, speed, eta
        job.touch()

    def log(self, text, level="info"):
        if self.log_sink:
            self.log_sink(f"#{self.job.id} {text}", level)


class SeparationReporter(JobReporter):
    """Shows which model is running without disturbing the track counter."""

    def item(self, index, total, title, collection=None):
        self.job.step = f"Model {index}/{total}: {title}" if total and total > 1 else f"Model: {title}"
        self.job.touch()


class Runner:
    """Runs jobs on a small pool of worker threads.

    ``ask(question, default)`` is used for questions that come up mid-download
    and must be safe to call from a worker thread. ``settings`` is the live
    Settings object; the interface may change it while the app is running.
    """

    def __init__(self, settings, updater, ask=None, workers=WORKERS):
        self.settings = settings
        self.updater = updater
        self.ask = ask or (lambda question, default=False: default)
        self.uvr = separation.find_uvr(settings.uvr_path)
        self.jobs = []
        self.logs = queue.Queue()
        self._ids = itertools.count(1)
        self._pending = queue.Queue()
        self._audacity_lock = threading.Lock()
        self._import_lock = threading.Lock()
        # Separating keeps a GPU (or every CPU core) busy, so one at a time.
        self._separation_lock = threading.Lock()
        self._closing = False
        self._threads = [
            threading.Thread(target=self._work, name=f"download-{n}", daemon=True)
            for n in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    # -- public API ---------------------------------------------------------

    def log(self, text, level="info"):
        self.logs.put((time.time(), level, text))

    def refresh_uvr(self):
        self.uvr = separation.find_uvr(self.settings.uvr_path)
        return self.uvr

    def add(self, text, playlist=None):
        """Queue ``text`` (a link or a search) and return its Job."""
        if playlist is None:
            playlist = bool(self.settings.playlist)
        job = Job(next(self._ids), text, copy.copy(self.settings), playlist=playlist)
        self.jobs.append(job)
        if not job.source.supported:
            self._finish(job, FAILED, job.source.problem)
        else:
            self._pending.put(job)
        return job

    def retry(self, job):
        return self.add(job.text, job.playlist)

    def cancel(self, job):
        if job.finished_state:
            return
        job.cancel_event.set()
        if job.state == QUEUED:
            self._finish(job, CANCELLED, "Cancelled before it started.")

    def active(self):
        return [job for job in self.jobs if not job.finished_state]

    def shutdown(self, timeout=3):
        """Cancel everything and give the workers a moment to tidy up."""
        self._closing = True
        for job in self.active():
            self.cancel(job)
        for _ in self._threads:
            self._pending.put(None)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))

    # -- the work -----------------------------------------------------------

    def _work(self):
        while True:
            job = self._pending.get()
            if job is None or self._closing:
                return
            if job.finished_state:  # cancelled while it was queued
                continue
            try:
                self._run(job)
            except Exception as error:  # a bug should fail one job, not the app
                self._finish(job, FAILED, f"Unexpected error: {error!r}")

    def _set_stage(self, job, stage):
        job.stage = stage
        job.fraction = None
        job.touch()

    def _warn(self, job, text):
        job.warnings.append(text)
        job.touch()
        self.log(f"#{job.id} {text}", "warning")

    def _finish(self, job, state, message=None):
        job.state = state
        job.message = message
        job.finished = time.time()
        job.fraction = 1.0 if state == DONE else job.fraction
        job.touch()
        level = {DONE: "success", FAILED: "error", CANCELLED: "warning"}[state]
        self.log(f"#{job.id} {job.label}: {message}", level)

    def _run(self, job):
        settings = job.settings
        job.state = RUNNING
        job.started = time.time()
        job.touch()

        if self.updater.busy:
            self._set_stage(job, "Waiting for updates to finish")
            while not self.updater.wait(0.2):
                if job.cancel_event.is_set():
                    return self._finish(job, CANCELLED, "Cancelled.")

        # Without Audacity this is just a downloader; say so once, not after a wait.
        if job.import_into_audacity and audacity.find_audacity() is None:
            self._warn(job, "Not imported: Audacity is not installed (see the README to set it up).")
            settings.import_to_audacity = False

        # Audacity is started before downloading so it can warm up meanwhile.
        audacity_expected = False
        if job.import_into_audacity:
            audacity_expected = self._prepare_audacity(settings)

        try:
            files = self._download(job)
        except Cancelled:
            return self._finish(job, CANCELLED, "Cancelled.")
        except DownloadError as error:
            return self._finish(job, FAILED, str(error))

        job.files = files
        summary = [f"Saved {plural(len(files), 'file')} to {describe_path(settings.downloads)}"]

        stems_by_song = {}
        if settings.separate:
            try:
                stems_by_song = self._separate(job, files)
            except Cancelled:
                return self._finish(job, CANCELLED, "Cancelled while separating; the downloads were kept.")
            if job.stems:
                summary.append(
                    f"split into {plural(len(job.stems), 'stem')} in {describe_path(settings.separated)}"
                )

        to_import = []
        for song in files:
            if settings.import_original:
                to_import.append(song)
            if settings.import_stems:
                to_import += stems_by_song.get(song, [])

        if not settings.import_to_audacity or not to_import or job.cancel_event.is_set():
            return self._finish(job, DONE, ", ".join(summary) + ".")

        try:
            self._import(job, to_import, audacity.STARTUP_TIMEOUT if audacity_expected else 10)
        except audacity.AudacityError as error:
            self._warn(job, f"Not imported: {error}")
            return self._finish(job, DONE, ", ".join(summary) + ", but not imported into Audacity.")
        summary.append(f"imported {plural(len(to_import), 'track')} into Audacity")
        self._finish(job, DONE, ", ".join(summary[:-1]) + " and " + summary[-1] + ".")

    def _download(self, job):
        settings = job.settings
        source = job.source
        reporter = JobReporter(job, self.log)
        if source.kind == "apple":
            return download_apple_music(
                source.url,
                output_dir=settings.downloads,
                cookies=settings.apple_cookies or DEFAULT_COOKIES,
                reporter=reporter,
                audio_format=settings.audio_format,
                bitrate=settings.bitrate,
            )
        return download_audio(
            source.url,
            output_dir=settings.downloads,
            # A search is technically a one-item playlist.
            playlist=job.playlist or source.kind == "search",
            quality=settings.bitrate,
            cookies=settings.site_cookies or None,
            thumbnail=settings.embed_thumbnail,
            reporter=reporter,
            audio_format=settings.audio_format,
        )

    def _separate(self, job, files):
        """Split each downloaded file into stems. Returns {song: [stem paths]}.

        Problems here are warnings rather than failures: the download itself
        worked and is kept either way.
        """
        settings = job.settings
        uvr = self.uvr
        if uvr is None:
            self._warn(job, "Not separated: Ultimate Vocal Remover 5 was not found. See the README.")
            return {}
        if separation.engine_info() is None:
            self._warn(job, "Not separated: set up the separation engine in Settings (Ctrl+S) first.")
            return {}

        preset = separation.PRESETS.get(settings.separation_preset)
        quality = separation.resolve_quality(
            settings.separation_quality, (separation.engine_info() or {}).get("device", "cpu")
        )
        order = preset.outputs(quality) if preset else []
        results = {}
        for number, song in enumerate(files, 1):
            if job.cancel_event.is_set():
                raise Cancelled("Cancelled.")
            if len(files) > 1:
                job.index, job.total, job.title = number, len(files), song.stem

            if self._separation_lock.locked():
                self._set_stage(job, "Waiting to separate")
            with self._separation_lock:
                job.step = None
                self._set_stage(job, "Separating")
                try:
                    stems = separation.separate(
                        song,
                        settings.separated,
                        preset=settings.separation_preset,
                        audio_format=settings.audio_format,
                        bitrate=settings.bitrate,
                        cover_art=settings.embed_thumbnail,
                        quality=settings.separation_quality,
                        device=settings.separation_device,
                        uvr=uvr,
                        reporter=SeparationReporter(job, self.log),
                    )
                except separation.SeparationError as error:
                    self._warn(job, f"{song.stem}: {error}")
                    continue
                finally:
                    job.step = None
            ordered = [stems[name] for name in order if name in stems]
            ordered += [path for name, path in stems.items() if name not in order]
            results[song] = ordered
            job.stems += ordered
        return results

    def _prepare_audacity(self, settings):
        """Start Audacity if needed. True if it should be on its way up."""
        with self._audacity_lock:
            try:
                return audacity.ensure_running(
                    auto_launch=settings.launch_audacity,
                    ask=lambda question: self.ask(question, True),
                    log=self.log,
                )
            except audacity.AudacityError as error:
                self.log(f"{error} Carrying on with the download anyway.", "warning")
                return False

    def _import(self, job, files, timeout):
        # Audacity has one scripting connection, so imports take turns.
        self._set_stage(job, "Waiting to import" if self._import_lock.locked() else "Importing")
        with self._import_lock:
            self._set_stage(job, "Importing into Audacity")

            def waiting():
                self._set_stage(job, "Waiting for Audacity to start")

            with audacity.connect(timeout, on_wait=waiting) as session:
                self._set_stage(job, "Importing into Audacity")
                for number, path in enumerate(files, 1):
                    session.import_file(Path(path))
                    job.imported = number
                    job.fraction = number / len(files)
                    job.touch()
