"""The settings dialog.

Same list of options as the plugin's BetterNCM page (and the same ranges), plus
the two things that only exist because this runs outside a player: which media
source to read, and where lyrics come from.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import config as config_module


class ColorButton(QToolButton):
    """A swatch that opens the colour picker - the equivalent of <input type=color>."""

    picked = Signal(str)

    def __init__(self, value: str, parent=None) -> None:
        super().__init__(parent)
        self.value = value
        self.setFixedSize(38, 24)
        self.setToolTip("自定义背景颜色")
        self.clicked.connect(self._choose)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QColor(self.value))
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
        painter.drawRoundedRect(rect, 4, 4)

    def _choose(self) -> None:
        from PySide6.QtWidgets import QColorDialog

        chosen = QColorDialog.getColor(QColor(self.value), self, "自定义背景颜色")
        if chosen.isValid():
            self.value = chosen.name()
            self.update()
            self.picked.emit(self.value)


class SettingsDialog(QDialog):
    """Edits the config in place; every row reports its patch as it changes."""

    changed = Signal(dict)
    showRequested = Signal()
    hideRequested = Signal()
    reloadRequested = Signal()
    resetPositionRequested = Signal()

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = config
        self.setWindowTitle("MaterialFloatPlayerPy 设置")
        self.setMinimumWidth(470)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        self._add(outer, "启用悬浮播放器", self._checkbox("enabled", self._toggle_enabled))
        self._add(outer, "背景颜色", self._background_controls())
        self._add(outer, "白天模式（浅色背景）", self._checkbox("light"))
        self._add(outer, "沉浸模式（隐藏按钮与时间，文字居中）", self._checkbox("zen"))
        self._add(outer, "播放器宽度", self._slider("width", 260, 720, 4))
        self._add(outer, "圆角半径", self._slider("radius", 0, 32, 1))
        self._add(outer, "歌名字号", self._slider("fontTitle", 8, 40, 1))
        self._add(outer, "作者 / 专辑字号", self._slider("fontArtist", 8, 30, 1))
        self._add(outer, "正在播放歌词字号", self._slider("fontCurrent", 8, 40, 1))
        self._add(outer, "歌词字号（下一句）", self._slider("fontLyric", 8, 40, 1))
        self._add(outer, "翻译字号", self._slider("fontTranslation", 8, 40, 1))
        self._add(outer, "不透明度", self._slider("opacity", 0.4, 1.0, 0.05))
        self._add(outer, "显示歌词翻译", self._checkbox("translation"))
        self._add(outer, "窗口置顶", self._checkbox("alwaysOnTop"))
        self._add(outer, "全屏游戏上方显示", self._checkbox("gameTop"))
        self._add(outer, "显示阴影", self._checkbox("shadow"))
        # 由插件驱动时不需要选来源：曲目、歌词、播放模式都来自网易云本身。
        # 桥接状态文件只在手动启动时才有意义（插件每次都会用命令行参数覆盖它）。
        self._add(outer, "桥接状态文件（插件用）", self._text_field("bridgePath", 260))

        actions = QHBoxLayout()
        actions.setSpacing(8)
        for label, signal in (
            ("显示悬浮窗", self.showRequested),
            ("隐藏悬浮窗", self.hideRequested),
            ("重新加载悬浮窗", self.reloadRequested),
            ("重置窗口位置", self.resetPositionRequested),
            ("打开数据目录", None),
        ):
            button = QPushButton(label)
            if signal is None:
                button.clicked.connect(self._open_data_dir)
            else:
                button.clicked.connect(signal.emit)
            actions.addWidget(button)
        actions.addStretch(1)
        outer.addLayout(actions)

        tip = QLabel(
            "设置即时生效；悬浮窗可用鼠标拖动到任意位置，位置会被记住。\n"
            "由网易云插件启动时，曲目、进度、播放模式和歌词都直接来自网易云；"
            "单独运行时会读 Windows 系统媒体会话。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("font-size: 12px;")
        # follow the dialog theme instead of assuming a light-on-dark palette
        palette = tip.palette()
        palette.setColor(QPalette.WindowText, palette.color(QPalette.PlaceholderText))
        tip.setPalette(palette)
        outer.addWidget(tip)

    # ------------------------------------------------------------- fragments
    def _add(self, layout: QVBoxLayout, label: str, widget: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        text = QLabel(label)
        text.setMinimumWidth(215)
        row.addWidget(text)
        row.addStretch(1)
        row.addWidget(widget)
        layout.addLayout(row)

    def _checkbox(self, key: str, hook=None) -> QCheckBox:
        box = QCheckBox()
        box.setChecked(bool(self.cfg.get(key)))
        box.toggled.connect(lambda value: self._apply({key: bool(value)}, hook))
        return box

    def _toggle_enabled(self, value: bool) -> None:
        (self.showRequested if value else self.hideRequested).emit()

    def _slider(self, key: str, low: float, high: float, step: float) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        scale = 100 if step < 1 else 1
        slider = QSlider(Qt.Horizontal)
        slider.setMinimumWidth(190)
        slider.setMinimum(int(round(low * scale)))
        slider.setMaximum(int(round(high * scale)))
        slider.setSingleStep(int(round(step * scale)))
        slider.setValue(int(round(float(self.cfg.get(key)) * scale)))
        value_label = QLabel(self._format(self.cfg.get(key)))
        value_label.setMinimumWidth(52)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # debounce while dragging, like the plugin's sliders, then commit once
        timer = QTimer(holder)
        timer.setSingleShot(True)
        timer.setInterval(220)
        timer.timeout.connect(lambda: self._apply({key: slider.value() / scale}))
        slider.valueChanged.connect(
            lambda raw: (
                value_label.setText(self._format(raw / scale)),
                timer.start(),
            )
        )
        row.addWidget(slider)
        row.addWidget(value_label)
        return holder

    @staticmethod
    def _format(value) -> str:
        return f"{float(value):g}"

    def _background_controls(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        group = QButtonGroup(holder)
        for mode, label in (("album", "专辑主要颜色"), ("custom", "自定义颜色")):
            radio = QRadioButton(label)
            radio.setChecked(self.cfg.get("bgMode") == mode)
            radio.toggled.connect(
                lambda checked, value=mode: self._apply({"bgMode": value}) if checked else None
            )
            group.addButton(radio)
            row.addWidget(radio)
        swatch = ColorButton(self.cfg.get("customColor", "#6750a4"))
        swatch.picked.connect(lambda value: self._apply({"customColor": value}))
        row.addWidget(swatch)
        return holder

    def _text_field(self, key: str, width: int) -> QWidget:
        field = QLineEdit(str(self.cfg.get(key) or ""))
        field.setMinimumWidth(width)
        field.setPlaceholderText("C:/betterncm/mfp-bridge.json")
        field.editingFinished.connect(lambda: self._apply({key: field.text().strip()}))
        return field

    def _open_data_dir(self) -> None:
        import os

        os.startfile(str(config_module.data_dir()))  # noqa: S606 - user initiated

    def _apply(self, patch: dict, hook=None) -> None:
        self.changed.emit(patch)
        if hook is not None:
            hook(patch[list(patch)[0]])
