"""The floating card: frameless, translucent, always on top.

Ported from the plugin's window.js, with the same proportions (width, 0.35
aspect card, album art sized from the card height, controls in a bottom row,
progress bar riding the very bottom edge) and the same animations (crossfade on
track change, one-way title marquee, per-line lyric scroll, fade-in line change).

Two things get simpler than in the plugin: Qt paints into a transparent window
with per-pixel alpha, so the rounded corner needs no Win32 window region, and
the shadow is a real blurred silhouette instead of four clipped rectangles.
"""

from __future__ import annotations

import time
from bisect import bisect_right
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from .. import color as colors
from ..media import MODE_LABELS, mode_of
from .icons import MODE_ICONS, icon_pixmap

CARD_AR = 0.35          # window height / window width, same as the plugin
ART_RADIUS_RATIO = 0.60  # album art radius / card radius
SHADOW_PAD = 26         # room around the card for the drop shadow

TITLE_SCROLL = 2.6      # seconds to glide from head to tail
TITLE_HOLD_START = 5.0  # pause at the head before each glide (~10s per cycle)
TITLE_HOLD_END = 1.6    # pause at the tail, then snap back

FONT_FAMILIES = ["Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI"]

BUTTON_TOOLTIPS = {
    "prev": "上一首",
    "toggle": "播放 / 暂停",
    "next": "下一首",
    "mode": "切换播放模式",
    "close": "关闭悬浮播放器",
    "settings": "设置",
}


def _cubic_bezier(x1: float, y1: float, x2: float, y2: float):
    """CSS cubic-bezier(x1,y1,x2,y2) as a callable over [0,1]."""

    def bezier(t: float, first: float, second: float) -> float:
        return 3 * (1 - t) ** 2 * t * first + 3 * (1 - t) * t * t * second + t**3

    def solve(x: float) -> float:
        low, high = 0.0, 1.0
        for _ in range(18):
            mid = (low + high) / 2
            if bezier(mid, x1, x2) < x:
                low = mid
            else:
                high = mid
        return bezier((low + high) / 2, y1, y2)

    return solve


TITLE_EASE = _cubic_bezier(0.4, 0.0, 0.2, 1.0)
LINE_EASE = _cubic_bezier(0.2, 0.8, 0.2, 1.0)


@dataclass
class LyricLine:
    time: int          # milliseconds
    text: str = ""
    translation: str = ""
    duration: int = 0  # milliseconds


@dataclass
class LyricState:
    key: str = ""
    lines: tuple[LyricLine, ...] = ()
    none: bool = False


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds or 0)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def font(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    result = QFont()
    result.setFamilies(FONT_FAMILIES)
    result.setPixelSize(max(1, int(round(size))))
    result.setWeight(weight)
    try:  # tabular figures keep the mm:ss from jittering, like the plugin's CSS
        result.setFeature("tnum", 1)
    except Exception:  # noqa: BLE001 - cosmetic only, and Qt's API varies by build
        pass
    return result


@dataclass
class Button:
    key: str
    rect: QRectF
    size: float
    main: bool = False
    ghost: bool = False


class Card(QWidget):
    """Draws the whole card and turns clicks into actions."""

    actionRequested = Signal(str)
    dragFinished = Signal()

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = config
        self.snapshot = None
        self.lyric = LyricState()
        self.palette = colors.build_palette(colors.hex_to_rgb("#6750a4"), False, False)
        self._active = self.palette          # the palette this frame actually paints

        self._previous_palette = None
        self._fade_at = 0.0
        self._fade_t = 1.0

        self._cover_key = ""
        self._cover = QImage()

        self._pressed = ""
        self._hovered = ""
        self._ghost_visible = False
        self._ghost_t = 0.0
        self._ghost_from = 0.0
        self._ghost_target = 0.0
        self._ghost_at = 0.0
        self._dragging = False
        self._drag_offset = QPoint()

        self._title_text = ""
        self._line_times: tuple[float, ...] = ()
        self._title_overflow = 0
        self._title_x = 0.0
        self._title_phase = "hold"
        self._title_phase_at = time.monotonic()
        self._title_fade = False

        self._line_key = ""
        self._line_text = ""
        self._line_overflow = 0
        self._line_x = 0.0
        self._line_scroll_at = 0.0
        self._line_scroll_total = 0.0
        self._line_scroll_glide = 0.0
        self._line_scroll_hold = 0.0
        self._line_fade = False
        self._enter_at = 0.0

        self._sub_text = ""
        self._sub_translation = False

        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self._ticker = QTimer(self)
        # ~60fps: the marquee glides at a few pixels per second, so a 30fps tick
        # is itself visible as steps
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._on_tick)
        self._ticker.start()

    # ------------------------------------------------------------------ state
    def apply_config(self, config) -> None:
        self.cfg = config
        self._cover_key = ""  # the art is drawn at a size that depends on the card
        self.update()

    def set_palette(self, palette) -> None:
        if palette == self.palette:
            return
        self._previous_palette = self.palette
        self.palette = palette
        self._fade_at = time.monotonic()
        self._fade_t = 0.0
        self.update()

    def set_snapshot(self, snapshot) -> None:
        previous = self.snapshot
        self.snapshot = snapshot
        if snapshot is None:
            return
        track = snapshot.track
        if track is None:
            self._line_key = ""
            self._line_text = ""
            self._sub_text = ""
        if previous is None or previous.track is None or track is None or previous.track.key != track.key:
            self._line_key = ""
        self.update()

    def set_lyric(self, state: LyricState) -> None:
        self.lyric = state or LyricState()
        self._line_times = tuple(line.time / 1000.0 for line in self.lyric.lines)
        self._line_key = ""
        self.update()

    # --------------------------------------------------------------- geometry
    def card_size(self) -> tuple[int, int]:
        width = int(clamp(float(self.cfg.get("width", 384)), 260, 720))
        return width, max(60, int(round(width * CARD_AR)))

    def _radius(self) -> float:
        return float(clamp(float(self.cfg.get("radius", 16)), 0, 32))

    def _font_size(self) -> float:
        """The size the controls are built around: the current lyric line."""
        return self._font("fontCurrent", 13, 8, 40)

    def _font(self, key: str, fallback: float, low: float, high: float) -> float:
        value = self.cfg.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = fallback
        return float(clamp(value, low, high))

    def _fonts(self) -> dict:
        """Five independent sizes, all in px: 歌名 / 作者专辑 / 当前歌词 / 副行歌词 / 翻译."""
        return {
            "title": self._font("fontTitle", 15, 8, 40),
            "artist": self._font("fontArtist", 10, 8, 30),
            "current": self._font("fontCurrent", 13, 8, 40),
            "lyric": self._font("fontLyric", 11, 8, 40),
            "translation": self._font("fontTranslation", 11, 8, 40),
        }

    def _layout(self) -> dict:
        width = float(self.width())
        height = float(self.height())
        pad = max(6.0, 0.030 * width)
        gap = 0.028 * width
        art_side = min(height - 2 * pad, 0.42 * (width - 2 * pad))
        art = QRectF(pad, (height - art_side) / 2.0, art_side, art_side)
        body_left = art.right() + gap
        body = QRectF(body_left, pad, max(16.0, width - pad - body_left), height - 2 * pad)
        zen = bool(self.cfg.get("zen"))

        def measure(fonts: dict) -> tuple:
            title_h = fonts["title"] * 1.3
            artist_h = fonts["artist"] * 1.35
            current_h = fonts["current"] * 1.3
            sub_h = max(fonts["lyric"], fonts["translation"]) * 1.3
            lyric_h = current_h + 2 + sub_h
            head_h = title_h + 1 + artist_h + 3 + lyric_h
            return title_h, artist_h, current_h, sub_h, lyric_h, head_h, fonts["current"] * 2.4

        def room_for(fonts: dict, foot_h: float) -> float:
            if zen:
                # 沉浸模式里没有控制按钮，但要给右下角的设置/关闭按钮留位置
                return body.height() - (fonts["current"] * 1.7 + 4)
            return body.height() - foot_h - 2

        fonts = self._fonts()
        title_h, artist_h, current_h, sub_h, lyric_h, head_h, foot_h = measure(fonts)
        room = room_for(fonts, foot_h)
        if head_h > room > 0:
            # 几个字号都调得很大时整体等比缩小：宁可小一点，也不要被裁成一条缝
            scale = room / head_h
            fonts = {key: value * scale for key, value in fonts.items()}
            title_h, artist_h, current_h, sub_h, lyric_h, head_h, foot_h = measure(fonts)
            room = room_for(fonts, foot_h)

        size = fonts["current"]           # 控件大小跟着"正在播放歌词"字号走
        foot = QRectF(body.left(), body.bottom() - foot_h, body.width(), foot_h)
        if zen:
            head_top = body.top() + max(0.0, (room - head_h) / 2.0)
            clip_bottom = body.bottom()
        else:
            head_top = body.top()
            clip_bottom = foot.top() - 2
        return {
            "w": width,
            "h": height,
            "pad": pad,
            "gap": gap,
            "art": art,
            "body": body,
            "foot": foot,
            "head_h": head_h,
            "head_top": head_top,
            "head_clip_bottom": clip_bottom,
            "title_h": title_h,
            "artist_h": artist_h,
            "current_h": current_h,
            "sub_h": sub_h,
            "lyric_h": lyric_h,
            "fs": size,
            "fonts": fonts,
        }

    def _buttons(self, layout: dict) -> tuple[list[Button], Button | None, Button | None]:
        size = layout["fs"]
        foot = layout["foot"]
        zen = bool(self.cfg.get("zen"))
        buttons: list[Button] = []
        if not zen:
            normal = size * 1.95
            main = size * 2.4
            gap = layout["w"] * 0.015
            x = foot.left()
            centre = foot.center().y()
            for key, box in (
                ("prev", normal),
                ("toggle", main),
                ("next", normal),
                ("mode", normal),
            ):
                buttons.append(
                    Button(
                        key=key,
                        rect=QRectF(x, centre - box / 2, box, box),
                        size=box,
                        main=key == "toggle",
                    )
                )
                x += box + gap

        ghost_size = size * 1.7
        value = Button("close", QRectF(0, 0, 0, 0), ghost_size, ghost=True)
        settings = Button("settings", QRectF(0, 0, 0, 0), ghost_size, ghost=True)
        x = foot.right() - ghost_size
        value.rect = QRectF(x, foot.center().y() - ghost_size / 2, ghost_size, ghost_size)
        x -= ghost_size + 2
        settings.rect = QRectF(x, foot.center().y() - ghost_size / 2, ghost_size, ghost_size)
        return buttons, value, settings

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setOpacity(self._opacity())

        layout = self._layout()
        self._measure(layout)
        self._active = self._current_palette()
        self._paint_surface(painter, layout)
        self._paint_art(painter, layout)
        self._paint_head(painter, layout)
        self._paint_foot(painter, layout)
        self._paint_progress(painter, layout)
        painter.end()

    def _opacity(self) -> float:
        return float(clamp(float(self.cfg.get("opacity", 1.0)), 0.4, 1.0))

    def _ink(self) -> tuple[int, int, int]:
        return self._active.ink

    def _current_palette(self):
        """The palette for this frame: target, or a mix while a change is fading.

        The plugin animated the whole colour set (surface, text, accent, hairline)
        over 0.65s; mixing here does the same in one place, so no intermediate
        colour ever appears - including during track changes, day/night switches
        and custom colour picks.
        """
        previous = self._previous_palette
        if previous is None or self._fade_t >= 1.0:
            return self.palette
        return colors.mix_palette(previous, self.palette, self._fade_t)

    def _ink_alpha(self, alpha: float) -> QColor:
        red, green, blue = self._ink()
        return QColor(red, green, blue, int(round(alpha * 255)))

    def _card_path(self, layout: dict) -> QPainterPath:
        path = QPainterPath()
        radius = self._radius()
        rect = QRectF(0, 0, layout["w"], layout["h"])
        if radius <= 0.5:
            path.addRect(rect)
        else:
            path.addRoundedRect(rect, radius, radius)
        return path

    def _gradient(self, palette, layout: dict) -> QLinearGradient:
        gradient = QLinearGradient(0, 0, layout["w"], layout["h"])
        gradient.setColorAt(0.0, QColor(palette.color))
        gradient.setColorAt(1.0, QColor(palette.color_alt))
        return gradient

    def _paint_surface(self, painter: QPainter, layout: dict) -> None:
        path = self._card_path(layout)
        painter.fillPath(path, QBrush(self._gradient(self._active, layout)))
        radius = self._radius()
        inset = QRectF(0.5, 0.5, layout["w"] - 1, layout["h"] - 1)
        hairline = QPainterPath()
        if radius <= 0.5:
            hairline.addRect(inset)
        else:
            hairline.addRoundedRect(inset, max(0.0, radius - 0.5), max(0.0, radius - 0.5))
        painter.strokePath(hairline, QPen(self._ink_alpha(0.08), 1.0))

    def _cover_image(self, layout: dict):
        snapshot = self.snapshot
        track = snapshot.track if snapshot else None
        if track is None or not track.cover:
            return None
        key = f"{track.key}@{int(layout['art'].width())}"
        if key == self._cover_key and not self._cover.isNull():
            return self._cover
        image = QImage()
        if image.loadFromData(track.cover):
            self._cover = image
            self._cover_key = key
            return image
        self._cover = QImage()
        self._cover_key = key
        return None

    def _paint_art(self, painter: QPainter, layout: dict) -> None:
        rect = layout["art"]
        radius = self._radius() * ART_RADIUS_RATIO
        path = QPainterPath()
        if radius <= 0.5:
            path.addRect(rect)
        else:
            path.addRoundedRect(rect, radius, radius)
        painter.save()
        painter.setClipPath(path)
        image = self._cover_image(layout)
        if image is not None:
            # cover: object-fit: cover - fill the square, crop the overflow
            scaled = image.scaled(
                rect.size().toSize(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            painter.drawImage(
                QRectF(rect.left(), rect.top(), rect.width(), rect.height()),
                scaled,
                QRectF(
                    (scaled.width() - rect.width()) / 2,
                    (scaled.height() - rect.height()) / 2,
                    rect.width(),
                    rect.height(),
                ),
            )
        else:
            painter.fillPath(path, QBrush(self._ink_alpha(0.08)))
            side = rect.width() * 0.38
            icon_size = max(8, int(round(side)))
            # SVG fills take a plain colour; the 50% is applied through the painter
            pixmap = icon_pixmap("note", icon_size, QColor(*self._ink()).name())
            target = QRectF(
                rect.center().x() - side / 2,
                rect.center().y() - side / 2,
                side,
                side,
            )
            painter.setOpacity(self._opacity() * 0.5)
            painter.drawPixmap(target.toRect(), pixmap)
            painter.setOpacity(self._opacity())
        painter.restore()
        painter.strokePath(path, QPen(self._ink_alpha(0.10), 1.0))

    # ----------------------------------------------------------- measurement
    def _title_display(self) -> str:
        track = self.snapshot.track if self.snapshot else None
        return track.title if track else "Material 播放器"

    def _artist_display(self) -> str:
        track = self.snapshot.track if self.snapshot else None
        if track is None:
            app = (self.snapshot.app if self.snapshot else "") or ""
            return f"等待{app}开始播放" if app else "等待系统媒体会话开始播放"
        artist = track.artists or ""
        if track.album:
            artist = f"{artist} · {track.album}" if artist else track.album
        return artist

    def _current_line(self, lines) -> tuple[int, LyricLine | None]:
        if not lines:
            return -1, None
        index = bisect_right(self._line_times, self._position()) - 1
        index = max(0, min(index, len(lines) - 1))
        while index > 0 and not lines[index].text:
            index -= 1
        return index, lines[index]

    def _measure(self, layout: dict) -> None:
        """Pick the current line and measure how far the long lines overflow.

        The plugin measured inside its render pass; keeping it in one place means
        the marquee state machines and the painter always agree on the numbers.
        """
        width = layout["body"].width()
        title = self._title_display()
        if title != self._title_text:
            self._title_text = title
            self._title_phase = "hold"
            self._title_phase_at = time.monotonic()
            self._title_x = 0.0
            self._title_fade = False
        title_metrics = QFontMetricsF(font(layout["fonts"]["title"], QFont.Weight.DemiBold))
        overflow = title_metrics.horizontalAdvance(title) - width
        self._title_overflow = int(overflow) if overflow > 6 else 0

        lines = self.lyric.lines
        index, line = self._current_line(lines)
        line_text = line.text if line is not None else ""
        sub_text, translation = "", False
        if line is None:
            if self.lyric.none:
                line_text = "暂无歌词"
        else:
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].text:
                next_index += 1
            sub_text, translation = self._sub_line(line, lines, next_index)
        self._apply_line(line_text, index, line.duration if line is not None else 0)
        self._sub_text = sub_text
        self._sub_translation = translation
        line_metrics = QFontMetricsF(font(layout["fonts"]["current"], QFont.Weight.DemiBold))
        overflow = line_metrics.horizontalAdvance(line_text) - width
        self._line_overflow = int(overflow) if overflow > 6 else 0

    def _sub_line(self, line: LyricLine, lines, next_index: int) -> tuple[str, bool]:
        if self.cfg.get("translation", True) and line.translation:
            return line.translation, True
        if next_index < len(lines):
            return lines[next_index].text, False
        return "", False

    def _paint_head(self, painter: QPainter, layout: dict) -> None:
        fonts = layout["fonts"]
        left = layout["body"].left()
        width = layout["body"].width()
        top = layout["head_top"]

        # At the narrowest widths the text block is taller than the space above the
        # controls. The plugin clipped it the same way (body was overflow:hidden),
        # so the title/artist/lyric are cut off rather than drawn over the buttons.
        head_clip = QRectF(left, top, width, max(0.0, layout["head_clip_bottom"] - top))
        painter.save()
        painter.setClipRect(head_clip)

        title_rect = QRectF(left, top, width, layout["title_h"])
        self._draw_line(
            painter,
            title_rect,
            self._title_text,
            font(fonts["title"], QFont.Weight.DemiBold),
            QColor(self._active.on_color),
            self._title_x,
            self._title_fade,
            marquee=self._title_overflow > 6,
        )

        artist_rect = QRectF(left, title_rect.bottom() + 1, width, layout["artist_h"])
        artist_font = font(fonts["artist"])
        colour = QColor(self._active.on_color)
        colour.setAlphaF(0.55)
        painter.setFont(artist_font)
        painter.setPen(colour)
        painter.drawText(
            artist_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter),
            QFontMetricsF(artist_font).elidedText(self._artist_display(), Qt.ElideRight, width),
        )

        lyric_rect = QRectF(left, artist_rect.bottom() + 3, width, layout["lyric_h"])
        self._paint_lyric(painter, layout, lyric_rect)
        painter.restore()

    def _paint_lyric(self, painter: QPainter, layout: dict, rect: QRectF) -> None:
        if not self._line_text and not self._sub_text:
            return
        fonts = layout["fonts"]
        size = fonts["current"]
        enter = self._enter_progress()
        lift = 1.0 - enter

        painter.save()
        if enter < 1.0:
            painter.setOpacity(self._opacity() * enter)
        self._draw_line(
            painter,
            QRectF(rect.left(), rect.top() + lift * size * 0.52, rect.width(), size * 1.3),
            self._line_text,
            font(fonts["current"], QFont.Weight.DemiBold),
            QColor(self._active.accent),
            self._line_x,
            self._line_fade,
            marquee=self._line_overflow > 6,
        )
        painter.restore()

        if not self._sub_text:
            return
        sub_font = font(fonts["translation"] if self._sub_translation else fonts["lyric"])
        colour = QColor(self._active.on_color)
        colour.setAlphaF(0.72 if self._sub_translation else 0.62)
        painter.save()
        if enter < 1.0:
            painter.setOpacity(self._opacity() * enter)
        painter.setFont(sub_font)
        painter.setPen(colour)
        painter.drawText(
            QRectF(
                rect.left(),
                rect.top() + layout["current_h"] + 2 + lift * size * 0.35,
                rect.width(),
                layout["sub_h"],
            ),
            int(Qt.AlignLeft | Qt.AlignVCenter),
            QFontMetricsF(sub_font).elidedText(self._sub_text, Qt.ElideRight, rect.width()),
        )
        painter.restore()

    def _paint_foot(self, painter: QPainter, layout: dict) -> None:
        buttons, close, settings = self._buttons(layout)
        snapshot = self.snapshot
        has_song = bool(snapshot and snapshot.track)
        playing = bool(snapshot and snapshot.playing)
        mode = mode_of(snapshot) if snapshot else "order"

        icons = {
            "prev": "prev",
            "toggle": "pause" if (snapshot and snapshot.track and playing) else "play",
            "next": "next",
            "mode": MODE_ICONS.get(mode, "shuffle"),
        }
        enabled = {
            "prev": bool(snapshot and snapshot.can_prev),
            "toggle": bool(snapshot and snapshot.can_toggle),
            "next": bool(snapshot and snapshot.can_next),
            "mode": bool(snapshot and snapshot.can_mode),
        }

        for button in buttons:
            active = button.key == self._hovered and has_song and enabled.get(button.key, True)
            dim = not has_song or not enabled.get(button.key, True)
            painter.save()
            if dim:
                painter.setOpacity(self._opacity() * 0.4)
            if button.main:
                colour = QColor(self._active.accent)
                if active:
                    colour = colour.lighter(108)
                if self._pressed == button.key:
                    painter.translate(button.rect.center())
                    painter.scale(0.92, 0.92)
                    painter.translate(-button.rect.center())
                painter.setBrush(QBrush(colour))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(button.rect)
                icon_colour = self._active.accent_on
            else:
                if active:
                    painter.setBrush(QBrush(self._ink_alpha(0.16)))
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(button.rect)
                icon_colour = self._active.on_color
            side = button.size * 0.58
            pixmap = icon_pixmap(icons[button.key], max(6, int(round(side))), icon_colour)
            target = QRectF(
                button.rect.center().x() - side / 2,
                button.rect.center().y() - side / 2,
                side,
                side,
            )
            painter.drawPixmap(target.toRect(), pixmap)
            painter.restore()

        time_font = font(layout["fs"] * 0.78)
        painter.setFont(time_font)
        time_color = QColor(self._active.on_color)
        time_color.setAlphaF(0.55)
        painter.setPen(time_color)
        text = ""
        # 时间并入沉浸模式：开了沉浸就不画时间（原来的"显示播放时间"开关已经去掉）
        if not self.cfg.get("zen") and snapshot:
            duration = snapshot.track.duration if snapshot.track else 0.0
            text = f"{fmt_time(self._position())} / {fmt_time(duration)}"
        if text:
            # at the smallest widths the controls and the clock would collide; the
            # plugin just overlapped them, we drop the clock instead
            left_limit = max((button.rect.right() for button in buttons), default=0.0) + 6
            right_limit = settings.rect.left() - 8
            metrics = QFontMetricsF(time_font)
            if right_limit - left_limit >= metrics.horizontalAdvance(text):
                painter.drawText(
                    QRectF(
                        left_limit,
                        layout["foot"].top(),
                        right_limit - left_limit,
                        layout["foot"].height(),
                    ),
                    int(Qt.AlignRight | Qt.AlignVCenter),
                    text,
                )

        if self._ghost_t > 0.01:
            for button in (close, settings):
                painter.save()
                # the plugin showed ghosts at 50% while hovering the card, then 100% on the button
                alpha = self._ghost_t * (1.0 if button.key == self._hovered else 0.5)
                painter.setOpacity(self._opacity() * alpha)
                if button.key == self._hovered:
                    painter.setBrush(QBrush(self._ink_alpha(0.16)))
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(button.rect)
                side = button.size * 0.62
                pixmap = icon_pixmap(
                    "close" if button.key == "close" else "settings",
                    max(6, int(round(side))),
                    self._active.on_color,
                )
                target = QRectF(
                    button.rect.center().x() - side / 2,
                    button.rect.center().y() - side / 2,
                    side,
                    side,
                )
                painter.drawPixmap(target.toRect(), pixmap)
                painter.restore()

    def _paint_progress(self, painter: QPainter, layout: dict) -> None:
        snapshot = self.snapshot
        duration = snapshot.track.duration if (snapshot and snapshot.track) else 0.0
        ratio = 0.0
        if duration > 0:
            ratio = clamp(self._position() / duration, 0.0, 1.0)
        painter.save()
        # the card clips the bar, so both ends follow the rounded corner
        painter.setClipPath(self._card_path(layout))
        height = 3.0
        top = layout["h"] - height
        painter.fillRect(
            QRectF(0, top, layout["w"], height),
            QBrush(self._ink_alpha(0.13)),
        )
        if ratio > 0:
            painter.fillRect(
                QRectF(0, top, layout["w"] * ratio, height),
                QBrush(QColor(self._active.accent)),
            )
        painter.restore()

    # --------------------------------------------------------------- drawing
    def _draw_line(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        text_font: QFont,
        colour: QColor,
        offset: float,
        fade: bool,
        marquee: bool = False,
    ) -> None:
        """Draw one line of text, optionally marqueeing with soft edges.

        The plugin did this with a CSS mask; here the text is drawn into an image
        and multiplied by a horizontal alpha gradient, which is the same thing.

        A marquee is drawn as a *texture being moved*, not as text being redrawn
        at a new position: redrawing every frame lets Qt snap the glyph origins to
        whole pixels, so a slow glide sits still and then jumps a pixel ("有点抖").
        Rasterising the line once and blitting it at a fractional x is also what
        the plugin's CSS ``transform: translateX()`` did on the GPU.
        """
        if not text:
            return

        if not marquee and not fade:
            painter.setFont(text_font)
            painter.setPen(colour)
            painter.drawText(
                rect,
                int(Qt.AlignLeft | Qt.AlignVCenter),
                text,
            )
            return

        self._draw_marquee(painter, rect, text, text_font, colour, offset, fade)

    def _draw_marquee(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        text_font: QFont,
        colour: QColor,
        offset: float,
        fade: bool,
    ) -> None:
        """Draw a line that can be sliding, with optional soft edges.

        The offset goes through the painter transform and the text is placed with
        ``drawText(QPointF, ...)``. Both matter: a rect-based drawText snaps its
        origin to whole pixels, so a slow marquee would sit still for a few frames
        and then jump a pixel - that is the stutter this was written to remove
        (measured: 0.25px steps came out as -0.15..+0.40px, and 16 of 23 frames
        were pixel-identical to the previous one; with the transform they are a
        steady -0.25px, none identical).
        """
        ratio = self.devicePixelRatioF()
        pixels_w = max(1, int(round(rect.width() * ratio)))
        pixels_h = max(1, int(round(rect.height() * ratio)))
        canvas = QImage(pixels_w, pixels_h, QImage.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.transparent)
        # with the ratio set, the painter works in logical units but rasterises at
        # device resolution
        canvas.setDevicePixelRatio(ratio)
        metrics = QFontMetricsF(text_font)
        inner = QPainter(canvas)
        inner.setRenderHint(QPainter.Antialiasing, True)
        inner.setRenderHint(QPainter.TextAntialiasing, True)
        inner.setFont(text_font)
        inner.setPen(colour)
        inner.translate(-offset, 0.0)
        inner.drawText(
            QPointF(2.0, (rect.height() - metrics.height()) / 2.0 + metrics.ascent()),
            text,
        )
        inner.end()
        if fade:
            fade_painter = QPainter(canvas)
            gradient = QLinearGradient(0, 0, rect.width(), 0)
            gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
            edge = min(0.28, 14.0 / max(1.0, rect.width()))
            gradient.setColorAt(edge, QColor(0, 0, 0, 255))
            gradient.setColorAt(1.0 - edge, QColor(0, 0, 0, 255))
            gradient.setColorAt(1.0, QColor(0, 0, 0, 0))
            fade_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            fade_painter.fillRect(QRectF(0, 0, rect.width(), rect.height()), gradient)
            fade_painter.end()
        # whole device pixels: keeps the glyph shapes crisp while it glides
        painter.drawImage(
            QPointF(
                round(rect.left() * ratio) / ratio,
                round(rect.top() * ratio) / ratio,
            ),
            canvas,
        )

    # ------------------------------------------------------------ animation
    def _position(self) -> float:
        if self.snapshot is None:
            return 0.0
        return self.snapshot.live_position()

    def _enter_progress(self) -> float:
        if not self._enter_at:
            return 1.0
        elapsed = time.monotonic() - self._enter_at
        if elapsed >= 0.26:
            return 1.0
        return clamp(LINE_EASE(clamp(elapsed / 0.26, 0.0, 1.0)), 0.0, 1.0)

    def _apply_line(self, text: str, index: int, duration_ms: int) -> None:
        key = f"{index}|{text}"
        if key == self._line_key:
            return
        self._line_key = key
        self._line_text = text
        self._line_x = 0.0
        self._line_fade = False
        self._line_scroll_at = time.monotonic()
        self._line_scroll_total = clamp((duration_ms or 6000) / 1000.0, 2.6, 24.0)
        self._line_scroll_hold = min(0.9, self._line_scroll_total * 0.16)
        self._line_scroll_glide = max(1.8, self._line_scroll_total - self._line_scroll_hold * 2)
        self._enter_at = time.monotonic()

    def _advance_line_scroll(self) -> None:
        if self._line_overflow <= 6 or not self._line_text:
            self._line_x = 0.0
            self._line_fade = False
            return
        elapsed = time.monotonic() - self._line_scroll_at
        if elapsed < self._line_scroll_hold:
            self._line_x = 0.0
            self._line_fade = False
            return
        glide = elapsed - self._line_scroll_hold
        if glide < self._line_scroll_glide:
            self._line_x = self._line_overflow * (glide / self._line_scroll_glide)
            self._line_fade = True
        else:
            self._line_x = float(self._line_overflow)
            self._line_fade = False

    def _advance_title(self) -> None:
        now = time.monotonic()
        if self._title_overflow <= 6:
            self._title_x = 0.0
            self._title_fade = False
            return
        elapsed = now - self._title_phase_at
        if self._title_phase == "hold":
            self._title_x = 0.0
            self._title_fade = False
            if elapsed >= TITLE_HOLD_START:
                self._title_phase = "scroll"
                self._title_phase_at = now
        elif self._title_phase == "scroll":
            progress = clamp(elapsed / TITLE_SCROLL, 0.0, 1.0)
            self._title_x = self._title_overflow * TITLE_EASE(progress)
            self._title_fade = progress < 1.0
            if elapsed >= TITLE_SCROLL:
                self._title_phase = "tail"
                self._title_phase_at = now
        else:  # at the tail, then straight back to the head - never rolls back
            self._title_x = float(self._title_overflow)
            self._title_fade = False
            if elapsed >= TITLE_HOLD_END:
                self._title_phase = "hold"
                self._title_phase_at = now
                self._title_x = 0.0

    def _on_tick(self) -> None:
        if self.advance_animations():
            self.update()
        elif self.snapshot is not None and self.snapshot.playing:
            self.update()  # the progress bar moves even when nothing else does

    def advance_animations(self) -> bool:
        """Step every time-based animation; returns True when something moved."""
        layout = self._layout()
        self._measure(layout)
        before = (
            round(self._title_x, 3),
            round(self._line_x, 3),
            self._title_fade,
            self._line_fade,
            round(self._ghost_t, 3),
            round(self._fade_t, 3),
            self._title_phase,
        )
        now = time.monotonic()
        if self._fade_t < 1.0:
            self._fade_t = clamp((now - self._fade_at) / 0.65, 0.0, 1.0)
            if self._fade_t >= 1.0:
                self._previous_palette = None
        if abs(self._ghost_t - self._ghost_target) > 0.001:
            progress = 1.0 if not self._ghost_at else clamp((now - self._ghost_at) / 0.18, 0.0, 1.0)
            self._ghost_t = self._ghost_from + (self._ghost_target - self._ghost_from) * progress
        self._advance_title()
        self._advance_line_scroll()
        if self._enter_progress() >= 1.0:
            self._enter_at = 0.0
        after = (
            round(self._title_x, 3),
            round(self._line_x, 3),
            self._title_fade,
            self._line_fade,
            round(self._ghost_t, 3),
            round(self._fade_t, 3),
            self._title_phase,
        )
        return before != after

    def _set_ghost_visible(self, visible: bool) -> None:
        self._ghost_target = 1.0 if visible else 0.0
        self._ghost_from = self._ghost_t
        self._ghost_at = time.monotonic()
        self._ghost_visible = visible

    # ---------------------------------------------------------- interaction
    def _all_buttons(self) -> list[Button]:
        layout = self._layout()
        buttons, close, settings = self._buttons(layout)
        return [*buttons, close, settings]

    def _hit(self, position) -> Button | None:
        for button in self._all_buttons():
            if button.rect.contains(position):
                return button
        return None

    def _actionable(self, button: Button) -> bool:
        if button.ghost:
            return True
        snapshot = self.snapshot
        if snapshot is None or snapshot.track is None:
            return False
        return {
            "prev": snapshot.can_prev,
            "next": snapshot.can_next,
            "toggle": snapshot.can_toggle,
            "mode": snapshot.can_mode,
        }.get(button.key, True)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._set_ghost_visible(True)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._set_ghost_visible(False)
        self._hovered = ""
        QToolTip.hideText()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._dragging:
            window = self.window()
            window.move(event.globalPosition().toPoint() - self._drag_offset)
            return
        button = self._hit(event.position())
        key = button.key if button else ""
        if key != self._hovered:
            self._hovered = key
            # 全屏游戏时不要弹提示框：那会是一个新的置顶窗口，落到游戏上面添乱
            if button is not None and not self._fullscreen_foreground():
                QToolTip.showText(event.globalPosition().toPoint(), self._tooltip(button), self)
            else:
                QToolTip.hideText()
            self.update()

    @staticmethod
    def _fullscreen_foreground() -> bool:
        from .. import win

        return win.IS_WINDOWS and win.foreground_is_fullscreen()

    def _tooltip(self, button: Button) -> str:
        snapshot = self.snapshot
        track = snapshot.track if snapshot else None
        if button.key == "toggle":
            if track is None:
                return "暂无歌曲"
            return "正在播放（点击暂停）" if snapshot.playing else "已暂停（点击播放）"
        if button.key == "mode":
            label = MODE_LABELS.get(mode_of(snapshot) if snapshot else "order", "播放模式")
            return f"{label}（点击切换）"
        return BUTTON_TOOLTIPS.get(button.key, button.key)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.LeftButton:
            return
        button = self._hit(event.position())
        if button is None:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.window().pos()
            self.setCursor(Qt.ClosedHandCursor)
            return
        self._pressed = button.key
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.LeftButton:
            return
        if self._dragging:
            self._dragging = False
            self.unsetCursor()
            self.dragFinished.emit()
            return
        button = self._hit(event.position())
        pressed, self._pressed = self._pressed, ""
        self.update()
        if button is not None and button.key == pressed and self._actionable(button):
            self.actionRequested.emit(button.key)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
