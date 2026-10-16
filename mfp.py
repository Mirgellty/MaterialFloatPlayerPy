"""Launcher for when the widget is started from somewhere else.

The BetterNCM plugin runs this, so it must work with a bare interpreter and any
working directory:

    pythonw "C:\\...\\floating window\\mfp.py" --bridge "C:/betterncm/mfp-bridge.json"

Everything else - the window, the palette, the animations - lives in mfp_py/.
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def enable_crash_log() -> None:
    """Record native crashes and unhandled exceptions in the data folder.

    A window that vanishes without a trace is impossible to debug otherwise: the
    process died twice during testing with nothing in the log because Python never
    got a chance to report anything.
    """
    try:
        from mfp_py.config import data_dir

        handle = open(data_dir() / "crash.log", "a", buffering=1, encoding="utf-8")
        handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start pid={os.getpid()} "
                     f"argv={sys.argv[1:]} ---\n")
        faulthandler.enable(handle, all_threads=True)

        def hook(kind, value, tb):
            import traceback

            traceback.print_exception(kind, value, tb, file=handle)
            sys.__excepthook__(kind, value, tb)

        sys.excepthook = hook
    except Exception:  # noqa: BLE001 - never let logging break startup
        pass


def early_beat(argv: list[str]) -> None:
    """Say hello before Qt is even imported.

    Loading Qt takes a couple of seconds; the plugin supervises us through the
    heartbeat file, so stamping it here means it knows the launch worked instead
    of launching a second copy (which would fight over the single-instance lock).
    """
    for index, arg in enumerate(argv):
        if arg == "--bridge" and index + 1 < len(argv):
            try:
                Path(str(argv[index + 1]) + ".beat").write_text(
                    json.dumps(
                        {
                            "ts": time.time() * 1000,
                            "pid": os.getpid(),
                            "source": "bridge",
                            "phase": "starting",
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
            return


from mfp_py.app import main  # noqa: E402

if __name__ == "__main__":
    enable_crash_log()
    early_beat(sys.argv[1:])
    raise SystemExit(main())
