"""Playback source backed by the Windows media session (SMTC).

This is what replaces "read NetEase Cloud Music's DOM and JS API" in the plugin:
any player that talks to the Windows media controls - NetEase Cloud Music,
browsers, Spotify, foobar2000, the Windows film & TV player - publishes its
current track, cover, position and playback status there, and accepts
play/pause/next/previous through the same channel.

All COM work happens on one worker thread with its own asyncio loop: WinRT
objects are apartment-bound, so the Qt thread only ever reads the cached
snapshot this module publishes.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
import time

from .media import MODE_CYCLE, Snapshot, Track

# PlaybackStatus from Windows.Media.Control: 4 = playing.
_PLAYING = 4
_PAUSED = 5

# A player that stops refreshing its timeline should not make the position run away.
_MAX_EXTRAPOLATION = 10.0

APP_NAMES = {
    "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ 音乐",
    "spotify.exe": "Spotify",
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Firefox",
    "foobar2000.exe": "foobar2000",
    "potplayermini64.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "zunemusic.exe": "Windows 媒体播放器",
}


def friendly_app(app_id: str) -> str:
    key = (app_id or "").lower()
    for name, label in APP_NAMES.items():
        if name in key:
            return label
    if not key:
        return "系统媒体会话"
    return app_id.rsplit("!", 1)[0].rsplit("\\", 1)[-1]


def seconds(value) -> float:
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def read_stream_bytes(reference) -> bytes:
    """Pull an IRandomAccessStreamReference (the cover) into bytes.

    pywinrt exposes IBuffer through the buffer protocol, which is the cheap path;
    the InputStream fallback is kept for builds where that is not the case.
    """
    stream = await reference.open_read_async()
    size = int(stream.size)
    if size <= 0:
        return b""
    from winrt.windows.storage.streams import Buffer, DataReader

    try:
        reader = DataReader(stream)
        await reader.load_async(size)
        buffer = reader.read_buffer(size)
        return bytes(memoryview(buffer))
    except Exception:  # noqa: BLE001 - fall through to the slower path
        stream.seek(0)
        buffer = Buffer(size)
        read = stream.get_input_stream_at(0).read_async(buffer, size, 0).get()
        view = memoryview(buffer)
        return bytes(view[: int(read) or size])


class SystemMediaSource:
    """Polls SMTC a few times a second on its own thread and mirrors it."""

    name = "system"

    def __init__(self, interval: float = 0.5, preferred_app: str = "") -> None:
        self.interval = interval
        self.preferred_app = preferred_app
        self._snapshot = Snapshot(track=None, playing=False, position=0.0)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        self._identity = ""
        self._cover: bytes | None = None
        self._mode_override: tuple[str, float] | None = None
        self._play_override: tuple[bool, float] | None = None
        self._last_error = ""

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="mfp-smtc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def snapshot(self) -> Snapshot:
        return self._snapshot

    def lyrics(self):
        """No lyrics of its own: the app looks them up with LyricProvider."""
        return None

    def last_error(self) -> str:
        return self._last_error

    # -- thread -------------------------------------------------------------
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._poll_forever())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None

    async def _poll_forever(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        manager = None
        while not self._stopping.is_set():
            try:
                if manager is None:
                    manager = await SessionManager.request_async()
                await self._refresh(manager)
                self._last_error = ""
            except Exception as exc:  # noqa: BLE001 - keep the widget alive
                self._last_error = f"{type(exc).__name__}: {exc}"
                manager = None
                await asyncio.sleep(1.5)
                continue
            await asyncio.sleep(self.interval)

    async def _refresh(self, manager) -> None:
        session = self._pick_session(manager)
        if session is None:
            self._identity = ""
            self._cover = None
            self._publish(Snapshot(track=None, playing=False, position=0.0, app="", source="system"))
            return

        info = session.get_playback_info()
        playing = int(info.playback_status) == _PLAYING
        override = self._play_override
        if override is not None:
            if time.monotonic() < override[1]:
                playing = override[0]
            else:
                self._play_override = None

        timeline = session.get_timeline_properties()
        position = seconds(timeline.position)
        duration = seconds(timeline.end_time)
        if playing:
            # SMTC timestamps the last timeline update; carry the position forward.
            updated = timeline.last_updated_time
            if updated is not None:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=datetime.timezone.utc)
                drift = (_utc_now() - updated).total_seconds()
                position += min(max(drift, 0.0), _MAX_EXTRAPOLATION)

        properties = await session.try_get_media_properties_async()
        title = (properties.title or "").strip()
        artists = (properties.artist or "").strip()
        album = (properties.album_title or "").strip()

        identity = f"{title}|{artists}|{album}"
        if identity != self._identity:
            self._identity = identity
            self._cover = await self._read_cover(properties)
        if not title and not artists:
            self._cover = None

        repeat = {0: "none", 1: "track", 2: "list"}.get(int(info.auto_repeat_mode), "none")
        shuffle = bool(info.is_shuffle_active)
        mode = "random" if shuffle else {"track": "one", "list": "repeat", "none": "order"}[repeat]
        active_override = self._mode_override
        if active_override is not None:
            if time.monotonic() < active_override[1]:
                mode = active_override[0]
                shuffle = mode == "random"
                repeat = {"one": "track", "repeat": "list", "random": "list", "order": "none"}[mode]
            else:
                self._mode_override = None

        controls = info.controls
        track = None
        if title or artists:
            track = Track(
                key=identity,
                title=title or "(未知曲目)",
                artists=artists,
                album=album,
                duration=duration,
                cover=self._cover,
            )
        self._publish(
            Snapshot(
                track=track,
                playing=playing,
                position=position,
                sampled_at=time.monotonic(),
                repeat=repeat,
                shuffle=shuffle,
                app=friendly_app(session.source_app_user_model_id),
                source="system",
                can_toggle=bool(getattr(controls, "is_play_pause_toggle_enabled", True)),
                can_next=bool(getattr(controls, "is_next_enabled", True)),
                can_prev=bool(getattr(controls, "is_previous_enabled", True)),
                can_mode=mode in MODE_CYCLE,
            )
        )

    async def _read_cover(self, properties) -> bytes | None:
        reference = properties.thumbnail
        if reference is None:
            return None
        try:
            data = await read_stream_bytes(reference)
        except Exception:  # noqa: BLE001 - a missing cover must not break the widget
            return None
        return data or None

    def _pick_session(self, manager):
        sessions = list(manager.get_sessions())
        if not sessions:
            return None
        if self.preferred_app:
            want = self.preferred_app.lower()
            for session in sessions:
                if want in (session.source_app_user_model_id or "").lower():
                    return session
        current = manager.get_current_session()
        if current is not None:
            return current
        if self._identity:
            for session in sessions:
                if session.source_app_user_model_id and self._identity.startswith(
                    session.source_app_user_model_id
                ):
                    return session
        return sessions[0]

    def _publish(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    # -- commands -----------------------------------------------------------
    def command(self, action: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # Answer immediately: the real round trip through SMTC takes a moment, and
        # the plugin flipped locally for the same reason.
        self._optimistic(action)
        loop.call_soon_threadsafe(self._spawn, action)

    def _optimistic(self, action: str) -> None:
        from dataclasses import replace

        current = self._snapshot
        if action in ("toggle", "play", "pause"):
            playing = (not current.playing) if action == "toggle" else action == "play"
            self._play_override = (playing, time.monotonic() + 2.0)
            self._publish(
                replace(
                    current,
                    playing=playing,
                    position=current.live_position(),
                    sampled_at=time.monotonic(),
                )
            )
        elif action == "mode":
            mode = "random" if current.shuffle else {
                "track": "one",
                "list": "repeat",
                "none": "order",
            }.get(current.repeat, "order")
            index = MODE_CYCLE.index(mode) if mode in MODE_CYCLE else 0
            target = MODE_CYCLE[(index + 1) % len(MODE_CYCLE)]
            self._mode_override = (target, time.monotonic() + 2.5)
            self._publish(
                replace(
                    current,
                    shuffle=target == "random",
                    repeat={"one": "track", "repeat": "list", "random": "list", "order": "none"}[target],
                )
            )

    def _spawn(self, action: str) -> None:
        assert self._loop is not None
        self._loop.create_task(self._apply(action))

    async def _apply(self, action: str) -> None:
        manager = await self._manager()
        session = self._pick_session(manager) if manager is not None else None
        if session is None:
            return
        try:
            if action == "toggle":
                await session.try_toggle_play_pause_async()
            elif action == "play":
                await session.try_play_async()
            elif action == "pause":
                await session.try_pause_async()
            elif action == "next":
                await session.try_skip_next_async()
            elif action == "prev":
                await session.try_skip_previous_async()
            elif action == "mode":
                await self._cycle_mode(session)
        except Exception as exc:  # noqa: BLE001 - a refused command is not fatal
            self._last_error = f"{type(exc).__name__}: {exc}"

    async def _manager(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        return await SessionManager.request_async()

    async def _cycle_mode(self, session) -> None:
        from winrt.windows.media import MediaPlaybackAutoRepeatMode

        current = self._snapshot
        mode = "random" if current.shuffle else {
            "track": "one",
            "list": "repeat",
            "none": "order",
        }.get(current.repeat, "order")
        index = MODE_CYCLE.index(mode) if mode in MODE_CYCLE else 0
        target = MODE_CYCLE[(index + 1) % len(MODE_CYCLE)]
        if target == "random":
            await session.try_change_shuffle_active_async(True)
            await session.try_change_auto_repeat_mode_async(MediaPlaybackAutoRepeatMode.LIST)
        else:
            await session.try_change_shuffle_active_async(False)
            await session.try_change_auto_repeat_mode_async(
                {
                    "repeat": MediaPlaybackAutoRepeatMode.LIST,
                    "one": MediaPlaybackAutoRepeatMode.TRACK,
                    "order": MediaPlaybackAutoRepeatMode.NONE,
                }[target]
            )
