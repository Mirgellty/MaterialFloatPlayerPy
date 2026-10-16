"""Settings storage.

The plugin kept settings inside BetterNCM (plugin.getConfig). Standalone we keep
a plain JSON file under %APPDATA%, with the same keys and ranges so behaviour
stays comparable between the two implementations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "MaterialFloatPlayerPy"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "bgMode": "album",           # album | custom
    "customColor": "#6750a4",
    "light": False,
    "width": 384,                # 260 - 720
    "radius": 16,                # 0 - 32
    # 五处文字的字号可以分开调（旧版的单一 fontSize 会在读取时换算过来）
    "fontTitle": 15,             # 歌名
    "fontArtist": 10,            # 作者 / 专辑
    "fontCurrent": 13,           # 正在播放的那句歌词（也决定按钮大小）
    "fontLyric": 11,             # 副行显示下一句时的字号
    "fontTranslation": 11,       # 副行显示翻译时的字号
    "opacity": 1.0,              # 0.4 - 1
    "translation": True,
    "showTime": True,
    "alwaysOnTop": True,
    "gameTop": True,
    "shadow": True,
    "zen": False,
    "source": "system",          # system | demo | bridge
    "bridgePath": "",            # state file written by the NetEase plugin
    "lyrics": "online",          # online | local | off
    "pos": None,                 # {"x": int, "y": int} logical pixels
}

RANGES = {
    "width": (260, 720),
    "radius": (0, 32),
    "fontTitle": (8, 40),
    "fontArtist": (8, 30),
    "fontCurrent": (8, 40),
    "fontLyric": (8, 40),
    "fontTranslation": (8, 40),
    "opacity": (0.4, 1.0),
}

FONT_KEYS = ("fontTitle", "fontArtist", "fontCurrent", "fontLyric", "fontTranslation")

# 1.x/2.x 只有一个 fontSize，按当时的比例换算成五个
FONT_FROM_BASE = {
    "fontTitle": 1.18,
    "fontArtist": 0.78,
    "fontCurrent": 1.0,
    "fontLyric": 0.84,
    "fontTranslation": 0.84,
}


def data_dir() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home())
    path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def lyrics_dir() -> Path:
    path = data_dir() / "lyrics"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    """Dict-like settings object that persists itself on every write."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "config.json")
        self._values: dict[str, Any] = dict(DEFAULTS)
        self.load()

    # -- io -----------------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            # 旧配置只有一个 fontSize：按旧比例补出五个字号，用户不用重调
            base = raw.get("fontSize")
            if isinstance(base, (int, float)) and not any(key in raw for key in FONT_KEYS):
                for key, ratio in FONT_FROM_BASE.items():
                    raw[key] = round(float(base) * ratio * 2) / 2
            for key, default in DEFAULTS.items():
                value = raw.get(key, default)
                self._values[key] = self._coerce(key, value, default)
            # keep unknown keys from newer versions instead of silently dropping them
            for key, value in raw.items():
                self._values.setdefault(key, value)

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._values, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _coerce(key: str, value: Any, default: Any) -> Any:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            low, high = RANGES.get(key, (float("-inf"), float("inf")))
            number = min(high, max(low, number))
            return int(number) if isinstance(default, int) else round(number, 3)
        return value

    # -- mapping-ish --------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._values.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._values[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self[key] = value

    def update(self, patch: dict[str, Any]) -> None:
        self._values.update(patch)
        self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def reset_position(self) -> None:
        self._values["pos"] = None
        self.save()
