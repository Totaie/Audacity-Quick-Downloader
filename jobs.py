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
import mashup
import separation
import stemcache
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
    mashup: str = None  # from the Mashup tab: the second song (text is the first)

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
    titles: list = field(default_factory=lambda: [None, None])  # a mashup's two songs
    target_bpm: float = None  # the tempo a mashup's songs were brought to
    key_shift: int = None  # semitones a mashup's vocals song was moved by
    message: str = None  # the result, or why it failed
    warnings: list = field(default_factory=list)  # went wrong, but not fatally
    started: float = None
    finished: float = None
    revision: int = 0

    def __post_init__(self):
        self.source = identify(self.text)
        self.mashup_source = identify(self.mashup) if self.mashup else None
        self.cancel_event = threading.Event()

    def touch(self):
        self.revision += 1

    @property
    def warning(self):
        return "\n".join(self.warnings) if self.warnings else None

    @property
    def bpm_text(self):
        """The tempo, or the range of tempos for a playlist, e.g. "128 BPM"."""
        if self.mashup and self.target_bpm:
            before = " & ".join(str(int(round(bpm))) for bpm in self.bpms.values())
            text = f"{before} → {int(round(self.target_bpm))} BPM"
            if self.key_shift:
                text += f"  {self.key_shift:+d} st"
            return text
        return tempo.describe(self.bpms.values())

    @property
    def import_into_audacity(self):
        s = self.settings
        return not self.voice and not self.video and not self.mashup and s.import_to_audacity and (s.import_original or (s.separate and s.import_stems))

    @property
    def label(self):
        """The best name we have for what is being downloaded."""
        if self.mashup:
            names = [
                title or (source.url if source.kind != "search" else text)
                for title, source, text in zip(self.titles, (self.source, self.mashup_source), (self.text, self.mashup))
            ]
            return " × ".join(names)
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


class MashupReporter(JobReporter):
    """Shows which of a mashup's two songs is downloading."""

    def __init__(self, job, number, log_sink=None):
        super().__init__(job, log_sink)
        self.number = number

    def item(self, index, total, title, collection=None):
        job = self.job
        job.index, job.total = self.number, 2
        job.title = title or job.title
        job.touch()


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
        self.stem_cache = stemcache.StemCache(limit_gb=settings.stem_cache_gb)
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
        self.stem_cache.limit_gb = self.settings.stem_cache_gb
        return self.uvr

    def _cache(self, settings):
        """The stem cache, if this job's settings say to use it."""
        return self.stem_cache if settings.reuse_separations else None

    def add(self, text, playlist=None, voice=None, video=False, mashup=None):
        """Queue ``text`` (a link or a search) and return its Job.

        With ``voice`` (an artist's name) only the vocals are kept, in that
        artist's voice dataset folder. With ``video`` it is saved as a video.
        With ``mashup`` (a second link or search) the two are made into a
        mashup.
        """
        if playlist is None:
            playlist = bool(self.settings.playlist)
        if mashup:
            playlist = False  # one song from each
        job = Job(
            next(self._ids), text, copy.copy(self.settings),
            playlist=playlist, voice=voice, video=video, mashup=mashup,
        )
        self.jobs.append(job)
        if not job.source.supported:
            self._finish(job, FAILED, f"Song 1: {job.source.problem}" if mashup else job.source.problem)
        elif mashup and not job.mashup_source.supported:
            self._finish(job, FAILED, f"Song 2: {job.mashup_source.problem}")
        else:
            self._pending.put(job)
        return job

    def retry(self, job):
        return self.add(job.text, job.playlist, job.voice, job.video, job.mashup)

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
        if job.mashup:
            return self._run_mashup(job)

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

    def _download(self, job, output_dir=None, audio_format=None, thumbnail=None, skip=None, source=None, reporter=None):
        """Download the job's link (or ``source``). The arguments override the job's settings."""
        settings = job.settings
        output_dir = output_dir or settings.downloads
        audio_format = audio_format or settings.audio_format
        thumbnail = settings.embed_thumbnail if thumbnail is None else thumbnail
        source = source or job.source
        if source.kind == "local":
            # Nothing to download: use the files where they are.
            files = local_audio(source.url)
            if not files:
                raise DownloadError("There are no audio files there.")
            if job.mashup:
                pass  # the mashup sorts out its own titles
            elif len(files) > 1:
                job.collection, job.total = Path(source.url).name, len(files)
            else:
                job.title = files[0].stem
            return files
        reporter = reporter or JobReporter(job, self.log)
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
                            cache=self._cache(settings),
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

    def _run_mashup(self, job):
        """The Mashup tab: two songs, separated, in one tempo and key, lined up on the bar.

        Both songs are downloaded losslessly into a scratch folder, analysed
        (tempo, beat grid, downbeat, key), split into vocals and instrumental,
        then every stem is stretched to the shared tempo, the vocals song is
        moved into the other's key, the vocals are levelled against their new
        instrumental, and silence goes in front of whichever song's first bar
        comes sooner. It is all saved in the mashup's own folder, and the
        vocals of one song and the instrumental of the other go into a fresh
        Audacity project.
        """
        settings = job.settings
        first = 0 if settings.mashup_vocals != "second" else 1  # whose vocals
        other = 1 - first  # whose instrumental, and (by default) tempo

        importing = settings.import_to_audacity
        if importing and audacity.find_audacity() is None and not audacity.is_running():
            self._warn(job, "Not imported: Audacity is not installed (see the README to set it up).")
            importing = False
        audacity_expected = self._prepare_audacity(settings) if importing else False

        separating = separation.engine_info() is not None
        if not separating:
            self._warn(
                job,
                "Not separated: set up the separation engine in Settings (Ctrl+S). "
                "The whole songs were matched up instead.",
            )

        root = settings.mashups
        with temp_workspace(root) as work:
            # 1. Download both. FLAC: lossless, as they are stretched later.
            songs = []
            for number, source in enumerate((job.source, job.mashup_source), 1):
                job.index, job.total, job.title = number, 2, None
                self._set_stage(job, "Downloading")
                try:
                    files = self._download(
                        job, output_dir=work, audio_format="flac", thumbnail=False,
                        source=source, reporter=MashupReporter(job, number, self.log),
                    )
                except Cancelled:
                    return self._finish(job, CANCELLED, "Cancelled.")
                except DownloadError as error:
                    return self._finish(job, FAILED, f"Song {number}: {error}")
                if len(files) != 1:
                    return self._finish(
                        job, FAILED,
                        f"Song {number} is {plural(len(files), 'song')}: a mashup takes one song from each box.",
                    )
                songs.append(files[0])
                job.titles[number - 1] = files[0].stem
                job.touch()

            # 2. Their tempos, and the one they meet at.
            bpms = []
            for number, song in enumerate(songs, 1):
                if job.cancel_event.is_set():
                    return self._finish(job, CANCELLED, "Cancelled.")
                job.index, job.title = number, song.stem
                self._set_stage(job, "Finding the BPM")
                try:
                    bpm = tempo.detect(song)
                except Exception as error:
                    self.log(f"#{job.id} No BPM for {song.stem}: {error}", "warning")
                    bpm = None
                if bpm is None:
                    return self._finish(
                        job, FAILED, f"Song {number} ({song.stem}) has no steady beat to match the other song to."
                    )
                bpms.append(bpm)
                job.bpms[str(song)] = bpm
                job.touch()

            _, vocals_bpm, beat_bpm = mashup.plan(bpms[first], bpms[other], settings.mashup_tempo)
            counted = [None, None]
            counted[first], counted[other] = vocals_bpm, beat_bpm

            # 3. Each song's beat grid (its tempo fine-tuned), where its first
            # bar starts, and its key.
            self._set_stage(job, "Finding the beat and key")
            beats, profiles, keys = [], [], []
            for number, song in enumerate(songs, 1):
                if job.cancel_event.is_set():
                    return self._finish(job, CANCELLED, "Cancelled.")
                job.index, job.title = number, song.stem
                try:
                    analysis = mashup.analyse(song, counted[number - 1])
                except DownloadError as error:
                    self._warn(job, f"Could not find where the beat starts in {song.stem}: {error}")
                    beats.append(0.0)
                    profiles.append(None)
                    keys.append(None)
                    continue
                counted[number - 1] = analysis.bpm
                beats.append(analysis.downbeat)
                profiles.append(analysis.profile)
                keys.append(analysis.key)
                self.log(f"#{job.id} Song {number}: in {analysis.key or 'no clear key'}, "
                         f"first bar at {analysis.downbeat:.2f}s  {song.stem}")
            job.index = job.total = job.title = None

            # The vocals song moves into the other's key (all of it, so the
            # spare stems fit together too).
            semitones = [0, 0]
            if settings.mashup_key and profiles[first] is not None and profiles[other] is not None:
                semitones[first] = mashup.key_shift(profiles[first], profiles[other])
                job.key_shift = semitones[first]
                self.log(
                    f"#{job.id} Key: {keys[first] or '?'} over {keys[other] or '?'}, so the vocals song is "
                    f"{mashup.describe_shift(semitones[first])}"
                )
            keys_after = [mashup.shift_key(key, shift) for key, shift in zip(keys, semitones)]

            target = mashup.target_tempo(counted[first], counted[other], settings.mashup_tempo)
            ratios = [target / bpm for bpm in counted]
            job.target_bpm = target
            job.touch()
            # After stretching, a moment t of the song is at t / ratio.
            starts = [beat / ratio for beat, ratio in zip(beats, ratios)]
            delays = [max(starts) - start for start in starts]
            for number, (song, bpm, ratio) in enumerate(zip(songs, counted, ratios), 1):
                self.log(
                    f"#{job.id} Song {number}: {bpm:.1f} BPM, {mashup.describe_stretch(ratio)} "
                    f"to {target:.1f} BPM  {song.stem}"
                )
                if abs(ratio - 1) > mashup.COMFORTABLE_STRETCH:
                    self._warn(
                        job,
                        f"Song {number} had to go {mashup.describe_stretch(ratio)}, "
                        "which can sound stretched. Songs closer in tempo mash up better.",
                    )

            # 4. Vocals and instrumental of each.
            parts = []
            for number, song in enumerate(songs, 1):
                if job.cancel_event.is_set():
                    return self._finish(job, CANCELLED, "Cancelled.")
                if not separating:
                    parts.append({"Song": song})
                    continue
                job.index, job.total, job.title = number, 2, song.stem
                if self._separation_lock.locked():
                    self._set_stage(job, "Waiting to separate")
                with self._separation_lock:
                    job.step = None
                    self._set_stage(job, "Separating")
                    try:
                        stems = separation.separate(
                            song,
                            work,
                            preset="vocals_instrumental",
                            audio_format="wav",
                            cover_art=False,
                            quality=settings.separation_quality,
                            device=settings.separation_device,
                            uvr=self.uvr,
                            reporter=SeparationReporter(job, self.log),
                            cache=self._cache(settings),
                        )
                    except Cancelled:
                        return self._finish(job, CANCELLED, "Cancelled.")
                    except separation.SeparationError as error:
                        return self._finish(job, FAILED, f"Song {number} ({song.stem}): {error}")
                    finally:
                        job.step = None
                if not {"Vocals", "Instrumental"} <= set(stems):
                    return self._finish(
                        job, FAILED, f"Song {number} ({song.stem}) did not separate into vocals and instrumental."
                    )
                parts.append(stems)

            # The vocals keep the level they had against their own
            # instrumental, now against the new one.
            gains = {}
            try:
                if separating:
                    instrumentals = [mashup.levels(part["Instrumental"])[0] for part in parts]
                    for index, part in enumerate(parts):
                        peak = mashup.levels(part["Vocals"])[1]
                        gains[index, "Vocals"] = mashup.vocal_gain(
                            instrumentals[index], instrumentals[1 - index], peak
                        )
                else:
                    (own, peak), (new, _) = mashup.levels(parts[first]["Song"]), mashup.levels(parts[other]["Song"])
                    gains[first, "Song"] = mashup.vocal_gain(own, new, peak)
            except DownloadError as error:
                self.log(f"#{job.id} Levels left as they were: {error}", "warning")
            for (index, part), gain in gains.items():
                if abs(gain) > 0.05:
                    self.log(f"#{job.id} {songs[index].stem} ({part.lower()}): {gain:+.1f} dB")

            # 5. Stretch everything to the shared tempo, lined up, into the mashup's folder.
            names = [mashup.safe_name(song.stem) for song in songs]
            folder = separation.unique_folder(root, f"{names[first]} x {names[other]}")
            folder.mkdir(parents=True)
            if separating:
                # The mashup itself first; the other two are spares to swap in.
                order = [(first, "Vocals"), (other, "Instrumental"), (other, "Vocals"), (first, "Instrumental")]
            else:
                order = [(first, "Song"), (other, "Song")]
            job.index = job.total = job.title = None
            self._set_stage(job, "Matching tempo and key" if any(semitones) else "Matching the tempo")
            made = []
            for step, (index, part) in enumerate(order):
                job.fraction = step / len(order)
                job.step = f"{names[index]} ({part.lower()})" if part != "Song" else names[index]
                job.touch()
                suffix = f" - {part}" if part != "Song" else ""
                about = f"{int(round(target))} BPM" + (f", {keys_after[index]}" if keys_after[index] else "")
                destination = unique_path(folder / f"{names[index]}{suffix} ({about}).flac")
                try:
                    mashup.stretch(
                        parts[index][part], destination, ratios[index], delays[index],
                        vocals=part == "Vocals", cancel_event=job.cancel_event,
                        semitones=semitones[index], gain_db=gains.get((index, part), 0.0),
                    )
                except Cancelled:
                    job.step = None
                    return self._finish(job, CANCELLED, f"Cancelled. What was made so far is in {describe_path(folder)}.")
                except DownloadError as error:
                    job.step = None
                    return self._finish(job, FAILED, str(error))
                try:
                    tempo.save(destination, target)
                except Exception as error:
                    self.log(f"#{job.id} Could not save the BPM in {destination.name}: {error}", "warning")
                made.append(destination)
                job.stems.append(destination)
                job.touch()
            job.step = None
            job.fraction = None

        # 6. Into a fresh Audacity project, on top of each other.
        wanted = made if settings.mashup_import_all else made[:2]
        imported = False
        project = None
        if importing and not job.cancel_event.is_set():
            timeout = audacity.STARTUP_TIMEOUT if audacity_expected else 10
            try:
                # Saved in the mashup's folder, named after the two songs.
                project = self._import(
                    job, wanted, timeout, new_project=True, save_as=unique_path(folder / f"{folder.name}.aup3")
                )
                imported = True
            except audacity.AudacityError as error:
                self._warn(job, f"Not imported: {error}")

        if separating:
            text = f"{names[first]}'s vocals over {names[other]}'s instrumental"
        else:
            text = f"{names[first]} and {names[other]}"
        text += f" at {target:.0f} BPM"
        if semitones[first]:
            text += f", moved {mashup.describe_shift(semitones[first])}"
            if keys_after[first]:
                text += f" to {keys_after[first]}"
        elif settings.mashup_key and keys[first] and keys[other]:
            text += f" ({keys[first]} and {keys[other]} already fit)"
        text += f", saved to {describe_path(folder)}"
        if imported:
            text += f" and imported into a new Audacity project ({plural(len(wanted), 'track')})"
            if project is not None:
                text += f", saved as {project.name}"
        self._finish(job, DONE, text + ".")

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
                        cache=self._cache(settings),
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

    def _import(self, job, files, timeout, quietly=False, new_project=False, save_as=None):
        """Import ``files`` into Audacity.

        ``quietly`` leaves the job's status and progress alone, for stems
        imported while a separation is still showing its own progress.
        ``new_project`` puts them in a project of their own (the open one,
        if it is empty). ``save_as`` then saves that project as this .aup3
        file, which names it; returns the file, or None if it could not be
        saved (which is only a warning: the tracks are in).
        """
        saved = None
        stage = (lambda text: None) if quietly else (lambda text: self._set_stage(job, text))
        # Audacity has one scripting connection, so imports take turns.
        stage("Waiting to import" if self._import_lock.locked() else "Importing")
        with self._import_lock:
            stage("Importing into Audacity")
            with audacity.connect(timeout, on_wait=lambda: stage("Waiting for Audacity to start")) as session:
                stage("Importing into Audacity")
                if new_project and job.audacity_window is None:
                    job.audacity_window = session.empty_project()
                for number, path in enumerate(files, 1):
                    job.audacity_window = session.import_file(Path(path), window=job.audacity_window)
                    job.imported += 1
                    if not quietly:
                        job.fraction = number / len(files)
                    job.touch()
                if save_as is not None:
                    stage("Saving the Audacity project")
                    try:
                        session.save_project(save_as, window=job.audacity_window)
                        saved = Path(save_as)
                    except audacity.AudacityError as error:
                        self._warn(job, f"The project was not saved under the songs' names: {error}")
        if quietly:
            self.log(f"#{job.id} Imported {', '.join(Path(f).stem for f in files)}", "success")
        return saved
