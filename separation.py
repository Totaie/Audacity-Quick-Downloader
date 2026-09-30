"""Split songs into stems (vocals, drums, bass...) with Ultimate Vocal Remover's models.

UVR5 itself is a GUI with no command line, so the separating is done by the
"audio-separator" package, which runs the same model families (MDX-Net, VR,
Demucs, Roformer). It is only offered when UVR5 is installed, and it reuses
the models already downloaded in UVR5 rather than fetching them again.

audio-separator needs PyTorch, which is several gigabytes with GPU support,
so it lives in its own virtual environment (.venv-separator) that is only
created when the user asks for it from the Settings screen. Separations run
in a separate process using that environment: separator_worker.py.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from formats import convert_audio
from utils import Cancelled, DownloadError, Reporter, python_executable, strip_ansi

PROJECT_DIR = Path(__file__).resolve().parent
ENGINE_DIR = PROJECT_DIR / ".venv-separator"
ENGINE_MARKER = ENGINE_DIR / "engine.json"
WORKER = PROJECT_DIR / "separator_worker.py"


def _model_cache():
    """Where the engine looks for models.

    Kept in the user's local app data rather than the project folder: that is
    usually the drive UVR5 is installed on, and hard links (which is how UVR5's
    models are shared, at no extra space) only work within one drive. It also
    survives reinstalling the engine.
    """
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "AudacityQuickDownloader" / "models"


MODEL_CACHE = _model_cache()

TORCH_INDEX = {
    # CUDA 12.8 builds cover everything from GTX 16xx cards to the RTX 50
    # series, and run on older drivers than the CUDA 13 builds.
    "gpu": "https://download.pytorch.org/whl/cu128",
    "cpu": "https://download.pytorch.org/whl/cpu",
}
# The newest onnxruntime-gpu that matches PyTorch's CUDA 12 (it runs the
# MDX-Net models, the ones most presets use).
ONNXRUNTIME_CUDA12 = "onnxruntime-gpu<1.27"

# Where UVR5 keeps each kind of model, relative to its "models" folder.
UVR_MODEL_FOLDERS = (
    "MDX_Net_Models",
    "VR_Models",
    "Demucs_Models/v3_v4_repo",
    "Demucs_Models",
)


class SeparationError(DownloadError):
    """Separating a song failed."""


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
#
# Model choices come from MVSEP's public benchmarks (mvsep.com/quality_checker),
# picking the best single models audio-separator can run:
#
#   Vocals / instrumental  BS-Roformer "Resurrection" by unwa   vocals SDR 11.36
#   Six stems              BS-Roformer SW (ships with UVR 5.6)  drums 14.11, bass 14.62,
#                                                               #1 on the guitar board
#   Lead / backing vocals  BS-Roformer Karaoke by anvuew        lead SDR 10.23
#
# versus 8-10 for the MDX-Net and Demucs v4 models UVR5 has offered for years.
# Those older models are kept for the Fast quality level: they are several
# times quicker, which matters most without an NVIDIA GPU.


@dataclass(frozen=True)
class Step:
    """Run one model over one input.

    ``source`` is "song" or the name of a stem made by an earlier step.
    ``stems`` maps the model's stem names to ours. Names starting with "_"
    are intermediate files, used by a later step and then thrown away.
    ``merge`` combines stems by adding them together: {ours: [names...]}.
    """

    model: str
    source: str
    stems: dict
    merge: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Preset:
    label: str
    short: str  # for the options bar
    description: str
    steps: tuple  # Best and Balanced quality
    fast_steps: tuple  # Fast quality

    def steps_for(self, quality):
        return self.fast_steps if quality == "fast" else self.steps

    def models(self, quality):
        return [step.model for step in self.steps_for(quality)]

    def outputs(self, quality):
        """The stems this makes, in the order they are imported into Audacity."""
        names = []
        for step in self.steps_for(quality):
            names += [name for name in step.stems.values() if not name.startswith("_")]
            names += list(step.merge)
        return sorted(set(names), key=lambda name: STEM_ORDER.index(name) if name in STEM_ORDER else 99)


STEM_ORDER = ["Vocals", "Lead Vocals", "Backing Vocals", "Instrumental", "Drums", "Bass", "Guitar", "Piano", "Other"]


# Best models (downloaded on first use if UVR5 does not have them).
RESURRECTION = "bs_roformer_vocals_resurrection_unwa.ckpt"
SW = "BS-Roformer-SW.ckpt"
KARAOKE = "bs_roformer_karaoke_anvuew.ckpt"
# The classic UVR5 models, for Fast.
INST_HQ_3 = "UVR-MDX-NET-Inst_HQ_3.onnx"
KARA_2 = "UVR_MDXNET_KARA_2.onnx"
DEMUCS_4 = "htdemucs.yaml"
DEMUCS_6 = "htdemucs_6s.yaml"

MODEL_SIZES_MB = {RESURRECTION: 195, SW: 667, KARAOKE: 195, INST_HQ_3: 64, KARA_2: 50, DEMUCS_4: 80, DEMUCS_6: 53}

# A karaoke model splits a vocal track into the lead (its "Vocals") and
# everything else, which on a vocals-only input is the backing vocals.
SPLIT_VOCALS = Step(KARAOKE, "_vocals", {"Vocals": "Lead Vocals", "Instrumental": "Backing Vocals"})
SPLIT_VOCALS_FAST = Step(KARA_2, "_vocals", {"Vocals": "Lead Vocals", "Instrumental": "Backing Vocals"})
SIX_STEMS = {"Vocals": "Vocals", "Drums": "Drums", "Bass": "Bass", "Guitar": "Guitar", "Piano": "Piano", "Other": "Other"}

PRESETS = {
    "vocals_instrumental": Preset(
        "Vocals + Instrumental",
        "Vocals + Instrumental",
        "Two stems: the vocals and everything else.",
        (Step(RESURRECTION, "song", {"Vocals": "Vocals", "Other": "Instrumental"}),),
        (Step(INST_HQ_3, "song", {"Vocals": "Vocals", "Instrumental": "Instrumental"}),),
    ),
    "lead_backing": Preset(
        "Instrumental + Lead + Backing vocals",
        "Inst + Lead + Backing",
        "Three stems: the vocals are split again into the lead and the backing vocals.",
        (Step(RESURRECTION, "song", {"Vocals": "_vocals", "Other": "Instrumental"}), SPLIT_VOCALS),
        (Step(INST_HQ_3, "song", {"Vocals": "_vocals", "Instrumental": "Instrumental"}), SPLIT_VOCALS_FAST),
    ),
    "four_stems": Preset(
        "Vocals, Drums, Bass, Other",
        "4 stems",
        "Four stems. Guitar and piano are part of Other.",
        (
            Step(
                SW,
                "song",
                {"Vocals": "Vocals", "Drums": "Drums", "Bass": "Bass",
                 "Guitar": "_guitar", "Piano": "_piano", "Other": "_other"},
                merge={"Other": ["_guitar", "_piano", "_other"]},
            ),
        ),
        (Step(DEMUCS_4, "song", {"Vocals": "Vocals", "Drums": "Drums", "Bass": "Bass", "Other": "Other"}),),
    ),
    "six_stems": Preset(
        "Six stems (adds guitar & piano)",
        "6 stems (+guitar, piano)",
        "Vocals, drums, bass, guitar, piano and other.",
        (Step(SW, "song", SIX_STEMS),),
        (Step(DEMUCS_6, "song", SIX_STEMS),),
    ),
    "all_stems": Preset(
        "All stems (six stems, vocals split into lead & backing)",
        "All stems",
        "Everything: lead and backing vocals, drums, bass, guitar, piano and other "
        "(Fast leaves out guitar and piano).",
        (Step(SW, "song", {**SIX_STEMS, "Vocals": "_vocals"}), SPLIT_VOCALS),
        (
            Step(DEMUCS_4, "song", {"Vocals": "_vocals", "Drums": "Drums", "Bass": "Bass", "Other": "Other"}),
            SPLIT_VOCALS_FAST,
        ),
    ),
}
DEFAULT_PRESET = "vocals_instrumental"

# How hard each quality level works. Overlap is how many overlapping windows
# every moment of the song is predicted in, then averaged: more is cleaner
# and proportionally slower. fp16 autocast is ~1.7x faster on an RTX card and
# measured 78 dB below the fp32 result (inaudible), so it is used on GPUs.
QUALITY_LEVELS = {
    "best": {"label": "Best", "overlap": 8},
    "balanced": {"label": "Balanced", "overlap": 4},
    "fast": {"label": "Fast", "overlap": None},
}


def resolve_quality(quality, device):
    """Turn "auto" into a real level: Best on a GPU, Fast on the CPU."""
    if quality in QUALITY_LEVELS:
        return quality
    return "best" if device == "gpu" else "fast"


def tuning(quality, device):
    """The engine settings for a quality level on a device."""
    level = QUALITY_LEVELS[resolve_quality(quality, device)]
    return {"overlap": level["overlap"], "autocast": device == "gpu"}


# --------------------------------------------------------------------------
# Finding UVR5
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UvrInstall:
    path: Path
    version: str = ""

    @property
    def models_dir(self):
        return self.path / "models"

    def find_model(self, filename):
        """The path of ``filename`` in UVR5's model folders, or None."""
        for folder in UVR_MODEL_FOLDERS:
            candidate = self.models_dir / folder / filename
            if candidate.is_file():
                return candidate
        return None


def _looks_like_uvr(path):
    path = Path(path)
    if sys.platform == "darwin" and path.suffix == ".app":
        path = path / "Contents" / "Resources"
    return (path / "models").is_dir() and (
        any(path.glob("UVR*.exe")) or (path / "UVR.py").exists() or (path / "gui_data").is_dir()
    )


def _registry_installs():
    """(location, version) pairs from Windows' list of installed programs."""
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, key_path in roots:
        try:
            key = winreg.OpenKey(root, key_path)
        except OSError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, index)) as app:
                        name = winreg.QueryValueEx(app, "DisplayName")[0]
                        if "vocal remover" not in str(name).lower():
                            continue
                        location = winreg.QueryValueEx(app, "InstallLocation")[0]
                        try:
                            version = winreg.QueryValueEx(app, "DisplayVersion")[0]
                        except OSError:
                            version = ""
                        found.append((location, version))
                except OSError:
                    continue
    return found


def find_uvr(override=""):
    """Return the UVR5 installation, or None if it is not installed."""
    candidates = []
    for path in (override, os.environ.get("UVR_PATH")):
        if path:
            candidates.append((path, ""))
    candidates += _registry_installs()
    if sys.platform == "win32":
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                sub = Path(root) / "Programs" if variable == "LOCALAPPDATA" else Path(root)
                candidates.append((sub / "Ultimate Vocal Remover", ""))
    elif sys.platform == "darwin":
        candidates.append(("/Applications/Ultimate Vocal Remover.app", ""))
    else:
        # On Linux UVR5 runs from a checkout of its source, wherever it was cloned.
        for folder in (
            Path.home() / "ultimatevocalremovergui",
            Path.home() / "Ultimate Vocal Remover",
            Path.home() / ".local" / "share" / "ultimatevocalremovergui",
            Path("/opt/ultimatevocalremovergui"),
            Path("/opt/Ultimate Vocal Remover"),
        ):
            candidates.append((folder, ""))

    for path, version in candidates:
        path = Path(path).expanduser()
        if path.is_dir() and _looks_like_uvr(path):
            if sys.platform == "darwin" and path.suffix == ".app":
                path = path / "Contents" / "Resources"
            return UvrInstall(path, version)
    return None


# --------------------------------------------------------------------------
# The separation engine (audio-separator in its own virtual environment)
# --------------------------------------------------------------------------


def engine_python():
    if sys.platform == "win32":
        return ENGINE_DIR / "Scripts" / "python.exe"
    return ENGINE_DIR / "bin" / "python"


def engine_info():
    """What was installed, or None if the engine is not set up."""
    if not engine_python().exists():
        return None
    try:
        return json.loads(ENGINE_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def has_nvidia_gpu():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False
    try:
        result = subprocess.run(
            [smi, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
            **_no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _no_window():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _base_python():
    """The Python this app's own venv was made from, to make another one."""
    return getattr(sys, "_base_executable", None) or python_executable()


PIP_PROGRESS = re.compile(r"Progress (\d+) of (\d+)")
# pip lines worth showing in the activity log: big downloads, and the result.
PIP_ACTIVITY = re.compile(r"^(Downloading \S+ \(\d[\d.]* [MG]B\)|Successfully installed)")


class EngineInstaller:
    """Creates .venv-separator and installs PyTorch and audio-separator into it.

    Runs in a background thread; the interface reads ``stage``, ``fraction``
    and ``state`` to show how it is going.
    """

    def __init__(self, device="auto", log=None):
        if device == "auto":
            device = "gpu" if has_nvidia_gpu() else "cpu"
        self.device = device
        self.log = log or (lambda text, level="info": None)
        self.stage = "Starting"
        self.fraction = None
        self.state = "running"  # then "done", "failed" or "cancelled"
        self.error = None
        self.cancel_event = threading.Event()
        self._process = None

    def start(self):
        threading.Thread(target=self._run, name="engine-install", daemon=True).start()
        return self

    def cancel(self):
        self.cancel_event.set()
        if self._process and self._process.poll() is None:
            self._process.kill()

    def _run(self):
        try:
            self._install()
            self.state = "done"
            self.stage = "Installed"
            self.log(f"Separation engine installed ({self.device.upper()}).", "success")
        except Cancelled:
            self.state = "cancelled"
            self.stage = "Cancelled"
        except Exception as error:
            self.state = "failed"
            self.error = str(error)
            self.stage = "Failed"
            self.log(f"Installing the separation engine failed: {error}", "error")

    def _install(self):
        # Always start clean: a half finished or mismatched install (say, a
        # CPU PyTorch where a GPU one was wanted) is not worth repairing.
        remove_engine()
        python = str(engine_python())
        self._step("Creating its virtual environment", [_base_python(), "-m", "venv", str(ENGINE_DIR)])
        self._step("Updating pip", [python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"])
        pip = [python, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "raw"]

        # torch and torchvision have to come from the same index as a
        # matching pair, or pip "upgrades" to PyPI's CPU-only build.
        self._step(
            "Downloading PyTorch" + (" with CUDA (about 3 GB)" if self.device == "gpu" else " (about 200 MB)"),
            pip + ["torch", "torchvision", "--index-url", TORCH_INDEX[self.device]],
        )
        # Pin that pair while audio-separator installs, for the same reason.
        pinned = subprocess.run(
            [python, "-m", "pip", "freeze", "--disable-pip-version-check"],
            capture_output=True,
            text=True,
            **_no_window(),
        ).stdout.splitlines()
        lines = [line for line in pinned if line.lower().startswith(("torch==", "torchvision=="))]
        if self.device == "gpu":
            # onnxruntime-gpu 1.27 and later are built for CUDA 13; they cannot
            # use PyTorch's CUDA 12 libraries and quietly fall back to the CPU.
            lines.append(ONNXRUNTIME_CUDA12)
        constraints = ENGINE_DIR / "constraints.txt"
        constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")
        extra = "gpu" if self.device == "gpu" else "cpu"
        self._step("Installing audio-separator", pip + [f"audio-separator[{extra}]", "-c", str(constraints)])

        self.stage, self.fraction = "Checking it works", None
        result = subprocess.run(
            [python, str(WORKER), "--check"], capture_output=True, text=True, errors="replace", **_no_window()
        )
        try:
            check = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise SeparationError(f"The engine does not start: {(result.stderr or 'no output').strip()[-300:]}")

        device = "gpu" if check.get("cuda") else "cpu"
        if self.device == "gpu" and device == "cpu":
            self.log(
                "PyTorch cannot use your NVIDIA GPU, so separation will run on the CPU. "
                "Updating the graphics driver usually fixes this; then Reinstall.",
                "warning",
            )
        elif check.get("gpu"):
            self.log(f"Separation will run on {check['gpu']}.", "success")
        ENGINE_MARKER.write_text(
            json.dumps({"device": device, "gpu": check.get("gpu"), "audio_separator": check.get("audio_separator")}),
            encoding="utf-8",
        )

    def _step(self, stage, command):
        if self.cancel_event.is_set():
            raise Cancelled("Cancelled.")
        self.stage, self.fraction = stage, None
        self.log(f"Separation engine: {stage}")
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=dict(os.environ, PIP_NO_INPUT="1", PYTHONIOENCODING="utf-8"),
            **_no_window(),
        )
        tail = []
        for line in self._process.stdout:
            line = strip_ansi(line).strip()
            if not line:
                continue
            progress = PIP_PROGRESS.search(line)
            if progress:
                done, total = map(int, progress.groups())
                self.fraction = done / total if total else None
                continue
            tail = (tail + [line])[-20:]
            if PIP_ACTIVITY.match(line):
                self.log(f"  {line}")
        self._process.wait()
        if self.cancel_event.is_set():
            raise Cancelled("Cancelled.")
        if self._process.returncode != 0:
            errors = [line for line in tail if "error" in line.lower()]
            raise SeparationError((errors or tail or ["no output"])[-1])


def remove_engine():
    shutil.rmtree(ENGINE_DIR, ignore_errors=True)


# --------------------------------------------------------------------------
# Downloading the models ahead of time
# --------------------------------------------------------------------------


def all_models():
    """Every model any preset uses, at any quality."""
    models = []
    for preset in PRESETS.values():
        for quality in ("best", "fast"):
            models += preset.models(quality)
    return list(dict.fromkeys(models))


def missing_models(uvr, models=None):
    """The models that are neither downloaded nor in UVR5."""
    return [
        model
        for model in (models or all_models())
        if not (MODEL_CACHE / model).exists() and not (uvr and uvr.find_model(model))
    ]


class ModelDownloader:
    """Fetches every model the presets use, in a background thread.

    UVR5's copies are linked in first, so only what is really missing is
    downloaded. The interface reads ``stage``, ``fraction`` and ``state``.
    """

    def __init__(self, uvr, log=None):
        self.uvr = uvr
        self.log = log or (lambda text, level="info": None)
        self.stage = "Starting"
        self.fraction = None
        self.state = "running"  # then "done", "failed" or "cancelled"
        self.error = None
        self._process = None
        self._cancelled = False

    def start(self):
        threading.Thread(target=self._run, name="model-download", daemon=True).start()
        return self

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.kill()

    def _run(self):
        try:
            _link_models(all_models(), self.uvr)
            missing = missing_models(self.uvr)
            if missing:
                self._download(missing)
            self.state = "cancelled" if self._cancelled else "done"
            self.stage = "Cancelled" if self._cancelled else "Downloaded"
            if not self._cancelled:
                self.log("Separation models downloaded.", "success")
        except Exception as error:
            self.state, self.stage, self.error = "failed", "Failed", str(error)
            self.log(f"Downloading the separation models failed: {error}", "error")

    def _download(self, models):
        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            [str(engine_python()), str(WORKER), "--download", str(MODEL_CACHE), *models],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
            **_no_window(),
        )
        tail = []

        def read_errors():
            for raw in self._process.stderr:
                line = strip_ansi(raw.decode("utf-8", errors="replace")).strip()
                if line:
                    tail[:] = (tail + [line])[-10:]

        errors = threading.Thread(target=read_errors, daemon=True)
        errors.start()
        failure = None
        for raw in self._process.stdout:
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if event.get("event") == "step":
                size = MODEL_SIZES_MB.get(models[event["index"] - 1])
                self.stage = f"{event['label']} ({event['index']}/{len(models)}" + (f", {size} MB)" if size else ")")
                self.fraction = 0.0
            elif event.get("event") == "progress":
                self.fraction = event.get("fraction")
            elif event.get("event") == "error":
                failure = event.get("message")
        self._process.wait()
        errors.join(timeout=5)
        if self._process.returncode != 0 and not self._cancelled:
            raise SeparationError(failure or (tail[-1] if tail else f"exit code {self._process.returncode}"))


# --------------------------------------------------------------------------
# Running a separation
# --------------------------------------------------------------------------


def _link_models(models, uvr):
    """Make UVR5's copies of the preset's models available to the engine.

    Hard links take no extra space and leave UVR5's folder untouched; if
    they are not possible (another drive), the model is copied instead.
    Anything UVR5 does not have is downloaded by audio-separator itself.
    """
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    for model in models:
        source = uvr.find_model(model) if uvr else None
        if source is None:
            continue
        related = [source]
        if source.suffix == ".yaml":
            # A Demucs model is a yaml file naming one or more .th weights.
            text = source.read_text(encoding="utf-8", errors="replace")
            for signature in re.findall(r"\b([0-9a-f]{8})\b", text):
                related += source.parent.glob(f"{signature}*.th")
        for path in related:
            target = MODEL_CACHE / path.name
            if target.exists():
                continue
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)


def unique_folder(parent, name):
    folder = Path(parent) / name
    counter = 1
    while folder.exists():
        folder = Path(parent) / f"{name} ({counter})"
        counter += 1
    return folder


def separate(
    song,
    output_root,
    preset=DEFAULT_PRESET,
    audio_format="mp3",
    bitrate="192",
    cover_art=True,
    device="auto",
    quality="auto",
    uvr=None,
    reporter=None,
    on_stems=None,
):
    """Split ``song`` into stems in a new folder under ``output_root``.

    The stems are saved like the downloads are: in ``audio_format`` at
    ``bitrate``, carrying the song's tags and (with ``cover_art``) its artwork.
    ``on_stems({name: path})`` is called (on another thread) with each batch
    of stems as soon as they are saved, while later models are still running.
    Returns {stem name: path}, e.g. {"Vocals": ..., "Instrumental": ...}.
    """
    reporter = reporter or Reporter()
    if engine_info() is None:
        raise SeparationError(
            "The separation engine is not installed. Set it up from Settings (Ctrl+S)."
        )
    if preset not in PRESETS:
        raise SeparationError(f"Unknown separation preset {preset!r}.")

    # "auto" means whatever the engine was installed for.
    if device == "auto":
        device = engine_info().get("device", "cpu")
    quality = resolve_quality(quality, device)
    steps = PRESETS[preset].steps_for(quality)

    song = Path(song)
    output = unique_folder(output_root, song.stem)
    output.mkdir(parents=True)
    _link_models([step.model for step in steps], uvr)

    job = {
        "input": str(song),
        "output_dir": str(output),
        "title": song.stem,
        "model_dir": str(MODEL_CACHE),
        # Lossless while separating: in the lead/backing presets one model's
        # output is the next one's input, and lossy steps would compound.
        "format": "wav",
        "tuning": tuning(quality, device),
        "steps": [
            {"model": step.model, "source": step.source, "stems": step.stems, "merge": step.merge}
            for step in steps
        ],
    }
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if device == "cpu":
        environment["CUDA_VISIBLE_DEVICES"] = ""

    try:
        process = subprocess.Popen(
            [str(engine_python()), str(WORKER), json.dumps(job)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **_no_window(),
        )
    except OSError as error:
        raise SeparationError(f"Could not start the separation engine: {error}")

    result = {}
    problem = []
    total = len(job["steps"])
    # Stems waiting to be converted, one batch per model. Converting (and
    # importing, via on_stems) happens on its own thread, so the first
    # model's stems are ready while later models are still running.
    ready = queue.Queue()
    announced = set()  # stems the worker has handed over batch by batch
    stems = {}
    failures = []

    def read_events():
        for raw in process.stdout:
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "step":
                reporter.item(event["index"], total, event.get("label"))
                reporter.stage("Separating")
                reporter.progress(None)
            elif kind == "stage":
                reporter.stage(event["text"])
                reporter.progress(None)
            elif kind == "progress":
                reporter.progress(event.get("fraction"))
            elif kind == "log":
                reporter.log(event.get("text", ""), event.get("level", "info"))
            elif kind == "stems":
                result.update(event.get("stems", {}))
                announced.update(event.get("stems", {}))
                ready.put(event.get("stems", {}))
            elif kind == "done":
                result.update(event.get("stems", {}))
            elif kind == "error":
                problem.append(event.get("message", "unknown error"))

    def convert_batches():
        while True:
            batch = ready.get()
            if batch is None:
                return
            names = sorted(batch, key=lambda n: STEM_ORDER.index(n) if n in STEM_ORDER else 99)

            def save(name):
                path = Path(batch[name])
                try:
                    saved = convert_audio(
                        path,
                        audio_format,
                        bitrate,
                        tags_from=song,
                        title=f"{song.stem} ({name})",
                        cover_art=cover_art,
                    )
                except DownloadError as error:
                    failures.append(f"Could not save the {name} stem: {error}")
                    return None
                if saved != path:
                    path.unlink(missing_ok=True)
                return saved

            # One ffmpeg per stem, side by side: a six stem batch saves in
            # about the time one stem takes.
            with ThreadPoolExecutor(max_workers=min(len(names), 6) or 1) as pool:
                saved = dict(zip(names, pool.map(save, names)))
            converted = {name: path for name, path in saved.items() if path is not None}
            stems.update(converted)
            if converted and on_stems and not reporter.cancelled:
                try:
                    on_stems(converted)
                except Exception as error:  # an import problem must not stop separating
                    failures.append(str(error))

    tail = []

    def read_errors():
        for raw in process.stderr:
            line = strip_ansi(raw.decode("utf-8", errors="replace")).strip()
            if line:
                tail[:] = (tail + [line])[-20:]

    readers = [
        threading.Thread(target=read_events, daemon=True),
        threading.Thread(target=read_errors, daemon=True),
    ]
    converter = threading.Thread(target=convert_batches, daemon=True)
    for thread in readers + [converter]:
        thread.start()
    try:
        while process.poll() is None:
            if reporter.cancel_event.wait(0.2):
                process.kill()
                process.wait()
                raise Cancelled("Cancelled.")
    finally:
        for reader in readers:
            reader.join(timeout=5)
        # Anything reported only at the end (not batch by batch) still gets saved.
        pending = {name: path for name, path in result.items() if name not in announced}
        if pending and not reporter.cancelled and process.returncode == 0:
            ready.put(pending)
        ready.put(None)
        reporter.stage("Saving stems")
        converter.join()
        if reporter.cancelled and not stems:
            shutil.rmtree(output, ignore_errors=True)

    if process.returncode != 0 or not result:
        if not stems:
            shutil.rmtree(output, ignore_errors=True)
        message = problem[-1] if problem else (tail[-1] if tail else f"exit code {process.returncode}")
        raise SeparationError(f"Separation failed: {message}")
    if failures:
        raise SeparationError(failures[0])
    return stems
