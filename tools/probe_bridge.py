"""Show what the widget sees on the plugin bridge, without opening a window.

    .venv\\Scripts\\python.exe tools\\probe_bridge.py [--bridge PATH]

Prints the state the plugin published, the lyrics it published, what the bridge
source makes of both, and what colour the cover yields. Safe to run while the
widget is live: it never creates a window and never takes the instance lock.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from mfp_py import color as colors  # noqa: E402
from mfp_py.bridge import BridgeSource  # noqa: E402


def show_raw(base: Path) -> None:
    print("--- raw files ---")
    for suffix in ("", ".lyric", ".cmd", ".ctl", ".beat"):
        path = Path(str(base) + suffix)
        try:
            stat = path.stat()
        except OSError:
            print(f"{path.name:24s} (missing)")
            continue
        age = time.time() - stat.st_mtime
        print(f"{path.name:24s} {stat.st_size:7d} bytes  {age:6.1f}s ago")
    try:
        state = json.loads(Path(str(base)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("state: unreadable")
        return
    print(f"state ts        : {time.time() * 1000 - float(state.get('ts') or 0):.0f}ms ago")
    print(f"state track     : {state.get('track')}")
    print(f"state playing   : {state.get('playing')} position={state.get('position')}")
    print(f"state modeKind  : {state.get('modeKind')}")
    try:
        lyric = json.loads(Path(str(base) + ".lyric").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("lyric: unreadable")
        return
    lines = lyric.get("lines") or []
    print(f"lyric key       : {lyric.get('key')!r} rev={lyric.get('rev')} lines={len(lines)}")
    if lines:
        print(f"lyric first     : {lines[0]}")
        print(f"lyric last      : {lines[-1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", default="C:/betterncm/mfp-bridge.json")
    parser.add_argument(
        "--app",
        action="store_true",
        help="also drive the real app object a few ticks and dump the card state",
    )
    args = parser.parse_args()
    base = Path(args.bridge)

    show_raw(base)

    print("--- through the bridge source ---")
    source = BridgeSource(str(base))
    snapshot = source.snapshot()
    track = snapshot.track
    if track is None:
        print("track: none (idle)")
    else:
        print(f"key       : {track.key!r}")
        print(f"title     : {track.title!r} / {track.artists!r}")
        print(f"duration  : {track.duration}")
        print(f"cover     : {'None' if not track.cover else str(len(track.cover)) + ' bytes'}")
    print(f"playing   : {snapshot.playing} position={snapshot.position:.2f}")
    print(f"mode      : {snapshot.mode_kind!r}")

    payload = source.lyrics()
    if payload is None:
        print("lyrics    : None (file missing or unreadable)")
    else:
        print(
            f"lyrics    : key={payload.key!r} rev={payload.rev} "
            f"lines={len(payload.lines)} none={payload.none}"
        )
        if payload.lines:
            print(f"  first   : {payload.lines[0]}")

    # the widget only accepts lyrics whose key matches the track it is showing
    if payload is not None and track is not None and payload.key != track.key:
        print("!! lyric key does not match the track key: the card will not show them")

    if track is not None and track.cover:
        with Image.open(io.BytesIO(track.cover)) as image:
            rgb = colors.dominant_color(image)
        palette = colors.build_palette(rgb, keep_surface=False, light=False)
        print(f"cover rgb : {colors.rgb_to_hex(rgb)} -> surface {palette.color}")
    if args.app:
        dump_app(base)
    return 0


def dump_app(base: Path) -> None:
    """Tick the real FloatingPlayer (no window shown, lock skipped) and report."""
    import tempfile

    from PySide6.QtWidgets import QApplication

    from mfp_py.app import FloatingPlayer
    from mfp_py.config import Config

    app = QApplication(sys.argv[:1])
    config = Config(path=Path(tempfile.mkdtemp(prefix="mfp-probe-")) / "config.json")
    config.update({"source": "bridge", "bridgePath": str(base)})
    player = FloatingPlayer(app, config, None)
    player._tick()
    for _ in range(12):            # give the cover fetch/palette worker time
        app.processEvents()
        time.sleep(0.25)
        player._tick()
        snapshot = player.source.snapshot()
        print(
            f"  tick: source={player.source.name} "
            f"track={(snapshot.track.key if snapshot.track else None)!r} "
            f"key={player.track_key!r} palette_key={player._palette_key!r} "
            f"rgb={player.album_rgb} lyric_lines={len(player.card.lyric.lines)}"
        )
    card = player.card
    print("--- app state ---")
    print(f"track_key    : {player.track_key!r}")
    print(f"palette_key  : {player._palette_key!r}")
    print(f"album_rgb    : {player.album_rgb}")
    print(f"lyric_rev    : {player._lyric_rev!r}")
    print(f"card lyric   : {len(card.lyric.lines)} lines, key={card.lyric.key!r}")
    print(f"card line    : {card._line_text!r}")
    print(f"card palette : {card.palette.color} accent={card.palette.accent}")
    print(f"toggle label : {player.source.snapshot().mode_kind!r}")


if __name__ == "__main__":
    raise SystemExit(main())
