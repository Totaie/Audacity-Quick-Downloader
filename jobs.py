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
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import audacity
import separation
import tempo
import voice
from applemusicdownloader import DEFAULT_COOKIES, download_apple_music
from sources import identify, local_audio
from utils import Cancelled, DownloadError, Reporter, describe_path, temp_workspace, unique_path
from ytdlpdownloader import download_audio, download_video

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
    voice: str = None  # the artist, for the Voice tab: keep only their vocals
    video: bool = False  # from the Videos tab: save .mp4 (and .mp3)

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
    # The Audacity window its first track went into: the rest follow it there,
    # even if the user has moved to another window since.
    audacity_window: int = None
    bpms: dict = field(default_factory=dict)  # {file: tempo} for each song with a steady beat
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
    def bpm_text(self):
        """The tempo, or the range of tempos for a playlist, e.g. "128 BPM"."""
        return tempo.describe(self.bpms.values())

    @property
    def import_into_audacity(self):
        s = self.settings
        return not self.voice and not self.video and s.import_to_audacity and (s.import_original or (s.separate and s.import_stems))

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

    def add(self, text, playlist=None, voice=None, video=False):
        """Queue ``text`` (a link or a search) and return its Job.

        With ``voice`` (an artist's name) only the vocals are kept, in that
        artist's voice dataset folder. With ``video`` it is saved as a video.
        """
        if playlist is None:
            playlist = bool(self.settings.playlist)
        job = Job(next(self._ids), text, copy.copy(self.settings), playlist=playlist, voice=voice, video=video)
        self.jobs.append(job)
        if not job.source.supported:
            self._finish(job, FAILED, job.source.problem)
        else:
            self._pending.put(job)
        return job

    def retry(self, job):
        return self.add(job.text, job.playlist, job.voice, job.video)

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

        if job.voice:
            return self._run_voice(job)
        if job.video:
            return self._run_video(job)

        # Without Audacity this is just a downloader; say so once, not after a wait.
        # A running Audacity counts: its scripting pipes are enough to import
        # into, even when the executable is not where the app looks for it.
        if (
            job.import_into_audacity
            and audacity.find_audacity() is None
            and not audacity.is_running()
        ):
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
        if job.source.kind == "local":
            summary = [f"Found {plural(len(files), 'audio file')}"]
        else:
            summary = [f"Saved {plural(len(files), 'file')} to {describe_path(settings.downloads)}"]
        importing = settings.import_to_audacity
        timeout = audacity.STARTUP_TIMEOUT if audacity_expected else 10
        imported = 0
        import_failed = False

        # The song goes into Audacity straight away, so it can be worked on
        # while the (slower) separation runs; the stems follow when ready.
        if importing and settings.import_original and not job.cancel_event.is_set():
            try:
                self._import(job, files, timeout)
                imported += len(files)
            except audacity.AudacityError as error:
                self._warn(job, f"Not imported: {error}")
                import_failed = True

        if settings.detect_bpm and not job.cancel_event.is_set():
            # Before separating, so the stems carry the tag too. Files that
            # were already on this PC are measured but left untouched.
            downloaded = job.source.kind != "local"
            self._detect_bpm(job, [(path, [path] if downloaded else []) for path in files])

        if settings.separate:
            def import_stems(stems):
                # Stems go in as soon as each model has made them, while
                # the next model is still running.
                nonlocal imported, import_failed
                if not (importing and settings.import_stems and stems) or import_failed:
                    return
                try:
                    self._import(job, stems, timeout, quietly=True)
                    imported += len(stems)
                except audacity.AudacityError as error:
                    self._warn(job, f"Stems not imported: {error}")
                    import_failed = True

            try:
                self._separate(job, files, on_stems=import_stems)
            except Cancelled:
                return self._finish(job, CANCELLED, "Cancelled while separating; the songs were kept.")
            if job.stems:
                summary.append(
                    f"split into {plural(len(job.stems), 'stem')} in {describe_path(settings.separated)}"
                )

        if imported:
            summary.append(f"imported {plural(imported, 'track')} into Audacity")
        text = ", ".join(summary[:-1]) + " and " + summary[-1] if len(summary) > 1 else summary[0]
        if import_failed:
            text += ", but not everything was imported into Audacity"
        self._finish(job, DONE, text + ".")

    def _download(self, job, output_dir=None, audio_format=None, thumbnail=None, skip=None):
        """Download the job's link. The arguments override the job's settings."""
        settings = job.settings
        output_dir = output_dir or settings.downloads
        audio_format = audio_format or settings.audio_format
        thumbnail = settings.embed_thumbnail if thumbnail is None else thumbnail
        source = job.source
        if source.kind == "local":
            # Nothing to download: use the files where they are.
            files = local_audio(source.url)
            if not files:
                raise DownloadError("There are no audio files there.")
            if len(files) > 1:
                job.collection, job.total = Path(source.url).name, len(files)
            else:
                job.title = files[0].stem
            return files
        reporter = JobReporter(job, self.log)
        if source.kind == "apple":
            return download_apple_music(
                source.url,
                output_dir=output_dir,
                cookies=settings.apple_cookies or DEFAULT_COOKIES,
                reporter=reporter,
                audio_format=audio_format,
                bitrate=settings.bitrate,
            )
        return download_audio(
            source.url,
            output_dir=output_dir,
            # A search is technically a one-item playlist.
            playlist=job.playlist or source.kind == "search",
            quality=settings.bitrate,
            cookies=settings.site_cookies or None,
            thumbnail=thumbnail,
            reporter=reporter,
            audio_format=audio_format,
            skip=skip,
        )

    def _run_video(self, job):
        """The Videos tab: save .mp4 videos and, if wanted, their sound as MP3s."""
        settings = job.settings
        source = job.source
        if source.kind == "apple":
            return self._finish(job, FAILED, "Apple Music has no videos to download. Use the Downloads tab for its songs.")
        if source.kind == "local":
            return self._finish(job, FAILED, "That is already on this PC, so there is nothing to download.")
        try:
            pairs = download_video(
                source.url,
                output_dir=settings.videos,
                # A search is technically a one-item playlist.
                playlist=job.playlist or source.kind == "search",
                quality=settings.video_quality,
                cookies=settings.site_cookies or None,
                thumbnail=settings.embed_thumbnail,
                reporter=JobReporter(job, self.log),
                mp3=settings.video_mp3,
                bitrate=settings.bitrate,
            )
        except Cancelled:
            return self._finish(job, CANCELLED, "Cancelled.")
        except DownloadError as error:
            return self._finish(job, FAILED, str(error))

        job.files = [path for pair in pairs for path in pair if path]
        if settings.detect_bpm:
            # Measured on the MP3 when there is one, and saved in both files.
            self._detect_bpm(job, [(audio or video, [p for p in (video, audio) if p]) for video, audio in pairs])
        text = f"Saved {plural(len(pairs), 'video')}"
        if settings.video_mp3:
            text += " and its MP3" if len(pairs) == 1 else " and their MP3s"
        text += f" to {describe_path(settings.videos)}."
        if job.cancel_event.is_set():
            return self._finish(job, CANCELLED, f"Cancelled while finding the BPM. {text}")
        self._finish(job, DONE, text)

    def _detect_bpm(self, job, songs):
        """Find each song's tempo, save it in the files' tags, and keep it for the card.

        ``songs`` is [(file to listen to, [files to tag]), ...]. A tempo is a
        bonus: nothing here can fail the download.
        """
        for number, (source, targets) in enumerate(songs, 1):
            if job.cancel_event.is_set():
                return
            name = Path(source).stem
            if len(songs) > 1:
                job.index, job.total, job.title = number, len(songs), name
            self._set_stage(job, "Finding the BPM")
            try:
                bpm = tempo.detect(source)
            except Exception as error:
                self.log(f"#{job.id} No BPM for {name}: {error}", "warning")
                continue
            if bpm is None:
                self.log(f"#{job.id} {name}: no steady beat, so no BPM")
                continue
            job.bpms[str(source)] = bpm
            job.touch()
            for target in targets:
                try:
                    tempo.save(target, bpm)
                except Exception as error:
                    self._warn(job, f"Could not save the BPM in {Path(target).name}: {error}")
            self.log(f"#{job.id} {round(bpm)} BPM  {name}", "success")

    def _run_voice(self, job):
        """The Voice tab: keep only the artist's dry vocals, in their folder.

        Songs are downloaded losslessly into a scratch folder inside the
        artist's, separated one by one, and deleted once their vocals are
        saved. Songs already in the folder are skipped, so the same playlist
        can be run again after it grows.
        """
        settings = job.settings
        if not voice.folder_name(job.voice):
            return self._finish(job, FAILED, "The artist's name has nothing in it that can name a folder.")
        folder = settings.voices / voice.folder_name(job.voice)
        if separation.engine_info() is None:
            return self._finish(job, FAILED, "Set up the separation engine in Settings (Ctrl+S) first.")
        folder.mkdir(parents=True, exist_ok=True)

        known = voice.existing_keys(folder)
        skipped = set()

        def already_done(title):
            key = voice.match_key(title)
            if key not in known:
                return False
            if key not in skipped:
                skipped.add(key)
                self.log(f"#{job.id} Skipped {title}: it is already in {folder.name}'s folder")
            return True

        steps = voice.preset(settings.voice_lead_only, settings.voice_reverb)
        saved, seconds = [], 0.0
        with temp_workspace(folder) as work:
            try:
                # FLAC: lossless, at half the size of WAV for a long playlist.
                files = self._download(job, output_dir=work, audio_format="flac", thumbnail=False, skip=already_done)
            except Cancelled:
                return self._finish(job, CANCELLED, "Cancelled.")
            except DownloadError as error:
                if not skipped:  # rather than "every song was skipped"
                    return self._finish(job, FAILED, str(error))
                files = []

            for number, song in enumerate(files, 1):
                if job.cancel_event.is_set():
                    break
                if len(files) > 1:
                    job.index, job.total, job.title = number, len(files), song.stem
                if self._separation_lock.locked():
                    self._set_stage(job, "Waiting to separate")
                with self._separation_lock:
                    # Checked again here: another job may have just done this
                    # song, and Apple Music and local files are not filtered
                    # before they are downloaded.
                    known = voice.existing_keys(folder)
                    if already_done(song.stem):
                        continue
                    job.step = None
                    self._set_stage(job, "Separating")
                    try:
                        stems = separation.separate(
                            song,
                            work,
                            preset=steps,
                            audio_format="wav",
                            cover_art=False,
                            quality=settings.separation_quality,
                            device=settings.separation_device,
                            uvr=self.uvr,
                            reporter=SeparationReporter(job, self.log),
                        )
                    except Cancelled:
                        break
                    except separation.SeparationError as error:
                        self._warn(job, f"{song.stem}: {error}")
                        continue
                    finally:
                        job.step = None

                self._set_stage(job, "Saving vocals")
                vocals = stems["Vocals"]
                destination = unique_path(folder / f"{song.stem}.wav")
                try:
                    seconds += voice.finish(vocals, destination, settings.voice_trim_silence)
                except DownloadError as error:
                    self._warn(job, f"{song.stem}: {error}")
                    continue
                finally:
                    # The separation's own folder, and the song if it was
                    # downloaded (local files are left where they are).
                    shutil.rmtree(Path(vocals).parent, ignore_errors=True)
                    if work in song.parents:
                        song.unlink(missing_ok=True)
                saved.append(destination)
                job.stems.append(destination)
                job.touch()

        count, total = voice.stats(folder)
        summary = []
        if saved:
            summary.append(
                f"Saved {voice.format_minutes(seconds)} of vocals from {plural(len(saved), 'song')} "
                f"to {describe_path(folder)}"
            )
        if skipped:
            summary.append(f"{plural(len(skipped), 'song')} {'was' if len(skipped) == 1 else 'were'} already there")
        if saved or skipped:
            summary.append(f"{job.voice} has {voice.format_minutes(total)} in {plural(count, 'song')} now")
        text = "; ".join(summary) + "." if summary else ""
        if job.cancel_event.is_set():
            return self._finish(job, CANCELLED, f"Cancelled. {text}".strip())
        if not summary:
            return self._finish(job, FAILED, "No vocals were saved.")
        self._finish(job, DONE, text)

    def _separate(self, job, files, on_stems=None):
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
                        on_stems=lambda batch: on_stems and on_stems(list(batch.values())),
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

    def _import(self, job, files, timeout, quietly=False):
        """Import ``files`` into Audacity.

        ``quietly`` leaves the job's status and progress alone, for stems
        imported while a separation is still showing its own progress.
        """
        stage = (lambda text: None) if quietly else (lambda text: self._set_stage(job, text))
        # Audacity has one scripting connection, so imports take turns.
        stage("Waiting to import" if self._import_lock.locked() else "Importing")
        with self._import_lock:
            stage("Importing into Audacity")
            with audacity.connect(timeout, on_wait=lambda: stage("Waiting for Audacity to start")) as session:
                stage("Importing into Audacity")
                for number, path in enumerate(files, 1):
                    job.audacity_window = session.import_file(Path(path), window=job.audacity_window)
                    job.imported += 1
                    if not quietly:
                        job.fraction = number / len(files)
                    job.touch()
        if quietly:
            self.log(f"#{job.id} Imported {', '.join(Path(f).stem for f in files)}", "success")
