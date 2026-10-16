"""Diagnostic: what the widget sees coming out of the Windows media session.

Run with the project venv:

    .venv\\Scripts\\python.exe tools\\probe_smtc.py
    .venv\\Scripts\\python.exe tools\\probe_smtc.py --command next

Useful when the floating window shows nothing, or when checking that a player
really accepts commands: it answers "does the OS see the player at all" before
any of the widget's own drawing gets involved. Pair it with tools/fake_player.py
to test without a real player running.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mfp_py.smtc import SystemMediaSource  # noqa: E402


def report(snapshot) -> None:
    track = snapshot.track
    print(f"app            : {snapshot.app or '-'}  (source: {snapshot.source})")
    if track is None:
        print("track          : (nothing playing)")
    else:
        print(f"title          : {track.title}")
        print(f"artists        : {track.artists}")
        print(f"album          : {track.album}")
        print(f"duration       : {track.duration:.1f}s")
        print(f"cover          : {len(track.cover) if track.cover else 0} bytes")
    print(f"playing        : {snapshot.playing}")
    print(f"position       : {snapshot.live_position():.2f}s")
    print(f"repeat/shuffle : {snapshot.repeat} / {snapshot.shuffle}")
    print(
        "controls       : "
        f"toggle={snapshot.can_toggle} next={snapshot.can_next} "
        f"prev={snapshot.can_prev} mode={snapshot.can_mode}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="", help="toggle | next | prev | mode")
    parser.add_argument("--watch", type=float, default=0.0, help="keep printing for N seconds")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    source = SystemMediaSource(interval=0.3)
    source.start()
    try:
        time.sleep(1.2)
        report(source.snapshot())
        if source.last_error():
            print("last error     :", source.last_error())
        if args.command:
            print(f"-> command: {args.command}")
            source.command(args.command)
            time.sleep(1.5)
            report(source.snapshot())
        deadline = time.monotonic() + args.watch
        while args.watch and time.monotonic() < deadline:
            time.sleep(args.interval)
            snapshot = source.snapshot()
            print(
                f"[{time.strftime('%H:%M:%S')}] {snapshot.playing} "
                f"{snapshot.live_position():6.2f}s "
                f"{(snapshot.track.title if snapshot.track else '-')}"
            )
    finally:
        source.stop()


if __name__ == "__main__":
    main()
