"""The Settings screen: every saved default in one place (Ctrl+S)."""

import copy
import sys
from dataclasses import fields
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, ProgressBar, Select, Static

import audacity
import separation
from settings import (
    AUDIO_FORMATS,
    BITRATES,
    DEVICES,
    PLAYLIST_MODES,
    SEPARATION_QUALITIES,
    Settings,
    settings_path,
)
from settings import load as load_settings
from utils import default_downloads_dir

LABEL_WIDTH = 28

WELCOME = """\
[b]Welcome to Audacity Quick Downloader![/b]
Have a look at where downloads (and separated stems) are saved, then press [b]Save[/b].
You can come back here any time with [b]Ctrl+S[/b]."""

NO_UVR = (
    "Ultimate Vocal Remover 5 was not found, so stem separation is unavailable. "
    "Install it from github.com/Anjok07/ultimatevocalremovergui (see the README), "
    "or point \"UVR5 folder\" at it if it is somewhere unusual."
)
ENGINE_NOTE = (
    "Separation runs UVR5's models through the audio-separator package, which needs "
    "PyTorch: about 3 GB with an NVIDIA GPU, about 200 MB for CPU only. It is "
    "installed into .venv-separator in the app's folder, and your UVR5 models are "
    "reused, not downloaded again."
)


class Toggle(Checkbox):
    """A compact checkbox that shows a tick rather than an X when it is on."""

    BUTTON_INNER = "✓"

    def __init__(self, label, value, **kwargs):
        super().__init__(label, value, compact=True, **kwargs)

    @property
    def _button(self):
        # Textual draws the glyph dimmed when off, which reads as a faint
        # tick; an empty box is much clearer.
        self.BUTTON_INNER = "✓" if self.value else " "
        return super()._button


class PickerUnavailable(Exception):
    """There is no graphical folder picker (no Tk, or no display)."""


def ask_path(kind, initial, title):
    """Show the system's folder or file picker. Runs in a worker thread.

    Returns the chosen path, or None if the picker was cancelled.
    """
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
    except (ImportError, RuntimeError) as error:
        # tkinter.TclError (no display, e.g. over SSH) is a RuntimeError.
        raise PickerUnavailable(str(error))
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        initial = str(initial) if initial and Path(initial).exists() else str(Path.home())
        if kind == "folder":
            path = filedialog.askdirectory(parent=root, initialdir=initial, title=title, mustexist=False)
        else:
            path = filedialog.askopenfilename(
                parent=root,
                initialdir=initial,
                title=title,
                filetypes=[("Cookies file", "*.txt"), ("All files", "*.*")],
            )
    finally:
        root.destroy()
    return str(Path(path)) if path else None


def row(label, *widgets, id=None):
    return Horizontal(Static(label, classes="label"), *widgets, classes="row", id=id)


def hint(text="", id=None):
    return Static(text, classes="hint", id=id)


def path_input(name, value, placeholder, kind="folder"):
    """An Input plus a Browse… button that opens the system picker."""
    return (
        Input(value=value, placeholder=placeholder, compact=True, id=f"f-{name}"),
        Button("Browse…", compact=True, classes="browse", name=f"{kind}:{name}"),
    )


class SettingsScreen(Screen):
    """Edits a copy of the saved settings. Dismisses with the new Settings or None."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = f"""
    SettingsScreen {{
        background: $background;
    }}
    #settings-body {{
        padding: 1 2 0 2;
        scrollbar-size-vertical: 1;
    }}
    #welcome {{
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
    }}
    .section {{
        height: auto;
        border: round $panel-lighten-2;
        border-title-color: $accent;
        border-title-style: bold;
        padding: 1 2 0 2;
        margin-bottom: 1;
        background: $surface;
    }}
    .row {{
        height: auto;
        margin-bottom: 1;
    }}
    .row > .label {{
        width: {LABEL_WIDTH};
        color: $text-muted;
    }}
    .row > Input {{
        width: 1fr;
    }}
    #settings-top {{
        height: 1;
        margin: 1 3;
    }}
    #settings-title {{
        width: 1fr;
    }}
    #settings-keys {{
        width: auto;
    }}
    .row > Select {{
        width: 52;
    }}
    .row > Button {{
        margin-left: 2;
        min-width: 10;
    }}
    .row > Toggle {{
        width: auto;
    }}
    .status {{
        width: 1fr;
    }}
    .hint {{
        color: $text-muted;
        padding-left: {LABEL_WIDTH};
        margin: -1 0 1 0;
    }}
    #engine-progress, .task-progress {{
        height: auto;
        padding-left: {LABEL_WIDTH};
        margin: -1 0 1 0;
    }}
    #engine-progress ProgressBar, .task-progress ProgressBar {{
        width: 1fr;
    }}
    #engine-progress Bar, .task-progress Bar {{
        width: 1fr;
    }}
    #settings-buttons Button {{
        min-width: 12;
    }}
    #settings-buttons {{
        height: auto;
        padding: 0 2 1 2;
        align-horizontal: right;
    }}
    #settings-buttons Button {{
        margin-left: 2;
    }}
    """

    def __init__(self, first_run=False, detected=None):
        super().__init__()
        self.first_run = first_run
        if first_run and detected is not None:
            # Nothing saved yet: start from defaults that suit this PC.
            saved = detected
        else:
            # Edit what is saved on disk, not this session's command line overrides.
            saved, _ = load_settings()
        self.draft = copy.copy(saved)
        self.uvr = separation.find_uvr(saved.uvr_path)
        # What "Automatic" means on this PC, for the hints.
        info = separation.engine_info()
        self.engine_device = info.get("device", "cpu") if info else ("gpu" if separation.has_nvidia_gpu() else "cpu")

    # -- layout -----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        s = self.draft
        with Horizontal(id="settings-top"):
            yield Static("[b]Settings[/b]", id="settings-title")
            yield Static("[dim]ctrl+s save   esc cancel[/dim]", id="settings-keys")
        with VerticalScroll(id="settings-body"):
            if self.first_run:
                yield Static(WELCOME, id="welcome")

            with Vertical(classes="section") as section:
                section.border_title = "Downloads"
                yield row("Save downloads to", *path_input("download_dir", s.download_dir, str(default_downloads_dir())))
                yield row("Audio format", self._select("audio_format", {k: v["label"] for k, v in AUDIO_FORMATS.items()}))
                yield row("Bitrate", self._select("bitrate", {b: f"{b} kbps" for b in BITRATES}))
                yield hint(id="bitrate-hint")
                yield row("Playlists", self._select("playlist_mode", PLAYLIST_MODES))
                yield row("Cover art", Toggle("Embed the thumbnail or album art", s.embed_thumbnail, id="f-embed_thumbnail"))

            with Vertical(classes="section") as section:
                section.border_title = "Audacity"
                yield row("Status", Static(self._audacity_status(), classes="status"))
                yield row("Import", Toggle("Import downloads into Audacity automatically", s.import_to_audacity, id="f-import_to_audacity"))
                yield row("Start Audacity", Toggle("Open Audacity if it is not running", s.launch_audacity, id="f-launch_audacity"))
                if audacity.find_audacity() and audacity.script_pipe_enabled() is False:
                    yield row(
                        "Scripting module",
                        Static("[yellow]mod-script-pipe is off[/yellow]", classes="status"),
                        Button("Turn it on", compact=True, id="enable-pipe"),
                    )

            with Vertical(classes="section") as section:
                section.border_title = "Stem separation (Ultimate Vocal Remover 5)"
                yield row("UVR5", Static(id="uvr-status", classes="status"))
                yield hint(id="uvr-hint")
                yield row("Separation engine", Static(id="engine-status", classes="status"),
                          Button("Set up", compact=True, id="engine-install"),
                          Button("Remove", compact=True, id="engine-remove"))
                with Horizontal(id="engine-progress"):
                    yield ProgressBar(total=None, show_eta=False)
                yield hint(ENGINE_NOTE, id="engine-hint")
                yield row("Models", Static(id="models-status", classes="status"),
                          Button("Download all", compact=True, id="models-download"))
                with Horizontal(id="models-progress", classes="task-progress"):
                    yield ProgressBar(total=None, show_eta=False)
                yield row("Separate", Toggle("Split every download into stems", s.separate, id="f-separate"))
                yield row("Preset", self._select("separation_preset", {k: p.label for k, p in separation.PRESETS.items()}))
                yield row("Quality", self._select("separation_quality", SEPARATION_QUALITIES))
                yield hint(id="preset-hint")
                yield row("Save stems to", *path_input("separation_dir", s.separation_dir, str(s.separated)))
                yield hint(id="stems-hint")
                yield row("Import into Audacity", Toggle("The original song", s.import_original, id="f-import_original"))
                yield row("", Toggle("The separated stems", s.import_stems, id="f-import_stems"))
                yield row("Run on", self._select("separation_device", DEVICES))
                yield row("UVR5 folder", *path_input("uvr_path", s.uvr_path, "Found automatically"))

            with Vertical(classes="section") as section:
                section.border_title = "Cookies (optional)"
                yield row("Apple Music cookies", *path_input("apple_cookies", s.apple_cookies, "cookies.txt next to main.py", kind="file"))
                yield row("Other sites' cookies", *path_input("site_cookies", s.site_cookies, "None", kind="file"))
                yield hint("For age restricted videos and sign-in-only tracks. Netscape format, exported from your browser.")

            with Vertical(classes="section") as section:
                section.border_title = "Appearance"
                themes = {name: name.replace("-", " ").title() for name in sorted(self.app.available_themes)}
                yield row("Theme", self._select("theme", themes))
                yield hint(f"Settings are saved in {settings_path()}")

        with Horizontal(id="settings-buttons"):
            yield Button("Cancel", id="cancel", compact=True)
            yield Button("Save", variant="primary", id="save", compact=True)

    def _select(self, name, choices):
        value = getattr(self.draft, name)
        options = [(label, key) for key, label in choices.items()]
        if value not in choices:
            value = options[0][1]
        return Select(options, value=value, allow_blank=False, compact=True, id=f"f-{name}")

    def on_mount(self):
        self.title = "Settings"
        self.refresh_state()
        self.set_interval(0.25, self.refresh_engine)
        self.refresh_engine()

    # -- keeping the form consistent ------------------------------------------

    def _audacity_status(self):
        executable = audacity.find_audacity()
        if executable is None:
            return Text.from_markup(
                "[yellow]Not found.[/yellow] Install it from audacityteam.org to import "
                "downloads into it (see the README). Downloads still work without it."
            )
        return Text.from_markup(f"[green]✓[/green] {executable}")

    def value(self, name):
        widget = self.query_one(f"#f-{name}")
        if isinstance(widget, Input):
            return widget.value.strip()
        return widget.value

    @on(Input.Changed)
    @on(Select.Changed)
    @on(Checkbox.Changed)
    def changed(self, event):
        if getattr(event, "input", None) is not None and event.input.id == "f-uvr_path":
            self.uvr = separation.find_uvr(event.value.strip())
        self.refresh_state()

    def refresh_state(self):
        """Enable, disable and explain things as the form changes."""
        lossy = AUDIO_FORMATS[self.value("audio_format")]["lossy"]
        self.query_one("#f-bitrate").disabled = not lossy
        self.set_hint("#bitrate-hint", "" if lossy else "Lossless formats keep the full quality, so there is no bitrate to pick.")

        uvr = self.uvr
        self.query_one("#uvr-status", Static).update(
            Text.from_markup(f"[green]✓[/green] {'UVR ' + uvr.version + ' at ' if uvr.version else ''}{uvr.path}")
            if uvr
            else Text.from_markup("[yellow]Not found[/yellow]")
        )
        self.set_hint("#uvr-hint", "" if uvr else NO_UVR)

        separate_toggle = self.query_one("#f-separate", Toggle)
        separate_toggle.disabled = uvr is None
        if uvr is None and separate_toggle.value:
            separate_toggle.value = False
        # Everything else here stays editable with separation off, so it can
        # be set up first and switched on later.

        audio_format = AUDIO_FORMATS[self.value("audio_format")]
        saved_as = audio_format["label"].split(" (")[0]
        if audio_format["lossy"]:
            saved_as += f" at {self.value('bitrate')} kbps"
        self.set_hint(
            "#stems-hint",
            f"Stems are saved like your downloads: {saved_as}, with the song's tags"
            + (" and cover art." if self.value("embed_thumbnail") else "."),
        )

        preset = separation.PRESETS[self.value("separation_preset")]
        device = self.value("separation_device")
        if device == "auto":
            device = self.engine_device
        quality = separation.resolve_quality(self.value("separation_quality"), device)
        models = preset.models(quality)
        text = f"{preset.description} Uses {', '.join(Path(m).stem for m in dict.fromkeys(models))}"
        missing = [
            model
            for model in dict.fromkeys(models)
            if not (separation.MODEL_CACHE / model).exists() and not (uvr and uvr.find_model(model))
        ]
        if missing:
            size = sum(separation.MODEL_SIZES_MB.get(model, 0) for model in missing)
            text += f"; about {size} MB is downloaded the first time."
        else:
            text += ", already downloaded."
        if self.value("separation_quality") == "auto":
            text += f" Automatic means {separation.QUALITY_LEVELS[quality]['label']} here."
        self.set_hint("#preset-hint", text)

        downloads = Path(self.value("download_dir") or default_downloads_dir())
        self.query_one("#f-separation_dir", Input).placeholder = str(downloads / "Separated")

    def set_hint(self, selector, text):
        hint = self.query_one(selector, Static)
        hint.update(text)
        hint.display = bool(text)

    def refresh_engine(self):
        installer = getattr(self.app, "engine_installer", None)
        installing = installer is not None and installer.state == "running"
        info = separation.engine_info()
        status = self.query_one("#engine-status", Static)
        progress = self.query_one("#engine-progress")
        progress.display = installing
        if installing:
            status.update(Text.from_markup(f"[yellow]↻ {installer.stage}…[/yellow]"))
            bar = progress.query_one(ProgressBar)
            if installer.fraction is None:
                bar.update(total=None)
            else:
                bar.update(total=100, progress=installer.fraction * 100)
        elif info:
            device = info.get("gpu") or ("GPU" if info.get("device") == "gpu" else "CPU")
            status.update(Text.from_markup(
                f"[green]✓ Installed[/green] · runs on {device} · "
                f"audio-separator {info.get('audio_separator', '?')}"
            ))
        elif installer is not None and installer.state == "failed":
            status.update(Text.from_markup(f"[red]Install failed:[/red] {installer.error}"))
        else:
            status.update(Text.from_markup("[yellow]Not installed[/yellow]"))

        install = self.query_one("#engine-install", Button)
        install.label = "Cancel" if installing else ("Reinstall" if info else "Set up")
        install.disabled = self.uvr is None and not installing
        self.query_one("#engine-remove", Button).display = bool(info) and not installing
        self.query_one("#engine-hint").display = not info or installing
        self.refresh_models(bool(info) and not installing)

    def refresh_models(self, engine_ready):
        """The "Download all" row: what is there, and any download running."""
        downloader = getattr(self.app, "model_downloader", None)
        busy = downloader is not None and downloader.state == "running"
        status = self.query_one("#models-status", Static)
        progress = self.query_one("#models-progress")
        progress.display = busy
        button = self.query_one("#models-download", Button)
        button.label = "Cancel" if busy else "Download all"
        if busy:
            status.update(Text.from_markup(f"[yellow]↓ {downloader.stage}[/yellow]"))
            bar = progress.query_one(ProgressBar)
            if downloader.fraction is None:
                bar.update(total=None)
            else:
                bar.update(total=100, progress=downloader.fraction * 100)
            button.disabled = False
            return

        models = separation.all_models()
        missing = separation.missing_models(self.uvr, models)
        if not missing:
            status.update(Text.from_markup(f"[green]✓[/green] All {len(models)} preset models are downloaded"))
        else:
            size = sum(separation.MODEL_SIZES_MB.get(model, 0) for model in missing)
            text = f"{len(models) - len(missing)} of {len(models)} downloaded · about {size} MB to fetch"
            if downloader is not None and downloader.state == "failed":
                text = f"[red]Download failed:[/red] {downloader.error}"
            status.update(Text.from_markup(text))
        button.display = bool(missing)
        button.disabled = not engine_ready

    # -- buttons --------------------------------------------------------------

    @on(Button.Pressed, ".browse")
    def browse(self, event):
        kind, name = event.button.name.split(":", 1)
        field = self.query_one(f"#f-{name}", Input)
        initial = field.value or field.placeholder
        title = {"download_dir": "Save downloads to", "separation_dir": "Save separated stems to",
                 "uvr_path": "Ultimate Vocal Remover's folder"}.get(name, "Choose a file")

        def pick():
            try:
                path = ask_path(kind, initial if kind == "folder" else Path(initial).parent, title)
            except PickerUnavailable:
                hint = " On Arch: sudo pacman -S tk" if sys.platform.startswith("linux") else ""
                self.app.call_from_thread(
                    self.notify,
                    f"No folder picker is available here, so type the path instead.{hint}",
                    severity="warning",
                )
                return
            if path:
                self.app.call_from_thread(setattr, field, "value", path)

        self.run_worker(pick, thread=True, exclusive=True, group="browse")

    @on(Button.Pressed, "#enable-pipe")
    def enable_pipe(self, event):
        if audacity.is_running():
            self.notify("Close Audacity first, then try again.", severity="warning")
        elif audacity.enable_script_pipe():
            event.button.parent.query_one(".status", Static).update("[green]✓ mod-script-pipe is on[/green]")
            event.button.display = False
        else:
            self.notify(audacity.ENABLE_INSTRUCTIONS, severity="warning", timeout=10)

    @on(Button.Pressed, "#engine-install")
    def engine_install(self):
        installer = getattr(self.app, "engine_installer", None)
        if installer is not None and installer.state == "running":
            installer.cancel()
            return
        device = self.value("separation_device")
        if device == "auto":
            device = "gpu" if separation.has_nvidia_gpu() else "cpu"
        size = "about 3 GB (PyTorch with CUDA for your NVIDIA GPU)" if device == "gpu" else "about 200 MB (PyTorch for CPU)"
        from tui import ConfirmScreen

        def answered(yes):
            if yes:
                self.app.start_engine_install(device)
                self.refresh_engine()

        self.app.push_screen(
            ConfirmScreen(
                f"Set up the separation engine?\nThis downloads {size} into .venv-separator "
                "in the app's folder. You can keep using the app while it installs.",
                yes="Download and install",
                no="Not now",
            ),
            answered,
        )

    @on(Button.Pressed, "#models-download")
    def models_download(self):
        downloader = getattr(self.app, "model_downloader", None)
        if downloader is not None and downloader.state == "running":
            downloader.cancel()
        else:
            self.app.start_model_download()
        self.refresh_engine()

    @on(Button.Pressed, "#engine-remove")
    def engine_remove(self):
        from tui import ConfirmScreen

        def answered(yes):
            if yes:
                separation.remove_engine()
                self.refresh_engine()

        self.app.push_screen(
            ConfirmScreen("Remove the separation engine (.venv-separator)?\nYour UVR5 models are not touched.",
                          yes="Remove", no="Keep", default=False),
            answered,
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def action_save(self):
        new = Settings()
        for field in fields(Settings):
            try:
                value = self.value(field.name)
            except Exception:
                value = getattr(self.draft, field.name)
            setattr(new, field.name, value)
        new.tidy()
        try:
            new.save()
        except OSError as error:
            self.notify(f"Could not save settings: {error}", severity="error")
            return
        self.dismiss(new)
