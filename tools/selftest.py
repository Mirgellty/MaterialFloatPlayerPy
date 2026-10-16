"""Integration self-test: does the widget actually read and drive a player?

    # system media session (needs tools/fake_player.py in another window)
    .venv\\Scripts\\python.exe tools\\selftest.py

    # NetEase plugin bridge (needs tools/fake_plugin.py in another window)
    .venv\\Scripts\\python.exe tools\\selftest.py --bridge "%TEMP%\\mfp-bridge.json"

Uses the real widgets and the real sources: the track is read, clicking the play
button really changes the player (or writes the command the plugin would act on),
and dragging the card persists a new position. Exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mfp_py.app import FloatingPlayer  # noqa: E402
from mfp_py.config import Config  # noqa: E402
from mfp_py import win  # noqa: E402

TIMEOUT = 12.0


def wait_for(predicate, timeout: float = TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            self.failures += 1


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", metavar="PATH", help="test the plugin bridge instead of SMTC")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    config = Config(path=Path(tempfile.mkdtemp(prefix="mfp-selftest-")) / "config.json")
    if args.bridge:
        config.update({"source": "bridge", "bridgePath": args.bridge})
    else:
        config.update({"source": "system", "alwaysOnTop": True})

    player = FloatingPlayer(app, config, None)
    player.show()
    report = Report()
    card = player.card
    command_path = Path(str(args.bridge) + ".cmd") if args.bridge else None

    ok = wait_for(lambda: player.source.snapshot().track is not None)
    snapshot = player.source.snapshot()
    report.check(
        "player state is read",
        ok,
        f"{snapshot.app} / {snapshot.track.title if snapshot.track else '-'}"
        + (f" ({player.source.last_error()})" if player.source.last_error() else ""),
    )
    if not ok:
        return 1

    card.set_snapshot(snapshot)
    card.advance_animations()  # the painter normally does this on its own tick
    report.check("card received the track", card._title_text == snapshot.track.title)
    report.check(
        "window never takes focus (fullscreen games keep the foreground)",
        win.is_non_activating(int(player.window.winId())),
        "WS_EX_NOACTIVATE",
    )
    report.check(
        "lyrics arrived",
        wait_for(lambda: bool(card.lyric.lines), timeout=6.0),
        f"{len(card.lyric.lines)} lines",
    )

    # click the play/pause button through the real hit test
    buttons, _, _ = card._buttons(card._layout())
    toggle = next(button for button in buttons if button.key == "toggle")
    before = player.source.snapshot().playing
    if command_path is not None:
        command_path.unlink(missing_ok=True)
    QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, toggle.rect.center().toPoint())
    if command_path is None:
        flipped = wait_for(lambda: player.source.snapshot().playing != before, timeout=4.0)
        report.check(
            "clicking play/pause flips the player",
            flipped,
            f"playing {before} -> {player.source.snapshot().playing}",
        )
    else:
        sent = wait_for(lambda: read_json(command_path).get("action") == "toggle", timeout=4.0)
        report.check(
            "clicking play/pause asks the plugin to toggle",
            sent,
            f"wrote {read_json(command_path).get('action')}",
        )

    # drag the card and make sure the new position is remembered
    start = QPoint(int(card.width() * 0.6), int(card.height() * 0.15))
    origin = player.window.pos()
    QTest.mousePress(card, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(card, start + QPoint(37, 23))
    QTest.mouseRelease(card, Qt.LeftButton, Qt.NoModifier, start + QPoint(37, 23))
    QApplication.processEvents()
    moved = player.window.pos() - origin
    saved = config.get("pos") or {}
    report.check(
        "drag moves the window",
        (moved.x(), moved.y()) == (37, 23),
        f"moved by ({moved.x()}, {moved.y()})",
    )
    report.check(
        "drag is remembered",
        saved.get("x") == player.window.x() and saved.get("y") == player.window.y(),
        f"saved {saved}",
    )

    player.shutdown()
    print("FAILURES:", report.failures)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
