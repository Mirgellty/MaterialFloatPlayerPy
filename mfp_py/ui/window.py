"""The frameless host window around the card.

Qt does the parts the plugin needed Win32 hacks for: the window is translucent,
so the rounded corner is genuinely antialiased (no SetWindowRgn region), and the
drop shadow is a blurred silhouette of that same rounded shape (no four clipped
shadow windows). Geometry is in logical pixels on both sides, so the plugin's
"measure the scale back out of the API" calibration dance is unnecessary.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

from .. import win
from .card import SHADOW_PAD, Card


class CardWindow(QWidget):
    """Frameless, translucent, optional keep-on-top host for one Card."""

    actionRequested = Signal(str)
    moved = Signal()

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = config
        flags = Qt.FramelessWindowHint | Qt.Tool
        # WS_EX_NOACTIVATE：点卡片、悬停按钮都不会把前台焦点从游戏手里抢走。
        # 窗口化 / 无边框全屏的游戏一旦被别的窗口抢走前台就会掉出全屏（甚至最小化），
        # 这就是"指针放到按钮上游戏就被顶掉"的原因 —— 卡片本身不能被激活。
        flags |= Qt.WindowDoesNotAcceptFocus
        if config.get("alwaysOnTop", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Material 播放器")

        self.card = Card(config, self)
        self.card.actionRequested.connect(self.actionRequested)
        self.card.dragFinished.connect(self._on_drag_finished)

        self._shadow_effect: QGraphicsDropShadowEffect | None = None
        self._apply_shadow()
        self.apply_config(config, reposition=False)

    # ------------------------------------------------------------- geometry
    def apply_config(self, config, reposition: bool = True) -> None:
        self.cfg = config
        self.card.apply_config(config)
        self._update_flags()
        self._apply_shadow()

        card_w, card_h = self.card.card_size()
        pad = SHADOW_PAD if config.get("shadow", True) else 0
        self.card.setGeometry(pad, pad, card_w, card_h)
        self.setFixedSize(card_w + pad * 2, card_h + pad * 2)
        if reposition:
            self.restore_position()
        else:
            self.clamp_to_screen()
        self.card.update()

    def clamp_to_screen(self) -> None:
        """Keep the card reachable after a resize or a monitor change."""
        area = self._screen_geometry()
        x = min(max(self.x(), area.left()), max(area.left(), area.right() - self.width() + 1))
        y = min(max(self.y(), area.top()), max(area.top(), area.bottom() - self.height() + 1))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _update_flags(self) -> None:
        on_top = bool(self.cfg.get("alwaysOnTop", True))
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
        if visible:
            self.show()

    def _apply_shadow(self) -> None:
        if self.cfg.get("shadow", True):
            effect = QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(30)
            effect.setOffset(0, 7)
            effect.setColor(QColor(0, 0, 0, 115))
            self.card.setGraphicsEffect(effect)
            self._shadow_effect = effect
        else:
            self.card.setGraphicsEffect(None)
            self._shadow_effect = None

    def _screen_geometry(self) -> QRect:
        screen = self.screen() or self.card.screen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)

    def default_position(self) -> QPoint:
        area = self._screen_geometry()
        return QPoint(
            max(area.left() + 16, area.right() - self.width() - 40),
            max(area.top() + 16, area.bottom() - self.height() - 40),
        )

    def restore_position(self) -> None:
        saved = self.cfg.get("pos")
        area = self._screen_geometry()
        if isinstance(saved, dict) and isinstance(saved.get("x"), (int, float)):
            point = QPoint(int(saved["x"]), int(saved["y"]))
            # a remembered spot can be off-screen after a monitor change
            if not area.intersects(QRect(point, self.size())):
                point = self.default_position()
        else:
            point = self.default_position()
        self.move(point)

    def save_position(self) -> None:
        self.cfg["pos"] = {"x": self.x(), "y": self.y()}

    def reset_position(self) -> None:
        self.cfg.reset_position()
        self.move(self.default_position())

    def _on_drag_finished(self) -> None:
        self.save_position()
        self.moved.emit()

    # ---------------------------------------------------------------- topmost
    def raise_above_fullscreen(self) -> bool:
        """Only when something really is fullscreen, and without stealing focus."""
        if not self.cfg.get("gameTop", True) or not self.cfg.get("alwaysOnTop", True):
            return False
        if not self.isVisible() or not win.foreground_is_fullscreen():
            return False
        return win.raise_above_fullscreen(int(self.winId()))
