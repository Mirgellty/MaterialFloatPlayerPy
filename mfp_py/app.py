"""Wiring: settings, media source, lyrics, palette and the card, plus a tray icon.

The plugin's main-loop work lives here - publish the current state, keep the
window alive, react to settings - except that the state now comes from the media
source and there is no host application to restart.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import threading
import time

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__, color as colors, win
from .bridge import BridgeSource
from .config import APP_NAME, Config, data_dir
from .demo import DemoSource
from .lyrics import LyricProvider
from .media import MODE_LABELS, mode_of
from .smtc import SystemMediaSource
from .ui.card import Card, LyricLine, LyricState, fmt_time
from .ui.settings import SettingsDialog
from .ui.window import CardWindow

log = logging.getLogger("mfp")

SNAPSHOT_INTERVAL_MS = 250
TOPCHECK_INTERVAL_MS = 2000

LOCK_NAME = "MaterialFloatPlayerPy"
QUIT_REQUEST = data_dir() / "quit.request"


def take_single_instance_lock(bridge_mode: bool, skip: bool = False) -> bool:
    """One card at a time.

    If the widget is already running and this launch is the plugin-driven one, ask
    the other instance to quit and take over: otherwise enabling the plugin while
    the standalone widget is open would draw two identical cards.
    """
    if skip:
        return True
    if win.acquire_instance_lock(LOCK_NAME):
        QUIT_REQUEST.unlink(missing_ok=True)
        return True
    if not bridge_mode:
        log.info("已经有一个悬浮窗在运行，这次启动退出")
        return False
    log.info("已有实例在运行，请求它退出后接管")
    try:
        QUIT_REQUEST.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    for _ in range(40):
        QThread.msleep(100)
        QCoreApplication.processEvents()
        if win.acquire_instance_lock(LOCK_NAME):
            QUIT_REQUEST.unlink(missing_ok=True)
            return True
    log.warning("已有实例占着位置且没有让出，这次启动退出")
    return False


class PaletteWorker(QObject):
    """Extracts the album colour off the UI thread."""

    ready = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, tuple[int, int, int]] = {}

    def request(self, key: str, cover: bytes | None) -> None:
        if not cover:
            self.ready.emit(key, None)
            return
        cached = self._cache.get(key)
        if cached is not None:
            self.ready.emit(key, cached)
            return
        threading.Thread(
            target=self._work, args=(key, cover), name="mfp-palette", daemon=True
        ).start()

    def _work(self, key: str, cover: bytes) -> None:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(cover)) as image:
                rgb = colors.dominant_color(image)
        except Exception as exc:  # noqa: BLE001 - a broken cover just falls back
            log.warning("取色失败 %s: %s", key, exc)
            self.ready.emit(key, None)
            return
        self._cache[key] = rgb
        log.info("取色 %s -> %s", key, colors.rgb_to_hex(rgb))
        self.ready.emit(key, rgb)


class LyricBridge(QObject):
    """Carries lyric results from the provider's worker thread onto the UI thread."""

    ready = Signal(str, object, bool)


class FloatingPlayer(QObject):
    def __init__(self, app: QApplication, config: Config, args) -> None:
        super().__init__()
        self.app = app
        self.cfg = config
        self.args = args
        self.album_rgb: tuple[int, int, int] | None = None
        self.track_key = ""
        self._lyric_key = ""
        self._lyric_rev = None
        self._palette_key = None

        self.window = CardWindow(config)
        self.card: Card = self.window.card
        self.card.actionRequested.connect(self._on_action)
        self.started_at = time.time()
        self.lock = None
        self._plugin_asked_quit = False

        self.palette_worker = PaletteWorker()
        self.palette_worker.ready.connect(self._on_palette)
        self.lyric_bridge = LyricBridge()
        self.lyric_bridge.ready.connect(self._on_lyrics)
        self.lyrics = LyricProvider(mode=config.get("lyrics", "online"), on_ready=self._lyric_ready)

        self.source = self._make_source()
        self.settings_dialog: SettingsDialog | None = None

        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(SNAPSHOT_INTERVAL_MS)
        self._snapshot_timer.timeout.connect(self._tick)
        self._snapshot_timer.start()

        self._top_timer = QTimer(self)
        self._top_timer.setInterval(TOPCHECK_INTERVAL_MS)
        self._top_timer.timeout.connect(self.window.raise_above_fullscreen)
        self._top_timer.start()

        self.tray = self._build_tray()
        self._apply_palette(None)

    # ------------------------------------------------------------- sources
    def _make_source(self):
        # --demo is a runtime override: it must not rewrite the saved setting
        bridge_path = self._bridge_path()
        if bridge_path:
            source = BridgeSource(bridge_path, on_command=self._on_bridge_command)
        elif getattr(self.args, "demo", False) or self.cfg.get("source") == "demo":
            source = DemoSource()
        else:
            source = SystemMediaSource()
        source.start()
        log.info("媒体来源: %s%s", source.name, f"（{bridge_path}）" if bridge_path else "")
        return source

    def _bridge_path(self) -> str:
        """--bridge wins; otherwise the saved path, but only in bridge mode."""
        explicit = getattr(self.args, "bridge", None)
        if explicit:
            return str(explicit)
        if self.cfg.get("source") == "bridge":
            return str(self.cfg.get("bridgePath") or "")
        return ""

    def _switch_source(self, name: str) -> None:
        if self.args is not None:
            self.args.demo = False
            self.args.bridge = None
        if self.source.name == name:
            return
        if name == "bridge" and not (self.cfg.get("bridgePath") or ""):
            log.warning("桥接来源没有配置状态文件路径（bridgePath）")
            return
        self.source.stop()
        self.source = self._make_source()
        self.track_key = ""
        self._lyric_key = ""
        self._lyric_rev = None
        self._palette_key = None
        self.album_rgb = None

    # --------------------------------------------------- commands from the plugin
    def _on_bridge_command(self, action: str) -> None:
        """The plugin asks the window to show, hide, reload, or go away.

        Called on the UI thread: the bridge is polled from the app's tick timer.
        """
        log.info("插件命令: %s", action)
        if action in ("show", "open"):
            self.show()
        elif action == "hide":
            self.hide()
        elif action == "reload":
            self._reload()
        elif action == "settings":
            self.open_settings()
        elif action in ("quit", "exit"):
            self._plugin_asked_quit = True
            self.app.quit()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.cfg.get("enabled", True):
            self.show()
        log.info("启动 MaterialFloatPlayerPy %s（%s）", __version__, data_dir())

    def show(self) -> None:
        self.window.show()
        self.window.raise_()

    def hide(self) -> None:
        self.window.hide()

    def shutdown(self) -> None:
        if isinstance(self.source, BridgeSource) and not self._plugin_asked_quit:
            # Only report a *user* exit. The plugin reads "quit" as "the user
            # closed it for good" and stops supervising; when the plugin itself
            # asked us to go (restart, or the feature was switched off), saying
            # goodbye would leave it refusing to bring us back.
            self.source.command("quit")
        self.source.stop()
        self.window.save_position()
        self.cfg.save()
        self.window.hide()

    # ---------------------------------------------------------------- tray
    def _tray_icon(self) -> QIcon:
        from .ui.icons import icon_pixmap

        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#6750a4"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, size - 4, size - 4)
        painter.drawPixmap(14, 14, icon_pixmap("note", 36, "#ffffff"))
        painter.end()
        return QIcon(pixmap)

    def _build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(self._tray_icon(), self.app)
        tray.setToolTip("Material 播放器")
        menu = QMenu()

        def add(text: str, handler) -> None:
            action = QAction(text, menu)
            action.triggered.connect(handler)
            menu.addAction(action)

        add("显示悬浮窗", self.show)
        add("隐藏悬浮窗", self.hide)
        menu.addSeparator()
        add("设置…", self.open_settings)
        add("重置窗口位置", self._reset_position)
        menu.addSeparator()
        add("退出", self.app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show() if not self.window.isVisible() else self.hide()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    # ------------------------------------------------------------- settings
    def open_settings(self) -> None:
        if self.settings_dialog is None:
            dialog = SettingsDialog(self.cfg)
            dialog.changed.connect(self._on_settings_changed)
            dialog.showRequested.connect(self.show)
            dialog.hideRequested.connect(self.hide)
            dialog.reloadRequested.connect(self._reload)
            dialog.resetPositionRequested.connect(self._reset_position)
            self.settings_dialog = dialog
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _on_settings_changed(self, patch: dict) -> None:
        log.info("设置: %s", patch)
        self.cfg.update(patch)
        self.window.apply_config(self.cfg, reposition=False)
        if "source" in patch:
            self._switch_source(patch["source"])
        if "lyrics" in patch:
            self.lyrics.set_mode(patch["lyrics"])
            self._lyric_key = ""
        self._apply_palette(self.album_rgb)

    def _reload(self) -> None:
        self.window.hide()
        self.card.set_lyric(LyricState())
        self.track_key = ""
        self._lyric_key = ""
        QTimer.singleShot(120, self.show)

    def _reset_position(self) -> None:
        self.window.reset_position()

    # ----------------------------------------------------------------- tick
    def _tick(self) -> None:
        self._check_quit_request()
        snapshot = self.source.snapshot()
        self.card.set_snapshot(snapshot)
        track = snapshot.track
        if track is None:
            if self.track_key:
                self._forget_track()
            return
        if track.key == self.track_key:
            self._ensure_palette(track)
            self._poll_source_lyrics(track)
            return
        self.track_key = track.key
        self._palette_key = None
        self._lyric_rev = None
        # keep the colour we have until the new cover's colour is known: going
        # through the default palette here is what made track changes fade via a
        # third colour
        self._ensure_palette(track)
        self._request_lyrics(track)

    def _check_quit_request(self) -> None:
        """Another instance (the plugin's) is taking over: step aside."""
        try:
            stamp = QUIT_REQUEST.stat().st_mtime
        except OSError:
            return
        if stamp <= self.started_at:
            return
        log.info("另一个实例请求接管，退出")
        self.app.quit()

    def _forget_track(self) -> None:
        self.track_key = ""
        self._palette_key = None
        self._lyric_rev = None
        self._lyric_key = ""
        self.album_rgb = None
        self.card.set_lyric(LyricState(none=True))
        self._apply_palette(None)

    def _ensure_palette(self, track) -> None:
        """Ask for the cover colour once the cover bytes are actually available.

        Over the bridge the cover arrives a moment after the track does, so the
        request has to happen on whichever tick first has the image.
        """
        key = (track.key, bool(track.cover), bool(track.cover_pending))
        if key == self._palette_key:
            return
        self._palette_key = key
        if track.cover:
            self.palette_worker.request(track.key, track.cover)
        elif not track.cover_pending:
            self.album_rgb = None
            self._apply_palette(None)

    def _poll_source_lyrics(self, track) -> None:
        """Lyrics that the source owns (demo, bridge) can arrive after the track."""
        payload = self.source.lyrics()
        if payload is None or payload.key != track.key or payload.rev == self._lyric_rev:
            return
        self._lyric_rev = payload.rev
        self._lyric_key = payload.key
        self.card.set_lyric(self._lyric_state(payload.key, payload.lines, payload.none))

    def _request_lyrics(self, track) -> None:
        if self.source.lyrics() is not None:
            self._poll_source_lyrics(track)
            return
        if self.cfg.get("source") == "bridge" or isinstance(self.source, BridgeSource):
            return
        if self.cfg.get("lyrics") == "off":
            self.card.set_lyric(LyricState(key=track.key, none=True))
            self._lyric_key = track.key
            return
        self.lyrics.request(track.key, track.title, track.artists)

    def _lyric_ready(self, key: str, lines: list[dict], none: bool) -> None:
        self.lyric_bridge.ready.emit(key, lines, none)

    def _on_lyrics(self, key: str, lines: list[dict], none: bool) -> None:
        if key != self.track_key:
            return
        if lines:
            log.info("歌词: %s 共 %d 行", key, len(lines))
        self._lyric_key = key
        self.card.set_lyric(self._lyric_state(key, lines, none))

    @staticmethod
    def _lyric_state(key: str, lines: list[dict], none: bool) -> LyricState:
        return LyricState(
            key=key,
            lines=tuple(
                LyricLine(
                    time=int(line.get("t", 0)),
                    text=str(line.get("o", "")),
                    translation=str(line.get("tr", "")),
                    duration=int(line.get("d", 0)),
                )
                for line in lines
            ),
            none=none,
        )

    # -------------------------------------------------------------- palette
    def _on_palette(self, key: str, rgb) -> None:
        if key != self.track_key:
            return
        self.album_rgb = rgb
        self._apply_palette(rgb)

    def _apply_palette(self, rgb) -> None:
        light = bool(self.cfg.get("light"))
        if self.cfg.get("bgMode") == "custom":
            base = colors.hex_to_rgb(self.cfg.get("customColor")) or colors.hex_to_rgb(
                colors.DEFAULT_ALBUM_COLOR
            )
            palette = colors.build_palette(base, True, light)
        else:
            base = rgb or colors.hex_to_rgb(colors.DEFAULT_ALBUM_COLOR)
            palette = colors.build_palette(base, False, light)
        self.card.set_palette(palette)

    # -------------------------------------------------------------- actions
    def _on_action(self, action: str) -> None:
        if action == "close":
            self.hide()
            if isinstance(self.source, BridgeSource):
                self.source.command("close")
            log.info("悬浮窗: 已隐藏（托盘图标可以再打开）")
            return
        if action == "settings":
            self.open_settings()
            return
        snapshot = self.source.snapshot()
        if snapshot.track is None:
            return
        self.source.command(action)
        # the source answers with its own optimistic state, so the button reacts now
        self.card.set_snapshot(self.source.snapshot())
        label = MODE_LABELS.get(mode_of(snapshot), "")
        log.info("控制: %s%s", action, f"（{label}）" if action == "mode" else "")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mfp",
        description="Material 风格悬浮播放器：独立进程、置顶、可拖动，播放信息来自系统媒体会话。",
    )
    parser.add_argument("--demo", action="store_true", help="用内置演示数据启动，不读系统媒体会话")
    parser.add_argument(
        "--bridge",
        metavar="PATH",
        help="由 BetterNCM 插件驱动：读插件写出的状态文件（PATH），把命令写回 PATH.cmd",
    )
    parser.add_argument("--settings", action="store_true", help="启动时直接打开设置")
    parser.add_argument("--reset-position", action="store_true", help="忽略记住的位置，回到默认角落")
    parser.add_argument("--screenshot", metavar="PATH", help="渲染一张卡片截图后退出（用于检查界面）")
    parser.add_argument("--version", action="version", version=f"MaterialFloatPlayerPy {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    win.set_dpi_awareness()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(data_dir() / "player.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    config = Config()
    if args.bridge:
        # the plugin is driving us: remember which channel we came in on, so the
        # settings window shows the truth instead of the last saved source
        config.update({"source": "bridge", "bridgePath": args.bridge})
    if args.reset_position:
        config.reset_position()

    # one card at a time; screenshots are one-off renders and skip the check
    lock = take_single_instance_lock(
        bridge_mode=bool(args.bridge), skip=bool(args.screenshot)
    )
    if not lock:
        return 0

    player = FloatingPlayer(app, config, args)
    app.aboutToQuit.connect(player.shutdown)

    if args.screenshot:
        _screenshot(app, player, args.screenshot)
        return app.exec()

    player.start()
    if args.settings:
        player.open_settings()
    _announce(player, args)
    return app.exec()


def _announce(player: FloatingPlayer, args) -> None:
    """Tell the console what is going on; the tray keeps it discoverable afterwards."""
    snapshot = player.source.snapshot()
    track = snapshot.track
    print(f"MaterialFloatPlayerPy {__version__} - 媒体来源: {player.source.name}", flush=True)
    if track is not None:
        print(f"当前曲目: {track.title} - {track.artists} ({fmt_time(track.duration)})", flush=True)
    else:
        print("当前没有播放中的媒体会话；播放器开始播放后这里会自动显示。", flush=True)
    print("托盘图标：显示/隐藏悬浮窗、设置、退出。", flush=True)


def _screenshot(app: QApplication, player: FloatingPlayer, path: str) -> None:
    """Render the card to a file - handy for reviewing the look without a player."""
    player.show()

    def capture() -> None:
        try:
            player.card.advance_animations()
            pixmap = player.window.grab()
            pixmap.save(path)
            print(f"截图已保存: {path} ({pixmap.width()}x{pixmap.height()})", flush=True)
        finally:
            app.quit()

    QTimer.singleShot(1600, capture)
