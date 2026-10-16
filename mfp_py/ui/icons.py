"""Material icon set, rendered from the same SVG paths the plugin uses.

Keeping the path data identical in both projects means an icon tweak only has to
be reasoned about once. Each path is a 24x24 outline; QSvgRenderer rasterises it
at the requested size and colour, and results are cached per (name, size, colour).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap

ICONS = {
    "prev": "M6 6h2v12H6zm3.5 6l8.5 6V6z",
    "next": "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z",
    "play": "M8 5v14l11-7z",
    "pause": "M6 19h4V5H6v14zm8-14v14h4V5h-4z",
    "shuffle": (
        "M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 "
        "5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5"
        "l-2.04 2.04-3.13-3.13z"
    ),
    "repeat": "M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z",
    "repeatOne": (
        "M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4zm-4-2V9h-1l-2 1v1h1.5v4H13z"
    ),
    "order": "M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z",
    "heart": (
        "M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 "
        "2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"
    ),
    "close": (
        "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 "
        "17.59 13.41 12z"
    ),
    "note": "M12 3v10.55A4 4 0 1014 17V7h4V3h-6z",
    "settings": (
        "M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32"
        "a.49.49 0 00-.59-.22l-2.39.96a7.03 7.03 0 00-1.62-.94l-.36-2.54a.48.48 0 00-.48-.41h-3.84"
        "a.48.48 0 00-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87"
        "a.48.48 0 00.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61"
        "l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84"
        "c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.57 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32"
        "a.49.49 0 00-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1112 8.4a3.6 3.6 0 010 7.2z"
    ),
    "playlist": "M3 10h11v2H3v-2zm0-5h11v2H3V5zm0 10h7v2H3v-2zm12 0v7l5-3.5-5-3.5z",
}

_CACHE: dict[tuple[str, int, str], QPixmap] = {}


def icon_pixmap(name: str, size: int, color: str) -> QPixmap:
    """A rasterised, colour-tinted icon at ``size`` device-independent pixels."""
    key = (name, int(size), color)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    path = ICONS.get(name) or ICONS["note"]
    document = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<path d="{path}" fill="{color}"/></svg>'
    )
    image = QImage(int(size), int(size), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    try:
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(bytearray(document.encode("utf-8")))
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
    except Exception:  # noqa: BLE001 - a missing QtSvg must not break the widget
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(Qt.white)
        margin = size * 0.2
        painter.drawEllipse(margin, margin, size - margin * 2, size - margin * 2)
        painter.end()

    pixmap = QPixmap.fromImage(image)
    _CACHE[key] = pixmap
    return pixmap


MODE_ICONS = {
    "repeat": "repeat",
    "one": "repeatOne",
    "random": "shuffle",
    "order": "order",
    "ai": "heart",
}
