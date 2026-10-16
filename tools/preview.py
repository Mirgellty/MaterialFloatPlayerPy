"""Render the card in every visual variant and write a contact sheet.

    .venv\\Scripts\\python.exe tools\\preview.py [--out DIR]

Drives the real Card/CardWindow widgets with fabricated snapshots, so it checks
the actual drawing code (dark/light, immersive, square corners, narrow/wide,
translucency, the long-title marquee mid-glide, paused, nothing playing) without
needing a player. Also handy as the project's visual regression sheet.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mfp_py import color as colors, win  # noqa: E402
from mfp_py.config import DEFAULTS, Config  # noqa: E402
from mfp_py.demo import render_cover  # noqa: E402
from mfp_py.media import Snapshot, Track  # noqa: E402
from mfp_py.ui.card import LyricLine, LyricState  # noqa: E402
from mfp_py.ui.settings import SettingsDialog  # noqa: E402
from mfp_py.ui.window import CardWindow  # noqa: E402

LONG_TITLE = "Sippin' through dreamland like a tourist with a very long song title"
LONG_LINE = "And every color's louder in my head than it has ever been before tonight"

LYRICS = LyricState(
    key="preview",
    lines=(
        LyricLine(time=0, text="Sippin' through dreamland like a tourist", translation="像游客一样在梦境里漫步", duration=9000),
        LyricLine(time=9000, text="I'm on my way to somewhere I don't know", translation="我正前往一个我不知道的地方", duration=8000),
    ),
)

LONG_LYRICS = LyricState(
    key="preview-long",
    lines=(LyricLine(time=0, text=LONG_LINE, translation="每一种颜色在脑海里都更响亮", duration=12000),),
)


def track(title: str = "Dreamland", duration: float = 202.0, seed: int = 0) -> Track:
    return Track(
        key=f"preview:{title}",
        title=title,
        artists="Glass Animals",
        album="Dreamland",
        duration=duration,
        cover=render_cover(seed),
    )


def snapshot(
    title: str = "Dreamland",
    position: float = 8.0,
    playing: bool = True,
    seed: int = 0,
) -> Snapshot:
    return Snapshot(
        track=track(title, seed=seed),
        playing=playing,
        position=position,
        sampled_at=time.monotonic(),
        repeat="list",
        shuffle=False,
        app="网易云音乐",
        source="system",
    )


VARIANTS = (
    ("01-dark", {}, snapshot(), LYRICS, 700, 0.35),
    ("02-light", {"light": True}, snapshot(), LYRICS, 700, 0.35),
    ("03-zen", {"zen": True}, snapshot(position=48.0), LYRICS, 700, 0.35),
    ("04-square", {"radius": 0}, snapshot(), LYRICS, 700, 0.35),
    ("05-narrow", {"width": 260, "fontSize": 11}, snapshot(), LYRICS, 700, 0.35),
    ("06-wide", {"width": 720, "fontSize": 17}, snapshot(), LYRICS, 700, 0.35),
    ("07-translucent", {"opacity": 0.6}, snapshot(), LYRICS, 700, 0.35),
    ("08-long-title", {}, snapshot(LONG_TITLE), LONG_LYRICS, 5400, 0.35),
    # catch a colour crossfade half-way: it must be a mix of the two album colours
    # and never a third one (this is what a track change looks like mid-transition)
    ("11-crossfade-mid", {}, snapshot("Space Ghost Coast To Coast", seed=3), LYRICS, 320, 0.35),
    ("12-crossfade-end", {}, snapshot("Space Ghost Coast To Coast", seed=3), LYRICS, 1200, 0.35),
    ("09-paused", {}, snapshot(playing=False), LYRICS, 700, 0.35),
    ("10-idle", {}, Snapshot(track=None, playing=False, position=0.0), LyricState(none=True), 700, 0.35),
    # 五个字号互不影响：这里把它们明显拉开
    (
        "13-font-sizes",
        {
            "fontTitle": 26,
            "fontArtist": 13,
            "fontCurrent": 19,
            "fontLyric": 12,
            "fontTranslation": 17,
        },
        snapshot(),
        LYRICS,
        700,
        0.35,
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="preview", help="output folder for the PNGs")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    win.set_dpi_awareness()
    app = QApplication(sys.argv[:1])
    # a throwaway config: variants must not leak settings into each other, and the
    # real one in %APPDATA% must not be touched
    config = Config(path=Path(tempfile.mkdtemp(prefix="mfp-preview-")) / "config.json")

    window = CardWindow(config)
    window.show()
    shots: list[tuple[str, Path]] = []
    queue = list(VARIANTS)
    state = {"settings_done": False}

    def step() -> None:
        if not queue:
            if not state["settings_done"]:
                state["settings_done"] = True
                dialog = SettingsDialog(config)
                dialog.show()
                dialog.adjustSize()
                QTimer.singleShot(
                    500,
                    lambda: (
                        dialog.grab().save(str(out / "20-settings.png")),
                        shots.append(("20-settings", out / "20-settings.png")),
                        dialog.close(),
                        window.hide(),
                        sheet(out, shots),
                        app.quit(),
                    ),
                )
            else:
                app.quit()
            return
        name, patch, snap, lyric, delay_ms, _ = queue.pop(0)
        config.update({**DEFAULTS, **patch})
        window.apply_config(config, reposition=True)
        rgb = None
        if snap.track and snap.track.cover:
            with Image.open(io.BytesIO(snap.track.cover)) as cover:
                rgb = colors.dominant_color(cover)
        window.card.set_palette(
            colors.build_palette(
                rgb or colors.hex_to_rgb(colors.DEFAULT_ALBUM_COLOR),
                False,
                bool(patch.get("light")),
            )
        )
        window.card.set_snapshot(snap)
        window.card.set_lyric(lyric)
        window.card.advance_animations()
        # 08 waits out the marquee's opening hold; 11 lands mid colour-crossfade
        QTimer.singleShot(delay_ms, lambda: grab(name))

    def grab(name: str) -> None:
        window.card.advance_animations()
        path = out / f"{name}.png"
        window.grab().save(str(path))
        shots.append((name, path))
        print(f"  {name}: {path}")
        step()

    QTimer.singleShot(900, step)
    code = app.exec()
    print(f"contact sheet: {out / 'sheet.png'}")
    return code


def sheet(out: Path, shots: list[tuple[str, Path]]) -> None:
    """Stack the variants into one image, so a whole run can be reviewed at a glance."""
    from PIL import Image, ImageDraw

    if not shots:
        return
    images = [Image.open(path).convert("RGB") for _, path in shots]
    width = max(image.width for image in images)
    height = sum(image.height + 34 for image in images)
    canvas = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)
    y = 0
    for (name, _), image in zip(shots, images, strict=True):
        draw.text((10, y + 10), name, fill=(230, 230, 235))
        y += 30
        canvas.paste(image, (0, y))
        y += image.height + 4
    canvas.save(out / "sheet.png")


if __name__ == "__main__":
    raise SystemExit(main())
