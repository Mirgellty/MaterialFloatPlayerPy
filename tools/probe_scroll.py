"""Measure how smoothly a scrolling line actually moves.

    .venv\\Scripts\\python.exe tools\\probe_scroll.py

The marquee is slow (a few pixels per second), so any per-frame snapping or
resampling shows up as a stutter. This drives the card's own text drawing with a
range of sub-pixel offsets and reports the ink's horizontal centre of mass per
step: a smooth implementation increases it by the offset step every time, a
jittery one plateaus and then jumps.

Run it after touching the marquee / fade code - it is the regression test for
"歌词滚动有点抖".
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mfp_py.config import Config  # noqa: E402
from mfp_py.ui.card import Card, font  # noqa: E402

TEXT = "Sippin' through dreamland like a tourist"
STEPS = 24
STEP_PX = 0.25


def column_profile(image: QImage) -> list[float]:
    """Per-column ink amount - the signal the marquee shifts."""
    return [
        float(sum((image.pixel(x, y) >> 24) & 0xFF for y in range(image.height())))
        for x in range(image.width())
    ]


def best_shift(first: QImage, second: QImage, margin: int = 20) -> float:
    """How far the ink moved between two frames, in *device* pixels (positive = left).

    Cross-correlating the column profiles is robust here: the marquee clips ink at
    the edges as it slides, which would ruin a centre-of-mass measurement.
    """
    a, b = column_profile(first), column_profile(second)
    best_shift_value, best_err = 0.0, None
    for step in range(-24, 25):        # -6 .. +6 device px in 0.25 steps
        shift = step * 0.25
        lo = max(margin, margin - step)
        hi = min(len(a), len(b)) - margin
        err = 0.0
        count = 0
        for x in range(lo, hi):
            position = x + shift
            index = int(position)
            if index < 0 or index + 1 >= len(b):
                continue
            fraction = position - index
            value = b[index] * (1.0 - fraction) + b[index + 1] * fraction
            err += (a[x] - value) ** 2
            count += 1
        if not count:
            continue
        err /= count
        if best_err is None or err < best_err:
            best_shift_value, best_err = shift, err
    # a frame that moved left matches the previous one at a *positive* sample
    # offset, so report the displacement (left-hand motion is positive here)
    return -best_shift_value


def main() -> int:
    app = QApplication(sys.argv[:1])
    card = Card(Config())
    card.resize(384, 134)
    ratio = card.devicePixelRatioF()

    text_font = font(13)
    # the canvas has to be wider than the text, otherwise the ink gets clipped and
    # the centre of mass moves for the wrong reason
    from PySide6.QtGui import QFontMetricsF  # noqa: E402

    width = QFontMetricsF(text_font).horizontalAdvance(TEXT) + 60
    height = 20.0
    base = QRectF(0, 0, width, height)

    ok = True
    # 滚动中不带渐隐、滚动中带渐隐：两条路径都必须每步移动同样多
    for label, marquee, fade in (("滚动", True, False), ("滚动+渐隐", True, True)):
        frames: list[QImage] = []
        for step in range(STEPS):
            offset = step * STEP_PX
            image = QImage(
                max(1, int(round(width * ratio))),
                max(1, int(round(height * ratio))),
                QImage.Format_ARGB32_Premultiplied,
            )
            image.fill(Qt.transparent)
            # the outer canvas must carry the same scale as the screen, otherwise
            # the measurement itself resamples the drawing and shows fake jitter
            image.setDevicePixelRatio(ratio)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            # the card's own drawing code, on a transparent canvas
            card._draw_line(
                painter, base, TEXT, text_font, QColor(255, 255, 255), offset, fade, marquee
            )
            painter.end()
            frames.append(image)

        # how far did the ink actually move between frames, in logical pixels?
        deltas = [best_shift(a, b) / ratio for a, b in zip(frames, frames[1:])]
        stuck = sum(1 for a, b in zip(frames, frames[1:]) if a == b)
        mean = statistics.mean(deltas)
        stdev = statistics.stdev(deltas)
        smooth = (
            abs(mean - STEP_PX) < 0.06          # moves the right distance
            and stdev < 0.06                     # every step the same
            and min(deltas) > 0                   # always forward, never back
            and stuck == 0                        # no frame is a copy of the previous
        )
        ok = ok and smooth
        print(f"[{label}] 每步 {STEP_PX}px -> 实际平均 {mean:.4f}px 标准差 {stdev:.4f} "
              f"最小 {min(deltas):.4f} 最大 {max(deltas):.4f} 原地不动 {stuck}/{len(deltas)} 帧  "
              f"{'PASS' if smooth else 'FAIL'}")
        print("   " + " ".join(f"{d:.3f}" for d in deltas[:12]))
    print(f"dpr={ratio}")
    print("smooth scroll check:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
