"""Keep yt-dlp and gamdl current without holding up start up.

Sites change constantly, and an out of date yt-dlp or gamdl is the most common
reason downloads break. The check runs in the background while the app is
already usable: every package's latest version is fetched from PyPI at the
same time, and pip only runs if something is actually out of date.

Deno and yt-dlp-ejs, which yt-dlp needs for full YouTube support, are also
installed here if they are missing, so they turn up even without the .bat
launcher.

Downloads wait for the update to finish before they start (usually a second
or two), so a package is never replaced while it is in use.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata

from utils import python_executable

# Kept at their latest release.
PACKAGES = ("yt-dlp", "gamdl")
# Only installed when missing. Deno ships almost weekly as a 40 MB wheel, and
# yt-dlp-ejs is pinned by yt-dlp[default] so it moves in step with yt-dlp.
HELPERS = ("deno", "yt-dlp-ejs")
# What to hand pip for each package. yt-dlp-ejs has to match the yt-dlp it
# is used with, and asking for it through yt-dlp's [default] extra gets the
# version that yt-dlp pins.
PIP_NAMES = {"yt-dlp": "yt-dlp[default]", "yt-dlp-ejs": "yt-dlp[default]"}
PYPI_URL = "https://pypi.org/pypi/{}/json"
CHECK_TIMEOUT = 10
INSTALL_TIMEOUT = 600  # the first Deno download is around 40 MB


@dataclass
class PackageStatus:
    name: str
    installed: str = None
    latest: str = None
    # "checking", "current", "outdated", "updating", "updated", "failed",
    # "offline" or "skipped". For a helper, "outdated" means missing.
    state: str = "checking"
    helper: bool = False


def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def latest_version(name):
    """The newest release on PyPI, or None if PyPI cannot be reached."""
    request = urllib.request.Request(
        PYPI_URL.format(name), headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as response:
            return json.load(response)["info"]["version"]
    except (OSError, ValueError, KeyError):
        return None


def _version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def in_virtualenv():
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


class Updater:
    """Checks for, and installs, newer yt-dlp and gamdl in a background thread."""

    def __init__(self, packages=PACKAGES, helpers=HELPERS, enabled=True):
        self.packages = {name: PackageStatus(name, installed_version(name)) for name in packages}
        for name in helpers:
            self.packages[name] = PackageStatus(name, installed_version(name), helper=True)
        self.enabled = enabled
        # Only install into this project's own virtual environment: quietly
        # upgrading packages in someone's system Python would be rude.
        self.can_install = in_virtualenv()
        self.error = None
        self.done = threading.Event()

    # -- public API ---------------------------------------------------------

    def start(self):
        if not self.enabled:
            for status in self.packages.values():
                status.state = "skipped"
            self.done.set()
            return self
        threading.Thread(target=self._run, name="updater", daemon=True).start()
        return self

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    @property
    def busy(self):
        return not self.done.is_set()

    # -- the work -----------------------------------------------------------

    def _run(self):
        try:
            self._check()
            outdated = [s for s in self.packages.values() if s.state == "outdated"]
            if outdated and self.can_install:
                self._install(outdated)
        except Exception as error:  # never let an update take the app down
            self.error = str(error)
        finally:
            for status in self.packages.values():
                if status.state in ("checking", "updating"):
                    status.state = "failed"
            self.done.set()

    def _check(self):
        for status in self.packages.values():
            if status.helper:
                status.state = "current" if status.installed else "outdated"

        names = [name for name, status in self.packages.items() if not status.helper]
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            latest = dict(zip(names, pool.map(latest_version, names)))

        for name, version in latest.items():
            status = self.packages[name]
            status.latest = version
            if version is None:
                status.state = "offline"
            elif status.installed is None or _version_key(version) > _version_key(
                status.installed
            ):
                status.state = "outdated"
            else:
                status.state = "current"

    def _install(self, outdated):
        # One pip call for everything: two pips writing into the same
        # environment at once can corrupt it (gamdl depends on yt-dlp).
        for status in outdated:
            status.state = "updating"

        options = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        command = [
            python_executable(),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            "--quiet",
            *dict.fromkeys(PIP_NAMES.get(status.name, status.name) for status in outdated),
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=INSTALL_TIMEOUT,
                env=dict(os.environ, PIP_NO_INPUT="1"),
                **options,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.error = str(error)
            return

        # Make importlib.metadata notice the packages pip just replaced.
        importlib.invalidate_caches()
        for status in outdated:
            status.installed = installed_version(status.name)
            if result.returncode == 0:
                status.state = "updated"
            else:
                status.state = "failed"
        if result.returncode != 0:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            self.error = lines[-1] if lines else f"pip exited with {result.returncode}"

