"""Lyrics for whatever the system media session is playing.

The plugin asked liblyric or LibEAPIRequest for lyrics keyed by NetEase song id.
Standalone there is no song id, only a title and artist, so lyrics come from:

* a ``<title>.lrc`` / ``<title> - <artist>.lrc`` file in the app's lyrics folder,
* optionally the public NetEase lyric API, located by searching for the title.

Everything is parsed with the same LRC rules as the plugin (multiple timestamps
per line, translation merged onto the nearest original line, per-line durations
for the marquee timing) and cached on disk so a song is only fetched once.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable

from .config import cache_dir, lyrics_dir

TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
SEARCH_URL = "https://music.163.com/api/search/get/web"
LYRIC_URL = "https://music.163.com/api/song/lyric"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Cookie": "appver=2.10.13;",
}


def parse_lrc(text: str) -> list[dict]:
    lines: list[dict] = []
    if not text:
        return lines
    for raw in str(text).splitlines():
        stamps = []
        for match in TIME_TAG.finditer(raw):
            fraction = match.group(3) or ""
            millis = int(fraction + "0" if len(fraction) == 2 else (fraction or "0")) if fraction else 0
            stamps.append((int(match.group(1)) * 60 + int(match.group(2))) * 1000 + millis)
        if not stamps:
            continue
        content = TIME_TAG.sub("", raw).strip()
        for stamp in stamps:
            lines.append({"time": stamp, "lyric": content})
    lines.sort(key=lambda line: line["time"])
    return lines


def merge_translation(base: list[dict], translation: list[dict]) -> list[dict]:
    if not translation:
        return base
    for item in translation:
        best = None
        for candidate in base:
            if best is None or abs(candidate["time"] - item["time"]) < abs(best["time"] - item["time"]):
                best = candidate
        if best is not None and abs(best["time"] - item["time"]) <= 1200:
            best["tr"] = item["lyric"]
    return base


def fill_durations(lines: list[dict]) -> list[dict]:
    """Give every line a duration so the marquee knows how long to take.

    Accepts either the parsed shape (``time``) or the shape the UI uses (``t``).
    """

    def stamp(line: dict) -> int:
        return int(line.get("t", line.get("time", 0)) or 0)

    for index, line in enumerate(lines):
        if not line.get("d"):
            following = lines[index + 1] if index + 1 < len(lines) else None
            line["d"] = max(1200, stamp(following) - stamp(line)) if following else 6000
    return lines


def normalise(lines: list[dict]) -> list[dict]:
    out = []
    for line in lines:
        if not line.get("lyric"):
            continue
        out.append(
            {
                "t": int(line["time"]),
                "o": line["lyric"],
                "tr": line.get("tr", ""),
                "d": int(line.get("d", 0)),
            }
        )
    return fill_durations(out)


class LyricProvider:
    """Fetches lyrics in the background; ``on_ready(key, lines, none)`` reports back."""

    def __init__(self, mode: str = "online", on_ready: Callable | None = None) -> None:
        self.mode = mode
        self.on_ready = on_ready
        self._lock = threading.Lock()
        self._wanted = ""
        self._done: dict[str, list[dict]] = {}

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def cache_path(self, key: str) -> str:
        import hashlib

        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return str(cache_dir() / "lyrics" / f"{digest}.json")

    def request(self, key: str, title: str, artists: str) -> None:
        """Ask for lyrics for one track; a later request supersedes an earlier one."""
        with self._lock:
            self._wanted = key
            cached = self._done.get(key)
        if cached is not None:
            self._report(key, cached)
            return
        thread = threading.Thread(
            target=self._work, args=(key, title, artists), name="mfp-lyrics", daemon=True
        )
        thread.start()

    # -- worker -------------------------------------------------------------
    def _work(self, key: str, title: str, artists: str) -> None:
        lines = self._load_cache(key)
        if lines is None:
            lines = self._from_file(title, artists)
        if lines is None and self.mode == "online":
            lines = self._from_web(key, title, artists)
        if lines is None:
            lines = []
        with self._lock:
            self._done[key] = lines
            stale = key != self._wanted
        if not stale:
            self._report(key, lines)

    def _report(self, key: str, lines: list[dict]) -> None:
        if self.on_ready is not None:
            self.on_ready(key, list(lines), not lines)

    def _load_cache(self, key: str) -> list[dict] | None:
        try:
            with open(self.cache_path(key), encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        lines = payload.get("lines")
        return lines if isinstance(lines, list) else None

    def _store_cache(self, key: str, lines: list[dict]) -> None:
        try:
            with open(self.cache_path(key), "w", encoding="utf-8") as handle:
                json.dump({"lines": lines}, handle, ensure_ascii=False)
        except OSError:
            pass

    def _from_file(self, title: str, artists: str) -> list[dict] | None:
        folder = lyrics_dir()
        for name in (f"{title} - {artists}.lrc", f"{artists} - {title}.lrc", f"{title}.lrc"):
            path = folder / name.replace("/", "_").replace("\\", "_")
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                return normalise(parse_lrc(text))
        return None

    def _from_web(self, key: str, title: str, artists: str) -> list[dict] | None:
        import requests

        try:
            query = f"{title} {artists}".strip()
            response = requests.get(
                SEARCH_URL,
                params={"s": query, "type": 1, "offset": 0, "total": "true", "limit": 5},
                headers=HEADERS,
                timeout=8,
            )
            response.raise_for_status()
            songs = (response.json().get("result") or {}).get("songs") or []
            if not songs:
                return None
            song_id = songs[0].get("id")
            if not song_id:
                return None
            response = requests.get(
                LYRIC_URL,
                params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                headers=HEADERS,
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - offline or the API changed; not fatal
            return None

        base = parse_lrc((payload.get("lrc") or {}).get("lyric", ""))
        merge_translation(base, parse_lrc((payload.get("tlyric") or {}).get("lyric", "")))
        lines = normalise(base)
        self._store_cache(key, lines)
        return lines
