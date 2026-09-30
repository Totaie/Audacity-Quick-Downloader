"""Runs one separation inside the engine's own virtual environment.

This is started by separation.py with .venv-separator's Python, never
imported by the app itself (the app's environment has no PyTorch). It reads
a JSON job from its first argument and reports back with one JSON object per
line on stdout:

    {"event": "step", "index": 1, "label": "UVR-MDX-NET-Inst_HQ_3"}
    {"event": "progress", "fraction": 0.42}
    {"event": "stems", "stems": {"Drums": "C:/.../Song (Drums).wav", ...}}
    {"event": "done", "stems": {"Vocals": "C:/.../Song (Vocals).wav", ...}}
    {"event": "error", "message": "..."}

Run with --check to confirm the engine loads, and see whether a GPU is used,
or with --download <model folder> <model>... to fetch models ahead of time.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

# Everything else audio-separator prints goes to stderr, which the app only
# reads to explain a failure. stdout is reserved for the events above.
_events = sys.stdout
sys.stdout = sys.stderr


def emit(event, **data):
    _events.write(json.dumps({"event": event, **data}) + "\n")
    _events.flush()


def patch_progress_bars():
    """Turn the library's tqdm progress bars into progress events."""
    import tqdm
    import tqdm.auto

    class Reporting(tqdm.tqdm):
        _last = 0.0

        def __init__(self, *args, **kwargs):
            kwargs["file"] = open(os.devnull, "w")
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            result = super().update(n)
            now = time.monotonic()
            if self.total and (now - Reporting._last > 0.2 or self.n >= self.total):
                Reporting._last = now
                emit("progress", fraction=min(self.n / self.total, 1.0))
            return result

    tqdm.tqdm = Reporting
    tqdm.auto.tqdm = Reporting


def check():
    import torch
    from importlib.metadata import version

    import onnxruntime

    emit(
        "check",
        audio_separator=version("audio-separator"),
        torch=torch.__version__,
        cuda=torch.cuda.is_available(),
        gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        onnx_cuda=onnx_runs_on_cuda(onnxruntime),
    )


def onnx_runs_on_cuda(onnxruntime):
    """Whether ONNX Runtime can really use CUDA (it lists the provider even
    when the CUDA libraries it needs cannot be loaded)."""
    if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
        return False
    import onnx
    from onnx import TensorProto, helper

    graph = helper.make_graph(
        [helper.make_node("Identity", ["x"], ["y"])],
        "check",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    try:
        session = onnxruntime.InferenceSession(
            model.SerializeToString(), providers=["CUDAExecutionProvider"]
        )
    except Exception:
        return False
    return session.get_providers()[0] == "CUDAExecutionProvider"


def run(job):
    patch_progress_bars()
    from audio_separator.separator import Separator

    output_dir = Path(job["output_dir"])
    tuning = job.get("tuning") or {}
    separator = Separator(
        log_level=logging.WARNING,
        model_file_dir=job["model_dir"],
        output_dir=str(output_dir),
        output_format=job["format"].upper(),
        # Roformer models: how many overlapping windows each moment is
        # predicted in (None keeps the model's own setting).
        mdxc_params={
            "segment_size": 256,
            "override_model_segment_size": False,
            "batch_size": None,
            "overlap": tuning.get("overlap"),
            "pitch_shift": 0,
        },
        use_autocast=bool(tuning.get("autocast")),
    )

    # Every stem made so far, by our name. "song" is the downloaded file.
    stems = {"song": Path(job["input"])}
    for index, step in enumerate(job["steps"], 1):
        model = step["model"]
        emit("step", index=index, label=Path(model).stem)
        first_use = not (Path(job["model_dir"]) / model).exists()
        emit("stage", text="Downloading model (first use only)" if first_use else "Loading model")
        separator.load_model(model_filename=model)
        emit("stage", text="Separating")

        # audio-separator "sanitises" names it is given (collapsing underscores,
        # trimming dots...), so it writes plain names like "step1-Vocals" that
        # survive that untouched, and the files are renamed afterwards.
        names = {
            model_stem: f"step{index}-{ours.lstrip('_')}"
            for model_stem, ours in step["stems"].items()
        }
        files = separator.separate(str(stems[step["source"]]), custom_output_names=names)

        made = {}
        for file in files:
            path = Path(file)
            if not path.is_absolute():
                path = output_dir / path
            for model_stem, name in names.items():
                if path.stem == name:
                    ours = step["stems"][model_stem]
                    if not ours.startswith("_"):
                        # The final name, e.g. "Song (Vocals).wav".
                        final = path.with_name(f"{job['title']} ({ours}){path.suffix}")
                        path = path.replace(final)
                    made[ours] = path
        missing = set(step["stems"].values()) - set(made)
        if missing:
            raise RuntimeError(
                f"{Path(model).stem} did not produce {', '.join(sorted(missing))} "
                f"(got {', '.join(Path(f).name for f in files) or 'nothing'})"
            )
        stems.update(made)

        # Some presets fold stems together, e.g. guitar + piano + other.
        for ours, parts in (step.get("merge") or {}).items():
            stems[ours] = merge([stems[part] for part in parts], output_dir / f"{job['title']} ({ours}).wav")

        # Hand over the stems this model finished, so the app can save and
        # import them while the next model runs. They are not touched again.
        finished = [ours for ours in list(step["stems"].values()) + list(step.get("merge") or {})
                    if not ours.startswith("_")]
        if finished:
            emit("stems", stems={ours: str(stems[ours]) for ours in finished})

    # Throw away the intermediate files later steps used as their input.
    for name, path in list(stems.items()):
        if name.startswith("_"):
            path.unlink(missing_ok=True)
            del stems[name]
    del stems["song"]
    emit("done", stems={name: str(path) for name, path in stems.items()})


def merge(paths, destination):
    """Add audio files together (stems sum back to what they were split from).

    Written as 32-bit float so the sum is exact, and stays level with the
    other stems, whatever format it is converted to afterwards.
    """
    import soundfile

    total, rate = None, None
    for path in paths:
        audio, rate = soundfile.read(path, dtype="float32", always_2d=True)
        if total is None:
            total = audio
        else:
            length = min(len(total), len(audio))
            total = total[:length] + audio[:length]
    soundfile.write(destination, total, rate, subtype="FLOAT")
    return destination


def download(model_dir, models):
    """Fetch models ahead of time, without loading them."""
    patch_progress_bars()
    from audio_separator.separator import Separator

    separator = Separator(log_level=logging.WARNING, model_file_dir=model_dir)
    for index, model in enumerate(models, 1):
        emit("step", index=index, label=Path(model).stem)
        separator.download_model_and_data(model)
    emit("done", stems={})


def main():
    if sys.argv[1:] == ["--check"]:
        check()
        return 0
    try:
        if sys.argv[1] == "--download":
            download(sys.argv[2], sys.argv[3:])
        else:
            run(json.loads(sys.argv[1]))
    except Exception as error:
        emit("error", message=f"{type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
