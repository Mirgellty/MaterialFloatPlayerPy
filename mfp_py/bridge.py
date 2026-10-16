"""Bridge source: the NetEase plugin owns the data, this process owns the window.

This is the "put the Python architecture inside the plugin" path. The plugin
keeps doing what only it can do - read NetEase's own playback state, pull lyrics
with translation through liblyric, drive the real player buttons - and pushes
that into a small set of files. This process renders the card in its own
per-pixel-alpha window and writes commands back.

Every file is derived from one base path (``--bridge <path>``):

    <path>          state, written by the plugin on every tick
    <path>.lyric    lyrics, written by the plugin when they change
    <path>.ctl      commands, plugin -> this process (show / hide / quit / ...)
    <path>.cmd      commands, this process -> plugin (toggle / next / mode / ...)
    <path>.beat     heartbeat, this process -> plugin (so it can supervise us)

File-based on purpose: the plugin's renderer can write files through BetterNCM's
fs API (it already writes its log and its PowerShell helper that way), so this
needs no sockets, no ports and no extra permissions.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import cache_dir
from .media import LyricPayload, Snapshot, Track
from .smtc import friendly_app

STATE_SUFFIX = ""
LYRIC_SUFFIX = ".lyric"
CTL_SUFFIX = ".ctl"
CMD_SUFFIX = ".cmd"
BEAT_SUFFIX = ".beat"

STALE_AFTER = 6.0      # seconds without a plugin tick before the card goes idle
BEAT_EVERY = 1.0       # heartbeat period


class _JsonFile:
    """Reads a small JSON file, re-parsing only when it actually changed.

    A half-written file parses as garbage; in that case the previous reading is
    kept so the card does not blink back to the idle state twice a second.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._data: object = None

    def read(self):
        try:
            stat = self.path.stat()
        except OSError:
            self._stamp = None
            self._data = None
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return self._data
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._data
        self._stamp = stamp
        self._data = payload
        return payload

    def write(self, payload: dict) -> None:
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


class BridgeSource:
    """Playback state pushed by the BetterNCM plugin."""

    name = "bridge"

    def __init__(self, state_path: str, on_command=None) -> None:
        base = Path(state_path)
        self.base = base
        self.state = _JsonFile(base)
        self.lyric_file = _JsonFile(Path(str(base) + LYRIC_SUFFIX))
        self.ctl_file = _JsonFile(Path(str(base) + CTL_SUFFIX))
        self.cmd_file = _JsonFile(Path(str(base) + CMD_SUFFIX))
        self.beat_file = _JsonFile(Path(str(base) + BEAT_SUFFIX))
        self.on_command = on_command
        self._seq = int(time.time() * 1000)
        self._ctl_seq = 0
        self._started_at = time.time()
        self._beat_at = 0.0
        self._covers: dict[str, bytes | None] = {}
        self._fetching: set[str] = set()
        self._lyric_payload: LyricPayload | None = None
        self._lyric_stamp: tuple[int, int] | None = None
        self._pos_track = ""
        self._pos_value = 0.0
        self._last_error = ""

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._write_beat()

    def stop(self) -> None:
        pass

    def last_error(self) -> str:
        return self._last_error

    # -------------------------------------------------------------- reading
    def snapshot(self) -> Snapshot:
        self._poll_control()
        self._maybe_beat()
        data = self.state.read()
        if not isinstance(data, dict):
            return self._idle("网易云音乐")

        age = time.time() * 1000.0 - float(data.get("ts") or 0)
        if age > STALE_AFTER * 1000:
            return self._idle("网易云音乐")

        track_data = data.get("track") or {}
        title = str(track_data.get("title") or "")
        artists = str(track_data.get("artists") or "")
        if not title and not artists:
            return self._idle(friendly_app(str(data.get("app") or "")))

        cover_url = str(track_data.get("cover") or "")
        # the plugin sends its own key (NetEase song id when it has one); fall back
        # to title|artists|album so a hand-written state file still works
        identity = str(track_data.get("key") or f"{title}|{artists}|{track_data.get('album') or ''}")
        cover = self._cover_for(cover_url) if cover_url else None
        track = Track(
            key=identity,
            title=title or "(未知曲目)",
            artists=artists,
            album=str(track_data.get("album") or ""),
            duration=float(track_data.get("duration") or 0.0),
            cover=cover,
            # a cover URL that has not been fetched *yet* must not make the card
            # fall back to the default colour halfway through a track change
            cover_pending=cover is None and cover_url in self._fetching,
        )
        position = max(0.0, float(data.get("position") or 0.0))
        playing = bool(data.get("playing"))
        if playing:
            # the plugin stamped the reading; carry the position forward from there
            position += min(max(age / 1000.0, 0.0), 10.0)
        # The play bar only ticks in whole seconds, so a fresh reading can be a
        # fraction *behind* what we already showed. Keep it monotonic instead of
        # letting the progress bar and the clock jump backwards every second.
        if identity != self._pos_track:
            self._pos_track = identity
            self._pos_value = position
        elif playing and position < self._pos_value < position + 2.0:
            position = self._pos_value
        self._pos_value = position
        return Snapshot(
            track=track,
            playing=playing,
            position=position,
            sampled_at=time.monotonic(),
            repeat=str(data.get("repeat") or "none"),
            shuffle=bool(data.get("shuffle")),
            app=friendly_app(str(data.get("app") or "")) or "网易云音乐",
            source="bridge",
            can_toggle=bool(data.get("canToggle", True)),
            can_next=bool(data.get("canNext", True)),
            can_prev=bool(data.get("canPrev", True)),
            can_mode=bool(data.get("canMode", True)),
            mode_kind=str(data.get("modeKind") or ""),
        )

    def _idle(self, app: str) -> Snapshot:
        return Snapshot(track=None, playing=False, position=0.0, app=app, source="bridge")

    def lyrics(self) -> LyricPayload | None:
        """Lyrics come from the plugin, so they carry its translation and timing."""
        path = self.lyric_file.path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        if stamp == self._lyric_stamp and self._lyric_payload is not None:
            return self._lyric_payload
        data = self.lyric_file.read()
        if not isinstance(data, dict):
            return self._lyric_payload
        self._lyric_stamp = stamp
        lines = data.get("lines") or []
        self._lyric_payload = LyricPayload(
            key=str(data.get("key") or ""),
            rev=int(data.get("rev") or 0),
            lines=[line for line in lines if isinstance(line, dict)],
            none=not lines,
        )
        return self._lyric_payload

    # -------------------------------------------------------------- commands
    def command(self, action: str) -> None:
        self._seq += 1
        self.cmd_file.write({"seq": self._seq, "action": action, "ts": time.time() * 1000})

    def _poll_control(self) -> None:
        data = self.ctl_file.read()
        if not isinstance(data, dict):
            return
        # a command left over from the last session must not be replayed now
        if float(data.get("ts") or 0) < self._started_at * 1000:
            return
        seq = int(data.get("seq") or 0)
        if seq <= self._ctl_seq:
            return
        self._ctl_seq = seq
        if self.on_command is not None:
            self.on_command(str(data.get("action") or ""))

    # ------------------------------------------------------------- heartbeat
    def _maybe_beat(self) -> None:
        if time.monotonic() - self._beat_at >= BEAT_EVERY:
            self._write_beat()

    def _write_beat(self) -> None:
        self._beat_at = time.monotonic()
        self.beat_file.write(
            {
                "ts": time.time() * 1000,
                "pid": __import__("os").getpid(),
                "source": self.name,
            }
        )

    # ---------------------------------------------------------------- covers
    def _cover_for(self, url: str) -> bytes | None:
        """Cover art arrives as a URL (or a local path); fetch it once, cache on disk."""
        if url in self._covers:
            return self._covers[url]
        if url in self._fetching:
            return None
        self._fetching.add(url)
        threading.Thread(
            target=self._fetch_cover, args=(url,), name="mfp-cover", daemon=True
        ).start()
        return None

    def _fetch_cover(self, url: str) -> None:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        folder = cache_dir() / "covers"
        cached = folder / digest
        payload: bytes | None = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if cached.is_file():
                payload = cached.read_bytes()
            else:
                payload = self._download(url)
                if payload:
                    cached.write_bytes(payload)
        except OSError as exc:
            self._last_error = f"cover: {exc}"
        self._covers[url] = payload
        self._fetching.discard(url)

    @staticmethod
    def _download(url: str) -> bytes | None:
        if url.startswith("file://"):
            url = url[7:]
        if "://" not in url:
            try:
                return Path(url).read_bytes()
            except OSError:
                return None
        import requests

        try:
            response = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "MaterialFloatPlayerPy", "Referer": "https://music.163.com/"},
            )
            response.raise_for_status()
            return response.content
        except Exception:  # noqa: BLE001 - a missing cover just falls back
            return None
