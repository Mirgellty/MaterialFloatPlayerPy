"""Stand in for the NetEase plugin so the bridge can be tested without NetEase.

    .venv\\Scripts\\python.exe tools\\fake_plugin.py
    # in another window
    .venv\\Scripts\\python.exe mfp.py --bridge "%TEMP%\\mfp-bridge.json"

Writes the same files the plugin writes (state + lyrics), advances the position
like a player would, and prints every command the widget sends back. That makes
the whole plugin -> widget -> plugin loop verifiable offline.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mfp_py.demo import DEMO_TRACKS, demo_lyrics, render_cover  # noqa: E402

MODE_KINDS = ("repeat", "one", "random", "order", "ai")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=str(Path(tempfile.gettempdir()) / "mfp-bridge.json"),
        help="state file to write (same path the widget is given with --bridge)",
    )
    parser.add_argument("--track", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")
    parser.add_argument("--mute", action="store_true", help="accepted for symmetry (no audio)")
    parser.add_argument("--cover", choices=("local", "url", "none"), default="local")
    args = parser.parse_args()

    base = Path(args.state)
    lyric_path = Path(str(base) + ".lyric")
    cmd_path = Path(str(base) + ".cmd")
    ctl_path = Path(str(base) + ".ctl")
    beat_path = Path(str(base) + ".beat")
    cover_file = base.with_name("mfp-bridge-cover.png")
    cover_file.write_bytes(render_cover(args.track))
    cover_url = {
        "local": str(cover_file),
        "url": "https://p1.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg",
        "none": "",
    }[args.cover]

    print(f"fake plugin writing {base}  (Ctrl+C to stop)")
    started = time.time()
    position = 4.0
    playing = True
    mode_index = 0
    index = args.track
    # ignore commands left over from an earlier run, like the real plugin does
    try:
        last_cmd_seq = int(json.loads(cmd_path.read_text(encoding="utf-8")).get("seq") or 0)
    except (OSError, ValueError):
        last_cmd_seq = 0
    last_beat = 0.0
    last_tick = time.time()

    try:
        while True:
            now = time.time()
            if playing:
                position += now - last_tick
            last_tick = now
            title, artists, album, duration = DEMO_TRACKS[index % len(DEMO_TRACKS)]
            key = f"ncm:{1000 + index}"
            while position >= duration:
                position -= duration
                index += 1

            base.write_text(
                json.dumps(
                    {
                        "v": 1,
                        "ts": now * 1000,
                        "app": "cloudmusic.exe",
                        "track": {
                            "key": key,
                            "id": 1000 + index,
                            "title": title,
                            "artists": artists,
                            "album": album,
                            "duration": duration,
                            "cover": cover_url,
                        },
                        "playing": playing,
                        "position": position,
                        "canToggle": True,
                        "canNext": True,
                        "canPrev": True,
                        "canMode": True,
                        "modeKind": MODE_KINDS[mode_index % len(MODE_KINDS)],
                        "repeat": "list",
                        "shuffle": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            lyric_path.write_text(
                json.dumps(
                    {
                        "key": key,
                        "rev": index + 1,
                        "lines": demo_lyrics(f"demo:{index % len(DEMO_TRACKS)}") or [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # commands from the widget
            try:
                cmd = json.loads(cmd_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cmd = None
            if isinstance(cmd, dict) and int(cmd.get("seq") or 0) > last_cmd_seq:
                last_cmd_seq = int(cmd["seq"])
                action = str(cmd.get("action") or "")
                print(f"  <- command: {action}")
                if action == "toggle":
                    playing = not playing
                elif action == "play":
                    playing = True
                elif action == "pause":
                    playing = False
                elif action in ("next", "prev"):
                    index += 1 if action == "next" else -1
                    position = 0.0
                elif action == "mode":
                    mode_index += 1
                elif action == "close":
                    print("  <- widget hid itself")

            # heartbeat from the widget, so this side can act like the supervisor
            try:
                beat = json.loads(beat_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                beat = None
            if isinstance(beat, dict):
                stamp = float(beat.get("ts") or 0) / 1000
                if stamp > last_beat:
                    if last_beat == 0.0 and not ctl_path.exists():
                        ctl_path.write_text(
                            json.dumps({"seq": 1, "action": "show"}), encoding="utf-8"
                        )
                        print("  -> ctl: show")
                    last_beat = stamp

            if args.seconds and now - started >= args.seconds:
                return 0
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        print("fake plugin stopped")


if __name__ == "__main__":
    raise SystemExit(main())
