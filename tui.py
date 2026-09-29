"""The full screen terminal interface, built with Textual."""

import threading
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

import audacity
from jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Runner
from sources import SITE_COLORS, identify, split_input
from utils import describe_path, reveal

THEME = "tokyo-night"
REFRESH_INTERVAL = 1 / 8
AUDACITY_POLL_INTERVAL = 4

PLAYLIST_CHOICES = [("Ask", "ask"), ("Whole playlist", "all"), ("Just the track", "one")]
PLAYLIST_VALUES = {"ask": None, "all": True, "one": False}
QUALITY_CHOICES = [(f"{kbps} kbps", kbps) for kbps in ("128", "192", "256", "320")]

LOG_STYLES = {
    "info": "",
    "success": "green",
    "warning": "yellow",
    "error": "bold red",
}

WELCOME = """\
[b]Paste a link above and press Enter.[/b]

[dim]Works with[/dim] YouTube · YouTube Music · SoundCloud · Bandcamp · Apple Music
Mixcloud · Audiomack · Vimeo · TikTok · X · Twitch · Internet Archive
[dim]and the 1,000+ other sites yt-dlp supports.[/dim]

[dim]Not a link? Type a song name and the top YouTube result is downloaded.
Paste several links at once to queue them all.[/dim]"""


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


def badge(name):
    color = SITE_COLORS.get(name, "#5a6374")
    return Text(f" {name} ", style=f"bold #ffffff on {color}")


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


class Toggle(Checkbox):
    """A checkbox that shows a tick rather than an X when it is on."""

    BUTTON_INNER = "✓"


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
                yield Button(self.no, id="no")
                yield Button(self.yes, variant="primary", id="yes")

    def on_mount(self):
        self.query_one("#yes" if self.default else "#no").focus()

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value):
        self.dismiss(value)


class JobCard(Vertical, can_focus=True):
    """One download: what it is, how far along it is, and how it ended."""

    BINDINGS = [
        Binding("c", "cancel", "Cancel"),
        Binding("r", "retry", "Retry"),
        Binding("o,enter", "reveal", "Show file"),
        Binding("delete,backspace", "remove", "Remove"),
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
            yield Static(badge(self.job.source.name), classes="badge")
            yield Static(classes="title")
            yield Static(classes="state")
        with Horizontal(classes="progress"):
            yield ProgressBar(total=None, show_eta=False, show_percentage=False)
            yield Static(classes="percent")
        yield Static(classes="detail")

    def on_mount(self):
        self.sync()

    def check_action(self, action, parameters):
        """Only offer the actions that make sense for this job right now."""
        finished = self.job.finished_state
        if action == "cancel":
            return not finished
        if action == "retry":
            return self.job.state in (FAILED, CANCELLED)
        if action == "reveal":
            return bool(self.job.files)
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
            self.refresh_bindings()
        self.set_class(bool(job.warning), "-warning")

        self.query_one(".title", Static).update(Text(job.label, style="bold"))
        self.query_one(".state", Static).update(self._state_text())

        self.query_one(".progress").display = job.state == RUNNING
        if job.state == RUNNING:
            bar = self.query_one(ProgressBar)
            percent = self.query_one(".percent", Static)
            if job.fraction is None:
                bar.update(total=None)
                percent.update("")
            else:
                bar.update(total=100, progress=round(job.fraction * 100, 1))
                percent.update(f"{job.fraction:.0%}")

        self.query_one(".detail", Static).update(self._detail_text())
        return True

    def _state_text(self):
        job = self.job
        if job.state == QUEUED:
            return Text("Queued", style="dim")
        if job.state == RUNNING:
            return Text(job.stage, style="bold")
        if job.state == DONE:
            if job.warning:
                return Text("⚠ Saved", style="bold yellow")
            if job.import_into_audacity:
                return Text("✓ In Audacity", style="bold green")
            return Text("✓ Saved", style="bold green")
        if job.state == FAILED:
            return Text("✗ Failed", style="bold red")
        return Text("Cancelled", style="dim")

    def _detail_text(self):
        job = self.job
        if job.state == QUEUED:
            return Text("Waiting for a free slot…", style="dim")

        if job.state == RUNNING:
            parts = []
            if job.total and job.total > 1:
                track = f"Track {job.index}/{job.total}"
                if job.title and job.title != job.label:
                    track += f" · {job.title}"
                parts.append(track)
            elif job.source.kind == "search":
                parts.append(f"Search: {job.text}")
            speed = format_speed(job.speed)
            if speed:
                parts.append(speed)
            eta = format_duration(job.eta)
            if eta and job.stage == "Downloading":
                parts.append(f"{eta} left")
            return Text(" · ".join(parts) or job.source.url, style="dim")

        text = Text()
        style = {DONE: "", FAILED: "red", CANCELLED: "dim"}[job.state]
        text.append(job.message or "", style=style)
        if job.started and job.finished and job.state == DONE:
            text.append(f"  ({format_duration(job.finished - job.started)})", style="dim")
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
        if self.job.files:
            reveal(self.job.files[0])

    def action_remove(self):
        if self.job.finished_state:
            self.app.remove_job(self.job)


class DownloaderApp(App):
    """Paste links, watch them download, find them in Audacity."""

    TITLE = "Audacity Quick Downloader"

    CSS = """
    Screen {
        layout: vertical;
    }

    #top {
        height: auto;
        padding: 1 2 0 2;
    }

    #url {
        border: round $primary;
        border-title-color: $text-muted;
        padding: 0 1;
    }
    #url:focus {
        border: round $accent;
        border-title-color: $accent;
    }

    #options {
        height: 1;
        margin: 0 1 1 1;
    }
    #options > * {
        width: auto;
        margin-right: 2;
    }
    #options .label {
        color: $text-muted;
        margin-right: 1;
    }
    #options Select {
        width: 20;
    }
    #options #quality {
        width: 14;
    }
    #options #folder {
        color: $text-muted;
        width: 1fr;
        text-align: right;
        margin-right: 0;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }

    #jobs {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }

    #empty {
        width: 100%;
        height: auto;
        margin-top: 2;
        padding: 1 2;
        text-align: center;
        border: round $panel-lighten-2;
        color: $text;
    }

    JobCard {
        height: auto;
        padding: 0 1;
        border: round $panel-lighten-2;
        background: $surface;
    }
    JobCard:focus {
        border: round $accent;
        background: $boost;
    }
    JobCard.-running {
        border: round $primary;
    }
    JobCard.-done {
        border: round $success 70%;
    }
    JobCard.-done.-warning {
        border: round $warning 70%;
    }
    JobCard.-failed {
        border: round $error 70%;
    }
    JobCard.-cancelled, JobCard.-queued {
        opacity: 80%;
    }
    JobCard:focus.-done, JobCard:focus.-failed, JobCard:focus.-running {
        border: round $accent;
    }

    .job-top {
        height: 1;
    }
    .job-top .badge {
        width: auto;
        margin-right: 1;
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
    .progress {
        height: 1;
    }
    .progress ProgressBar {
        width: 1fr;
    }
    .progress ProgressBar > Bar {
        width: 1fr;
    }
    .progress ProgressBar > Bar > .bar--indeterminate {
        color: $accent;
    }
    .progress .percent {
        width: 5;
        text-align: right;
    }
    .detail {
        height: auto;
    }

    #log {
        display: none;
        height: 10;
        margin: 0 2;
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    #log.-visible {
        display: block;
    }

    #status {
        height: 1;
        padding: 0 2;
        background: $panel;
    }
    #status > Static {
        width: auto;
        margin-right: 3;
    }
    #status #counts {
        width: 1fr;
        text-align: right;
        margin-right: 0;
    }

    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 1 2;
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
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+l", "toggle_log", "Log"),
        Binding("ctrl+o", "open_folder", "Open folder"),
        Binding("ctrl+r", "clear_finished", "Clear finished"),
        Binding("escape", "focus_input", "Back to input", show=False),
    ]

    def __init__(self, options, updater, initial_urls=()):
        super().__init__()
        self.options = options
        self.updater = updater
        self.initial_urls = list(initial_urls)
        self.runner = Runner(options, updater, ask=self.ask_from_thread)
        self.cards = {}
        self.announced = set()
        self.update_announced = False

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="♫")
        with Vertical(id="top"):
            url = UrlInput(
                placeholder="https://soundcloud.com/…  ·  https://youtu.be/…  ·  or a song name",
                id="url",
            )
            url.border_title = "Paste a link or type a song name"
            yield url
            playlist_default = {None: "ask", True: "all", False: "one"}[self.options.playlist]
            quality = self.options.quality if self.options.quality in ("128", "192", "256", "320") else "192"
            with Horizontal(id="options"):
                yield Toggle(
                    "Import into Audacity",
                    value=not self.options.no_import,
                    compact=True,
                    id="import",
                )
                yield Static("Playlists", classes="label")
                yield Select(
                    PLAYLIST_CHOICES,
                    value=playlist_default,
                    allow_blank=False,
                    compact=True,
                    id="playlist",
                )
                yield Static("Quality", classes="label")
                yield Select(
                    QUALITY_CHOICES,
                    value=quality,
                    allow_blank=False,
                    compact=True,
                    id="quality",
                )
                yield Static(f"→ {describe_path(self.options.output)}", id="folder")
        with VerticalScroll(id="jobs"):
            yield Static(WELCOME, id="empty")
        log = RichLog(id="log", wrap=True, markup=False, max_lines=500)
        log.border_title = "Activity"
        yield log
        with Horizontal(id="status"):
            yield Static(id="audacity")
            yield Static(id="updates")
            yield Static(id="counts")
        yield Footer()

    def on_mount(self):
        if THEME in self.available_themes:
            self.theme = THEME
        self.sub_title = "YouTube · SoundCloud · Bandcamp · Apple Music · and more"
        # Kept rather than looked up each tick: the timers can fire while the
        # app is closing, after the widgets have gone.
        self.jobs_view = self.query_one("#jobs")
        self.empty_view = self.query_one("#empty")
        self.log_view = self.query_one("#log", RichLog)
        self.counts_view = self.query_one("#counts", Static)
        self.updates_view = self.query_one("#updates", Static)
        self.audacity_view = self.query_one("#audacity", Static)
        self.query_one("#url").focus()
        self.set_interval(REFRESH_INTERVAL, self.refresh_jobs)
        self.set_interval(AUDACITY_POLL_INTERVAL, self.poll_audacity)
        self.poll_audacity()
        self.show_audacity(None)
        for url in self.initial_urls:
            self.queue(url)

    # -- adding downloads -----------------------------------------------------

    @on(Input.Submitted, "#url")
    def submitted(self, event):
        entries = split_input(event.value)
        event.input.clear()
        for entry in entries:
            self.queue(entry)

    def queue(self, text):
        source = identify(text)
        if source.ambiguous_playlist and self.options.playlist is None:

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

    # -- options --------------------------------------------------------------

    @on(Toggle.Changed, "#import")
    def import_changed(self, event):
        self.options.no_import = not event.value

    @on(Select.Changed, "#playlist")
    def playlist_changed(self, event):
        self.options.playlist = PLAYLIST_VALUES[event.value]

    @on(Select.Changed, "#quality")
    def quality_changed(self, event):
        self.options.quality = event.value

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
                self.announce(job)

        self.empty_view.display = not jobs
        self.drain_log()
        self.show_updates()
        self.show_counts()

    def announce(self, job):
        """Pop up a notification when a download finishes."""
        if job.state == DONE:
            severity = "warning" if job.warning else "information"
            self.notify(job.message, title=job.label, severity=severity, markup=False)
        elif job.state == FAILED:
            self.notify(job.message, title="Download failed", severity="error", timeout=8, markup=False)

    def drain_log(self):
        log = self.log_view
        while not self.runner.logs.empty():
            when, level, message = self.runner.logs.get_nowait()
            stamp = time.strftime("%H:%M:%S", time.localtime(when))
            log.write(Text.assemble((f"{stamp} ", "dim"), (message, LOG_STYLES.get(level, ""))))

    def show_counts(self):
        jobs = self.runner.jobs
        running = sum(job.state == RUNNING for job in jobs)
        queued = sum(job.state == QUEUED for job in jobs)
        done = sum(job.state == DONE for job in jobs)
        failed = sum(job.state == FAILED for job in jobs)
        parts = []
        if running:
            parts.append(f"[b]{running}[/b] downloading")
        if queued:
            parts.append(f"{queued} queued")
        if done:
            parts.append(f"[green]{done} done[/green]")
        if failed:
            parts.append(f"[red]{failed} failed[/red]")
        self.counts_view.update(" · ".join(parts))

    def show_updates(self):
        pieces = []
        for status in self.updater.packages.values():
            version = status.installed or "not installed"
            if status.helper:
                # Deno and friends are only worth a mention while they are
                # being installed, or if they could not be.
                helper = {
                    "updating": f"[yellow]{status.name} ↻ installing[/yellow]",
                    "updated": f"[green]{status.name} {version} ✓ installed[/green]",
                    "outdated": f"[yellow]{status.name} missing[/yellow]",
                    "failed": f"[red]{status.name} (install failed)[/red]",
                }.get(status.state)
                if helper:
                    pieces.append(helper)
            elif status.state == "checking":
                pieces.append(f"[dim]{status.name} {version} …[/dim]")
            elif status.state == "updating":
                pieces.append(f"[yellow]{status.name} ↻ {status.latest}[/yellow]")
            elif status.state == "updated":
                pieces.append(f"[green]{status.name} {version} ↑[/green]")
            elif status.state == "outdated":
                pieces.append(f"[yellow]{status.name} {version} → {status.latest}[/yellow]")
            elif status.state == "failed":
                pieces.append(f"[red]{status.name} {version} (update failed)[/red]")
            elif status.state == "current":
                pieces.append(f"{status.name} {version} [green]✓[/green]")
            else:
                pieces.append(f"[dim]{status.name} {version}[/dim]")
        self.updates_view.update("   ".join(pieces))

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
            hint = "pip install --upgrade -r requirements.txt"
            names = ", ".join(s.name for s in outdated)
            self.runner.log(f"{names} could be updated or installed. Run: {hint}", "warning")
        if self.updater.error:
            self.notify(self.updater.error, title="Update failed", severity="warning", markup=False)
            self.runner.log(f"Update failed: {self.updater.error}", "warning")

    def poll_audacity(self):
        self.run_worker(self._check_audacity, thread=True, exclusive=True, group="audacity")

    def _check_audacity(self):
        running = audacity.is_running()
        self.call_from_thread(self.show_audacity, running)

    def show_audacity(self, running):
        widget = self.audacity_view
        if running is None:
            widget.update("[dim]○ Audacity …[/dim]")
        elif running:
            widget.update("[green]●[/green] Audacity running")
        else:
            widget.update("[dim]○ Audacity not running[/dim]")

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
        self.options.output.mkdir(parents=True, exist_ok=True)
        reveal(self.options.output)

    def action_focus_input(self):
        self.query_one("#url").focus()

    def _cards_in_order(self):
        return list(self.query(JobCard))

    def action_focus_next_card(self):
        self._move_card_focus(1)

    def action_focus_previous_card(self):
        self._move_card_focus(-1)

    def _move_card_focus(self, step):
        cards = self._cards_in_order()
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


def run(options, updater, urls=()):
    app = DownloaderApp(options, updater, urls)
    try:
        app.run()
    finally:
        app.runner.shutdown()
    return 0
