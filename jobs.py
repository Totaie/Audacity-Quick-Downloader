"""Queue downloads, run a few at once, and import the results into Audacity.

Worker threads update plain :class:`Job` objects; the interface simply reads
them a few times a second. That keeps the threads and the interface
decoupled, and means a flood of progress updates can never swamp the screen.
"""

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import audacity
from applemusicdownloader import download_apple_music
from sources import identify
from utils import Cancelled, DownloadError, Reporter, describe_path
from ytdlpdownloader import download_audio

WORKERS = 3

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = {DONE, FAILED, CANCELLED}


@dataclass
class Job:
    id: int
    text: str
    playlist: bool = False
    import_into_audacity: bool = True

    state: str = QUEUED
    stage: str = "Queued"
    title: str = None
    collection: str = None  # playlist or album name
    index: int = None
    total: int = None
    fraction: float = None
    speed: float = None
    eta: float = None
    files: list = field(default_factory=list)
    imported: int = 0
    message: str = None  # the result, or why it failed
    warning: str = None  # something went wrong, but not fatally
    started: float = None
    finished: float = None
    revision: int = 0

    def __post_init__(self):
        self.source = identify(self.text)
        self.cancel_event = threading.Event()

    def touch(self):
        self.revision += 1

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

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.cancel_event = job.cancel_event
        self.log_sink = None

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


class Runner:
    """Runs jobs on a small pool of worker threads.

    ``ask(question, default)`` is used for questions that come up mid-download
    and must be safe to call from a worker thread. ``options`` is the parsed
    command line; the interface may change it while the app is running.
    """

    def __init__(self, options, updater, ask=None, workers=WORKERS):
        self.options = options
        self.updater = updater
        self.ask = ask or (lambda question, default=False: default)
        self.jobs = []
        self.logs = queue.Queue()
        self._ids = itertools.count(1)
        self._pending = queue.Queue()
        self._audacity_lock = threading.Lock()
        self._import_lock = threading.Lock()
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

    def add(self, text, playlist=None):
        """Queue ``text`` (a link or a search) and return its Job."""
        if playlist is None:
            playlist = bool(self.options.playlist)
        job = Job(
            next(self._ids),
            text,
            playlist=playlist,
            import_into_audacity=not self.options.no_import,
        )
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

    def remove_finished(self):
        self.jobs = [job for job in self.jobs if not job.finished_state]

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

    def _finish(self, job, state, message=None):
        job.state = state
        job.message = message
        job.finished = time.time()
        job.fraction = 1.0 if state == DONE else job.fraction
        job.touch()
        level = {DONE: "success", FAILED: "error", CANCELLED: "warning"}[state]
        self.log(f"#{job.id} {job.label}: {message}", level)

    def _run(self, job):
        job.state = RUNNING
        job.started = time.time()
        job.touch()

        if self.updater.busy:
            self._set_stage(job, "Waiting for updates to finish")
            while not self.updater.wait(0.2):
                if job.cancel_event.is_set():
                    return self._finish(job, CANCELLED, "Cancelled.")

        # Audacity is started before downloading so it can warm up meanwhile.
        audacity_expected = False
        if job.import_into_audacity:
            audacity_expected = self._prepare_audacity()

        reporter = JobReporter(job)
        reporter.log_sink = self.log
        try:
            files = self._download(job, reporter)
        except Cancelled:
            return self._finish(job, CANCELLED, "Cancelled.")
        except DownloadError as error:
            return self._finish(job, FAILED, str(error))

        job.files = files
        where = describe_path(self.options.output)
        saved = f"Saved {len(files)} file{'s' if len(files) != 1 else ''} to {where}"

        if not job.import_into_audacity or job.cancel_event.is_set():
            return self._finish(job, DONE, saved)

        try:
            self._import(job, files, audacity.STARTUP_TIMEOUT if audacity_expected else 10)
        except audacity.AudacityError as error:
            job.warning = f"Not imported: {error}"
            return self._finish(job, DONE, f"{saved}, but not imported into Audacity.")
        self._finish(job, DONE, f"{saved} and imported into Audacity.")

    def _download(self, job, reporter):
        options = self.options
        source = job.source
        if source.kind == "apple":
            return download_apple_music(
                source.url,
                output_dir=options.output,
                cookies=options.cookies,
                reporter=reporter,
            )
        return download_audio(
            source.url,
            output_dir=options.output,
            # A search is technically a one-item playlist.
            playlist=job.playlist or source.kind == "search",
            quality=options.quality,
            cookies=options.site_cookies,
            thumbnail=not options.no_thumbnail,
            reporter=reporter,
        )

    def _prepare_audacity(self):
        """Start Audacity if needed. True if it should be on its way up."""
        with self._audacity_lock:
            try:
                return audacity.ensure_running(
                    auto_launch=not self.options.no_launch,
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
