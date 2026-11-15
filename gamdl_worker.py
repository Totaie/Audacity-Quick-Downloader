"""Run gamdl's ``rip`` command and report its progress as JSON lines.

gamdl 4 draws its progress with Rich live bars. Those never reach a pipe,
because they are transient and only drawn on a terminal. This script runs
gamdl's own command line in place of ``gamdl rip`` and swaps in a progress
display that, as well as doing what gamdl's does, prints one line per event
for applemusicdownloader.py to read:

    @@gamdl {"type": "track", "index": 1, "title": "Song"}
    @@gamdl {"type": "stage", "index": 1, "stage": "downloading"}
    @@gamdl {"type": "progress", "index": 1, "percent": 42.0}
    @@gamdl {"type": "finished", "index": 1, "status": "downloaded", ...}
    @@gamdl {"type": "discovered", "count": 12}

Its progress hooks are internal to gamdl. If a later release moves them,
the download still runs as plain ``gamdl rip``, just without these lines.

Usage: python gamdl_worker.py [gamdl rip options] URL
"""

import json
import sys

PREFIX = "@@gamdl "


def emit(**event):
    print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def install_reporting_progress():
    """Swap gamdl's progress display for one that also prints events."""
    try:
        from gamdl.cli.commands import runner
        from gamdl.download import DownloadStage, EventState
    except ImportError:
        return False
    base = getattr(runner, "RipProgress", None)
    if base is None:
        return False

    class ReportingProgress(base):
        def discovered(self, count, *args, **kwargs):
            emit(type="discovered", count=count, final=False)
            return super().discovered(count, *args, **kwargs)

        def discovery_finished(self, count=0, *args, **kwargs):
            if count:
                emit(type="discovered", count=count, final=True)
            return super().discovery_finished(count, *args, **kwargs)

        def add(self, occurrence, title, *args, **kwargs):
            emit(type="track", index=occurrence, title=title)
            return super().add(occurrence, title, *args, **kwargs)

        def event(self, occurrence, title, event, *args, **kwargs):
            try:
                if event.state == EventState.PROGRESS and event.progress is not None:
                    emit(
                        type="progress",
                        index=occurrence,
                        track=event.track,
                        percent=event.progress.percentage,
                    )
                elif (
                    event.state == EventState.STARTED
                    and event.stage != DownloadStage.COMPLETED
                ):
                    emit(type="stage", index=occurrence, stage=event.stage.value)
            except Exception:  # reporting must never break a download
                pass
            return super().event(occurrence, title, event, *args, **kwargs)

        def finish(self, occurrence, title, status, message=None, *args, **kwargs):
            path = kwargs.get("path")
            emit(
                type="finished",
                index=occurrence,
                title=title,
                status=status,
                message=message,
                path=str(path) if path is not None else None,
            )
            return super().finish(occurrence, title, status, message, *args, **kwargs)

    runner.RipProgress = ReportingProgress
    return True


def main(argv):
    # The parent decodes the pipe as UTF-8; Windows would otherwise use the
    # console code page and mangle non-English titles.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    install_reporting_progress()
    from gamdl.cli.main import main as gamdl_main

    # --no-config keeps the user's own ~/.gamdl/config.yml (and the default
    # one gamdl writes on first run) from changing what gets downloaded.
    gamdl_main(["--no-config", "rip", *argv], prog_name="gamdl")


if __name__ == "__main__":
    main(sys.argv[1:])
