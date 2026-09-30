"""The full screen terminal interface, built with Textual.

The screen is kept deliberately quiet: a box to paste into, three chips for
the choices people change often, the list of downloads, and one line of key
hints. Everything else lives in Settings (Ctrl+S).
"""

import threading
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ProgressBar, RichLog, Static

import audacity
import separation
from jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Runner
from settings_screen import SettingsScreen
from sources import SITE_COLORS, identify, split_input
from utils import describe_path, reveal

REFRESH_INTERVAL = 1 / 8
AUDACITY_POLL_INTERVAL = 4

PLAYLIST_LABELS = {"ask": "Ask", "all": "Whole playlist", "one": "Just the track"}
STEM_CHOICES = [None] + list(separation.PRESETS)  # None means off

LOG_STYLES = {"info": "", "success": "green", "warning": "yellow", "error": "bold red"}

HINTS = "enter add   ctrl+s settings   ctrl+o folder   ctrl+l log   ctrl+q quit"
JOB_HINTS = "c cancel   r retry   o show   del remove   esc back"

WELCOME = """\
[b]Drop in a link to get started[/b]

[dim]YouTube · SoundCloud · Bandcamp · Apple Music · and 1,000+ more
Type a song name to search, or give a file or folder on this PC[/dim]"""


def format_speed(speed):
    if not speed:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if speed < 1024 or unit == "GB":
            return f"{speed:.1f} {unit}/s" if unit != "B" else f"{speed:.0f} B/s"
        speed /= 1024


def format_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


class UrlInput(Input):
    """An Input that keeps every line of a multi-line paste, not just the first."""

    BINDINGS = [Binding("down", "app.focus_next_card", "Downloads", show=False)]

    def _on_paste(self, event):
        if event.text:
            self.insert_text_at_cursor(" ".join(event.text.split()))
        # Without prevent_default, Input's own paste handler runs as well and
        # the text goes in twice.
        event.prevent_default()
        event.stop()


class Chip(Static, can_focus=True):
    """A small pill that changes a setting when clicked (or Enter/Space)."""

    BINDINGS = [Binding("enter,space", "press", "Change", show=False)]

    def __init__(self, action, **kwargs):
        super().__init__(**kwargs)
        self.chip_action = action

    def on_click(self):
        self.action_press()

    def action_press(self):
        getattr(self.app, f"action_{self.chip_action}")()

    def show(self, label, value, on=True):
        self.update(Text.assemble((f"{label} ", "dim"), (value, "bold" if on else "dim")))
        self.set_class(on, "-on")


class ConfirmScreen(ModalScreen):
    """A yes/no question. Dismisses with True or False."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n", "answer(False)", "No", show=False),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, question, yes="Yes", no="No", default=True):
        super().__init__()
        self.question = question
        self.yes, self.no, self.default = yes, no, default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.question, id="question")
            with Horizontal(id="buttons"):
                yield Button(self.no, id="no", compact=True)
                yield Button(self.yes, variant="primary", id="yes", compact=True)

    def on_mount(self):
        self.query_one("#yes" if self.default else "#no").focus()

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value):
        self.dismiss(value)


class JobCard(Vertical, can_focus=True):
    """One download: a title, what is happening, and a thin progress line."""

    BINDINGS = [
        Binding("c", "cancel", "Cancel", show=False),
        Binding("r", "retry", "Retry", show=False),
        Binding("o,enter", "reveal", "Show file", show=False),
        Binding("delete,backspace", "remove", "Remove", show=False),
        Binding("up,k", "app.focus_previous_card", "Previous", show=False),
        Binding("down,j", "app.focus_next_card", "Next", show=False),
    ]

    def __init__(self, job):
        super().__init__(classes="job -queued")
        self.job = job
        self.seen_revision = -1
        self.seen_state = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="job-top"):
            yield Static(classes="title")
            yield Static(classes="state")
        yield Static(classes="detail")
        yield ProgressBar(total=None, show_eta=False, show_percentage=False)

    def on_mount(self):
        self.sync()

    def check_action(self, action, parameters):
        """Only allow the actions that make sense for this job right now."""
        finished = self.job.finished_state
        if action == "cancel":
            return not finished
        if action == "retry":
            return self.job.state in (FAILED, CANCELLED)
        if action == "reveal":
            return bool(self.job.files or self.job.stems)
        if action == "remove":
            return finished
        return True

    def sync(self):
        """Redraw from the Job, if anything changed."""
        job = self.job
        if job.revision == self.seen_revision:
            return False
        self.seen_revision = job.revision

        if job.state != self.seen_state:
            for state in (QUEUED, RUNNING, DONE, FAILED, CANCELLED):
                self.set_class(job.state == state, f"-{state}")
            self.seen_state = job.state
        self.set_class(bool(job.warning), "-warning")

        self.query_one(".title", Static).update(Text(job.label, style="bold"))
        self.query_one(".state", Static).update(self._state_text())
        self.query_one(".detail", Static).update(self._detail_text())

        bar = self.query_one(ProgressBar)
        bar.display = job.state == RUNNING
        if job.state == RUNNING:
            if job.fraction is None:
                bar.update(total=None)
            else:
                bar.update(total=100, progress=round(job.fraction * 100, 1))
        return True

    def _state_text(self):
        job = self.job
        if job.state == QUEUED:
            return Text("Queued", style="dim")
        if job.state == RUNNING:
            text = Text(job.stage, style="bold")
            if job.fraction is not None:
                text.append(f"  {job.fraction:.0%}", style="dim")
            return text
        if job.state == DONE:
            if job.warning:
                return Text("Done, with a note", style="yellow")
            if job.imported:
                return Text("✓ In Audacity", style="green")
            return Text("✓ Done", style="green")
        if job.state == FAILED:
            return Text("✗ Failed", style="red")
        return Text("Cancelled", style="dim")

    def _detail_text(self):
        job = self.job
        color = SITE_COLORS.get(job.source.name)
        text = Text(job.source.name, style=color or "dim")

        def add(part, style="dim"):
            if part:
                text.append("  ·  ", style="dim")
                text.append(part, style=style)

        if job.state == QUEUED:
            add("waiting for a free slot")
        elif job.state == RUNNING:
            if job.total and job.total > 1:
                track = f"{job.index}/{job.total}"
                if job.title and job.title != job.label:
                    track += f" {job.title}"
                add(track)
            elif job.source.kind == "search":
                add(f"“{job.text}”")
            add(job.step)
            add(format_speed(job.speed))
            if job.stage == "Downloading":
                eta = format_duration(job.eta)
                add(f"{eta} left" if eta else None)
        else:
            style = {DONE: "dim", FAILED: "red", CANCELLED: "dim"}[job.state]
            add(job.message, style)
            if job.started and job.finished and job.state == DONE:
                add(format_duration(job.finished - job.started))
            if job.warning:
                text.append(f"\n{job.warning}", style="yellow")
        return text

    # -- actions --------------------------------------------------------------

    def action_cancel(self):
        self.app.runner.cancel(self.job)

    def action_retry(self):
        self.app.runner.retry(self.job)
        self.app.refresh_jobs()

    def action_reveal(self):
        # Separated songs are more useful to see as their stems folder.
        if self.job.stems:
            reveal(self.job.stems[0])
        elif self.job.files:
            reveal(self.job.files[0])

    def action_remove(self):
        if self.job.finished_state:
            self.app.remove_job(self.job)


class DownloaderApp(App):
    """Paste links, watch them download, find them in Audacity."""

    TITLE = "Audacity Quick Downloader"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    #topbar {
        height: 1;
        margin: 1 3 0 3;
    }
    #brand {
        width: 1fr;
    }
    #status {
        width: auto;
    }

    #url {
        margin: 1 2 0 2;
        padding: 0 1;
        border: round $panel-lighten-2;
        background: $background;
    }
    #url:focus {
        border: round $accent;
        background-tint: $foreground 0%;
    }

    #chips {
        height: 1;
        margin: 1 3;
    }
    Chip {
        width: auto;
        padding: 0 1;
        margin-right: 1;
        background: $panel;
        color: $text-muted;
    }
    Chip.-on {
        background: $primary 25%;
        color: $text;
    }
    Chip:hover {
        background: $primary 40%;
    }
    Chip:focus {
        background: $primary 55%;
        color: $text;
    }
    #folder {
        width: 1fr;
        text-align: right;
        color: $text-disabled;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    #jobs {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }
    #empty {
        width: 100%;
        height: auto;
        margin-top: 4;
        text-align: center;
    }

    JobCard {
        height: auto;
        padding: 0 1 0 2;
        margin-bottom: 1;
        border-left: outer $panel-lighten-2;
    }
    JobCard:focus {
        background: $boost;
    }
    JobCard.-running {
        border-left: outer $accent;
    }
    JobCard.-done {
        border-left: outer $success;
    }
    JobCard.-done.-warning {
        border-left: outer $warning;
    }
    JobCard.-failed {
        border-left: outer $error;
    }
    JobCard.-queued, JobCard.-cancelled {
        opacity: 70%;
    }
    .job-top {
        height: 1;
    }
    .job-top .title {
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .job-top .state {
        width: auto;
        margin-left: 2;
    }
    .detail {
        height: auto;
        color: $text-muted;
    }
    JobCard ProgressBar {
        width: 100%;
        height: 1;
    }
    JobCard ProgressBar > Bar {
        width: 1fr;
    }
    JobCard ProgressBar > Bar > .bar--bar {
        color: $accent;
        background: $panel;
    }
    JobCard ProgressBar > Bar > .bar--indeterminate {
        color: $accent;
        background: $panel;
    }

    #log {
        display: none;
        height: 10;
        margin: 0 2;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        scrollbar-size-vertical: 1;
    }
    #log.-visible {
        display: block;
    }

    #bottombar {
        height: 1;
        margin: 0 3 1 3;
    }
    #activity {
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #hints {
        width: auto;
        color: $text-disabled;
    }

    ConfirmScreen {
        align: center middle;
        background: $background 60%;
    }
    #dialog {
        width: 60;
        max-width: 90%;
        height: auto;
        padding: 1 3;
        border: round $accent;
        background: $surface;
    }
    #question {
        margin-bottom: 1;
    }
    #buttons {
        height: auto;
        align-horizontal: right;
    }
    #buttons Button {
        margin-left: 2;
        min-width: 12;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+s", "settings", "Settings"),
        Binding("ctrl+o", "open_folder", "Open folder"),
        Binding("ctrl+l", "toggle_log", "Log"),
        Binding("ctrl+r", "clear_finished", "Clear finished"),
        Binding("escape", "focus_input", "Back", show=False),
    ]

    def __init__(self, settings, updater, initial_urls=(), first_run=False):
        super().__init__()
        self.settings = settings
        self.updater = updater
        self.initial_urls = list(initial_urls)
        self.first_run = first_run
        self.runner = Runner(settings, updater, ask=self.ask_from_thread)
        self.engine_installer = None
        self.model_downloader = None
        self.audacity_running = None
        self.cards = {}
        self.announced = set()
        self.update_announced = False

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("[b]♫  Quick Downloader[/b]  [dim]for Audacity[/dim]", id="brand")
            yield Static(id="status")
        yield UrlInput(placeholder="Paste a link, search for a song, or drop in a file or folder", id="url")
        with Horizontal(id="chips"):
            yield Chip("toggle_import", id="chip-import")
            yield Chip("cycle_stems", id="chip-stems")
            yield Chip("cycle_playlists", id="chip-playlists")
            yield Static(id="folder")
        with VerticalScroll(id="jobs"):
            yield Static(WELCOME, id="empty")
        log = RichLog(id="log", wrap=True, markup=False, max_lines=500)
        log.border_title = "Activity"
        yield log
        with Horizontal(id="bottombar"):
            yield Static(id="activity")
            yield Static(HINTS, id="hints")

    def on_mount(self):
        # Kept rather than looked up each tick: the timers can fire while the
        # app is closing, after the widgets have gone.
        self.jobs_view = self.query_one("#jobs")
        self.empty_view = self.query_one("#empty")
        self.log_view = self.query_one("#log", RichLog)
        self.status_view = self.query_one("#status", Static)
        self.activity_view = self.query_one("#activity", Static)
        self.hints_view = self.query_one("#hints", Static)
        self.show_settings()
        self.query_one("#url").focus()
        self.set_interval(REFRESH_INTERVAL, self.refresh_jobs)
        self.set_interval(AUDACITY_POLL_INTERVAL, self.poll_audacity)
        self.poll_audacity()
        for url in self.initial_urls:
            self.queue(url)
        if self.first_run:
            self.action_settings()

    # -- adding downloads -----------------------------------------------------

    @on(Input.Submitted, "#url")
    def submitted(self, event):
        entries = split_input(event.value)
        event.input.clear()
        for entry in entries:
            self.queue(entry)

    def queue(self, text):
        source = identify(text)
        if source.ambiguous_playlist and self.settings.playlist is None:

            def answered(whole):
                self.runner.add(text, playlist=whole)
                self.refresh_jobs()

            self.push_screen(
                ConfirmScreen(
                    "That video is part of a playlist.\n"
                    "Do you want the whole playlist, or just this video?",
                    yes="Whole playlist",
                    no="Just this video",
                    default=False,
                ),
                answered,
            )
            return
        self.runner.add(text)
        self.refresh_jobs()

    # -- the chips --------------------------------------------------------------
    # These change the current session; Settings (Ctrl+S) changes the defaults.

    def action_toggle_import(self):
        self.settings.import_to_audacity = not self.settings.import_to_audacity
        self.show_chips()

    def action_cycle_stems(self):
        current = self.settings.separation_preset if self.settings.separate else None
        index = STEM_CHOICES.index(current) if current in STEM_CHOICES else 0
        choice = STEM_CHOICES[(index + 1) % len(STEM_CHOICES)]
        self.settings.separate = choice is not None
        if choice:
            self.settings.separation_preset = choice
            if separation.engine_info() is None:
                self.notify(
                    "Set up the separation engine in Settings (Ctrl+S) first.",
                    title="Stems are not set up yet",
                    severity="warning",
                )
        self.show_chips()

    def action_cycle_playlists(self):
        modes = list(PLAYLIST_LABELS)
        index = modes.index(self.settings.playlist_mode) if self.settings.playlist_mode in modes else 0
        self.settings.playlist_mode = modes[(index + 1) % len(modes)]
        self.show_chips()

    def show_chips(self):
        settings = self.settings
        self.query_one("#chip-import", Chip).show(
            "Audacity", "On" if settings.import_to_audacity else "Off", settings.import_to_audacity
        )
        stems = self.query_one("#chip-stems", Chip)
        stems.display = self.runner.uvr is not None
        preset = separation.PRESETS.get(settings.separation_preset)
        stems.show("Stems", preset.short if settings.separate and preset else "Off", settings.separate)
        self.query_one("#chip-playlists", Chip).show(
            "Playlists", PLAYLIST_LABELS.get(settings.playlist_mode, "Ask"), True
        )
        self.query_one("#folder", Static).update(f"→ {describe_path(settings.downloads)}")

    def show_settings(self):
        """Make the chips and theme match ``self.settings``."""
        if self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        self.show_chips()
        self.show_status()

    def action_settings(self):
        if isinstance(self.screen, SettingsScreen):
            return

        def saved(new):
            if new is None:
                if self.first_run:
                    self.settings.save()  # do not greet them again next time
                return
            for name, value in vars(new).items():
                setattr(self.settings, name, value)
            self.runner.refresh_uvr()
            self.show_settings()
            self.notify("Settings saved.")

        self.push_screen(SettingsScreen(first_run=self.first_run, detected=self.settings), saved)
        self.first_run = False

    def start_engine_install(self, device):
        self.engine_installer = separation.EngineInstaller(device, log=self.runner.log).start()

    def start_model_download(self):
        self.model_downloader = separation.ModelDownloader(self.runner.uvr, log=self.runner.log).start()

    # -- keeping the screen current -------------------------------------------

    def refresh_jobs(self):
        container = self.jobs_view
        if not container.is_attached:  # the app is closing
            return
        jobs = self.runner.jobs

        for job in jobs:
            card = self.cards.get(job.id)
            if card is None:
                card = self.cards[job.id] = JobCard(job)
                container.mount(card, before=0)
                container.scroll_home(animate=False)
            elif card.is_mounted:
                card.sync()
            if job.finished_state and job.id not in self.announced:
                self.announced.add(job.id)
                if job.state == FAILED:
                    self.notify(job.message, title=job.label, severity="error", timeout=8, markup=False)

        self.empty_view.display = not jobs
        self.drain_log()
        self.show_status()
        self.show_activity()
        self.hints_view.update(JOB_HINTS if isinstance(self.focused, JobCard) else HINTS)

    def drain_log(self):
        log = self.log_view
        while not self.runner.logs.empty():
            when, level, message = self.runner.logs.get_nowait()
            stamp = time.strftime("%H:%M:%S", time.localtime(when))
            log.write(Text.assemble((f"{stamp} ", "dim"), (message, LOG_STYLES.get(level, ""))))

    def show_status(self):
        """The dots in the top right: is Audacity up, is UVR5 ready."""
        text = Text()
        if audacity.find_audacity() is not None:
            if self.audacity_running:
                text.append("● ", style="green")
                text.append("Audacity")
            else:
                text.append("○ Audacity", style="dim")
        if self.runner.uvr is not None:
            installer, downloader = self.engine_installer, self.model_downloader
            if text:
                text.append("    ")
            if installer is not None and installer.state == "running":
                percent = f" {installer.fraction:.0%}" if installer.fraction is not None else ""
                text.append(f"↻ Installing UVR5 engine{percent}", style="yellow")
            elif downloader is not None and downloader.state == "running":
                percent = f" {downloader.fraction:.0%}" if downloader.fraction is not None else ""
                text.append(f"↓ Downloading models{percent}", style="yellow")
            elif separation.engine_info():
                text.append("● ", style="green")
                text.append("UVR5")
            else:
                text.append("◐ UVR5 not set up", style="yellow")
        self.status_view.update(text)

    def show_activity(self):
        """The bottom left: what is going on, only when something is."""
        jobs = self.runner.jobs
        running = sum(job.state == RUNNING for job in jobs)
        queued = sum(job.state == QUEUED for job in jobs)
        done = sum(job.state == DONE for job in jobs)
        failed = sum(job.state == FAILED for job in jobs)
        parts = []
        updating = [s.name for s in self.updater.packages.values() if s.state in ("checking", "updating")]
        if any(s.state == "updating" for s in self.updater.packages.values()):
            parts.append(f"[yellow]↻ updating {', '.join(updating)}[/yellow]")
        if running:
            parts.append(f"{running} working")
        if queued:
            parts.append(f"{queued} queued")
        if done:
            parts.append(f"[green]{done} done[/green]")
        if failed:
            parts.append(f"[red]{failed} failed[/red]")
        self.activity_view.update("   ".join(parts))

        if not self.updater.busy and not self.update_announced:
            self.update_announced = True
            self.announce_updates()

    def announce_updates(self):
        packages = self.updater.packages.values()
        updated = [f"{s.name} {s.installed}" for s in packages if s.state == "updated"]
        outdated = [s for s in packages if s.state == "outdated"]
        if updated:
            message = f"Updated {', '.join(updated)}."
            self.notify(message, title="Updates installed")
            self.runner.log(message, "success")
        if outdated:
            names = ", ".join(s.name for s in outdated)
            self.runner.log(
                f"{names} could be updated or installed. Run: pip install --upgrade -r requirements.txt",
                "warning",
            )
        if self.updater.error:
            self.notify(self.updater.error, title="Update failed", severity="warning", markup=False)
            self.runner.log(f"Update failed: {self.updater.error}", "warning")

    def poll_audacity(self):
        self.run_worker(self._check_audacity, thread=True, exclusive=True, group="audacity")

    def _check_audacity(self):
        running = audacity.is_running()
        self.call_from_thread(setattr, self, "audacity_running", running)

    # -- questions from worker threads ----------------------------------------

    def ask_from_thread(self, question, default=True):
        """Show a yes/no dialog from a worker thread and wait for the answer."""
        answer = [default]
        answered = threading.Event()

        def show():
            def done(result):
                answer[0] = bool(result)
                answered.set()

            self.push_screen(ConfirmScreen(question, default=default), done)

        self.call_from_thread(show)
        answered.wait()
        return answer[0]

    # -- actions --------------------------------------------------------------

    def remove_job(self, job):
        card = self.cards.pop(job.id, None)
        if job in self.runner.jobs:
            self.runner.jobs.remove(job)
        if card is not None:
            if card.has_focus:
                self.action_focus_input()
            card.remove()
        self.refresh_jobs()

    def action_clear_finished(self):
        for job in [job for job in self.runner.jobs if job.finished_state]:
            self.remove_job(job)

    def action_toggle_log(self):
        self.log_view.toggle_class("-visible")

    def action_open_folder(self):
        self.settings.downloads.mkdir(parents=True, exist_ok=True)
        reveal(self.settings.downloads)

    def action_focus_input(self):
        self.query_one("#url").focus()

    def action_focus_next_card(self):
        self._move_card_focus(1)

    def action_focus_previous_card(self):
        self._move_card_focus(-1)

    def _move_card_focus(self, step):
        cards = list(self.query(JobCard))
        if not cards:
            return
        focused = self.focused
        if focused in cards:
            index = cards.index(focused) + step
            if index < 0:
                return self.action_focus_input()
            cards[min(index, len(cards) - 1)].focus()
        else:
            cards[0].focus()

    async def action_quit(self):
        active = self.runner.active()
        if not active:
            self.exit()
            return

        def answered(really):
            if really:
                self.exit()

        count = len(active)
        self.push_screen(
            ConfirmScreen(
                f"{count} download{'s are' if count != 1 else ' is'} still going.\n"
                "Quit and cancel {}?".format("them" if count != 1 else "it"),
                yes="Quit",
                no="Keep going",
                default=False,
            ),
            answered,
        )


def run(settings, updater, urls=(), first_run=False):
    app = DownloaderApp(settings, updater, urls, first_run)
    try:
        app.run()
    finally:
        app.runner.shutdown()
    return 0
