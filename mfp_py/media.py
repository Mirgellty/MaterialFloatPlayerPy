"""Playback data model shared by every source.

The plugin read NetEase Cloud Music's DOM and its own JS API. Standalone the
equivalent data comes from the Windows media session (SMTC), so the sources here
produce one small immutable snapshot and accept one small set of commands; the
UI never talks to a player directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Track:
    """Everything that identifies the current song; ``key`` also drives caching."""

    key: str
    title: str
    artists: str
    album: str
    duration: float
    cover: bytes | None = None
    cover_pending: bool = False   # cover is still being fetched for this track

    def label(self) -> str:
        if self.artists:
            return f"{self.title} - {self.artists}"
        return self.title


@dataclass(frozen=True)
class Snapshot:
    """One reading of the player, plus where it came from."""

    track: Track | None
    playing: bool
    position: float
    sampled_at: float = 0.0
    repeat: str = "none"        # none | track | list
    shuffle: bool = False
    app: str = ""
    source: str = "system"
    can_toggle: bool = True
    can_next: bool = True
    can_prev: bool = True
    can_mode: bool = True
    mode_kind: str = ""        # set when the source knows the mode exactly (bridge)

    def live_position(self, now: float | None = None) -> float:
        """Extrapolate the position: the source only samples a few times a second."""
        position = max(0.0, self.position)
        if self.playing:
            position += max(0.0, (now or time.monotonic()) - self.sampled_at)
        duration = self.track.duration if self.track else 0.0
        if duration > 0:
            position = min(position, duration)
        return position


class MediaSource(Protocol):
    """What the UI needs from a player."""

    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> Snapshot: ...

    def lyrics(self) -> "LyricPayload | None":
        """Sources that own their lyrics (demo, bridge) return them here;
        sources that do not (system) return None and the app looks them up."""
        ...

    def command(self, action: str) -> None:
        """One of: toggle, play, pause, next, prev, mode."""
        ...


EMPTY = Snapshot(track=None, playing=False, position=0.0)


@dataclass
class LyricPayload:
    """Lyrics plus the identity/version they belong to."""

    key: str
    rev: int
    lines: list[dict]
    none: bool = False


def mode_of(snapshot: Snapshot) -> str:
    """Collapse shuffle/repeat into the four modes the plugin showed in its button."""
    if snapshot.mode_kind:
        return snapshot.mode_kind
    if snapshot.shuffle:
        return "random"
    return {"track": "one", "list": "repeat", "none": "order"}.get(snapshot.repeat, "order")


MODE_LABELS = {
    "repeat": "列表循环",
    "one": "单曲循环",
    "random": "随机播放",
    "order": "顺序播放",
    "ai": "心动模式",
}

# 循环顺序和插件一致：列表循环 -> 单曲循环 -> 随机播放 -> 顺序播放
MODE_CYCLE = ("repeat", "one", "random", "order")
