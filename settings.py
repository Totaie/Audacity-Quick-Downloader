"""The user's saved defaults, kept in a small JSON file.

Everything here can be changed from the Settings screen in the app (Ctrl+S),
and most of it can be overridden for a single run on the command line. The
file lives in the user's own settings folder rather than next to the script,
so each person on a shared PC gets their own, and updating the app never
touches it.
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from formats import AUDIO_FORMATS, BITRATES
from utils import default_downloads_dir

APP_NAME = "AudacityQuickDownloader"

PLAYLIST_MODES = {"ask": "Ask each time", "all": "Whole playlist", "one": "Just the track"}
SEPARATION_QUALITIES = {
    "auto": "Automatic (Best with a GPU, Fast without)",
    "best": "Best: newest models, maximum overlap",
    "balanced": "Balanced: newest models, about twice as fast",
    "fast": "Fast: classic UVR5 models, good on a CPU",
}
DEVICES = {"auto": "Automatic", "gpu": "GPU (NVIDIA)", "cpu": "CPU only"}


def config_dir():
    """Where settings are kept: %APPDATA% on Windows, ~/.config elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def settings_path():
    return config_dir() / "settings.json"


@dataclass
class Settings:
    # Downloads
    download_dir: str = ""
    audio_format: str = "mp3"
    bitrate: str = "192"
    playlist_mode: str = "ask"
    embed_thumbnail: bool = True

    # Audacity
    import_to_audacity: bool = True
    launch_audacity: bool = True

    # Stem separation with UVR5's models
    separate: bool = False
    separation_preset: str = "vocals_instrumental"
    separation_quality: str = "auto"
    separation_dir: str = ""
    import_original: bool = True
    import_stems: bool = True
    separation_device: str = "auto"
    uvr_path: str = ""  # blank means find it automatically

    # Cookies
    apple_cookies: str = ""
    site_cookies: str = ""

    # Interface
    theme: str = "tokyo-night"

    # -- derived values -------------------------------------------------------

    @property
    def downloads(self):
        return Path(self.download_dir).expanduser() if self.download_dir else default_downloads_dir()

    @property
    def separated(self):
        """Where stems go. Defaults to a folder inside the downloads folder."""
        if self.separation_dir:
            return Path(self.separation_dir).expanduser()
        return self.downloads / "Separated"

    @property
    def playlist(self):
        """The playlist mode as the rest of the app wants it: None, True or False."""
        return {"ask": None, "all": True, "one": False}.get(self.playlist_mode)

    # -- loading and saving ---------------------------------------------------

    def tidy(self):
        """Replace anything invalid (hand edited, or from an older version)."""
        defaults = Settings()
        choices = {
            "audio_format": AUDIO_FORMATS,
            "bitrate": BITRATES,
            "playlist_mode": PLAYLIST_MODES,
            "separation_device": DEVICES,
            "separation_quality": SEPARATION_QUALITIES,
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                setattr(self, name, getattr(defaults, name))
        for field in fields(self):
            value, default = getattr(self, field.name), getattr(defaults, field.name)
            if type(value) is not type(default):
                setattr(self, field.name, default)
        return self

    def save(self, path=None):
        path = Path(path) if path else settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(temporary, path)  # never leave a half written file behind


def load(path=None):
    """Load saved settings. Returns (settings, first_run)."""
    path = Path(path) if path else settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings().tidy(), True
    except (OSError, ValueError):
        return Settings().tidy(), False

    known = {field.name for field in fields(Settings)}
    settings = Settings(**{key: value for key, value in data.items() if key in known})
    return settings.tidy(), False
