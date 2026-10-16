"""Launcher for when the widget is started from somewhere else.

The BetterNCM plugin runs this, so it must work with a bare interpreter and any
working directory:

    pythonw "C:\\...\\floating window\\mfp.py" --bridge "C:/betterncm/mfp-bridge.json"

Everything else - the window, the palette, the animations - lives in mfp_py/.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


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
    early_beat(sys.argv[1:])
    raise SystemExit(main())
