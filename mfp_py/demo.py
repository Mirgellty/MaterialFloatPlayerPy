"""A self-contained demo player.

Useful for two things: designing the widget without a player running, and
checking it after a change without touching the user's music. tools/fake_player.py
reuses the cover renderer from here so the artwork looks the same either way.
"""

from __future__ import annotations

import io
import math
import time

from PIL import Image, ImageDraw

from .media import MODE_CYCLE, LyricPayload, Snapshot, Track

DEMO_TRACKS = (
    ("Dreamland", "Glass Animals", "Dreamland", 202.0),
    ("夜航西飞", "陈鸿宇", "一如年少模样", 268.0),
    ("Midnight City", "M83", "Hurry Up, We're Dreaming", 243.0),
    ("Weightless", "Marconi Union", "Weightless", 480.0),
)

_COVER_CACHE: dict[int, bytes] = {}


def _clamp(value: float, low: float = 0.0, high: float = 255.0) -> float:
    return min(high, max(low, value))


def render_cover(seed: int, size: int = 384) -> bytes:
    """A cover with a clear hue family so the palette code has something real to chew on."""
    cached = _COVER_CACHE.get(seed)
    if cached is not None:
        return cached
    hue = (seed * 97) % 360
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            mix = x / size
            wave_value = 0.5 + 0.5 * math.sin((y / size) * math.pi * 1.4 + mix * 3.0)
            angle = math.radians(hue + 40 * mix)
            pixels[x, y] = (
                int(_clamp(120 + 110 * wave_value + 60 * math.cos(angle))),
                int(_clamp(70 + 90 * (1 - mix) + 40 * math.sin(angle))),
                int(_clamp(150 + 80 * mix + 50 * math.cos(angle + 2))),
            )
    draw = ImageDraw.Draw(image)
    centre = size / 2
    for index in range(7):
        radius = size * (0.125 + index * 0.052)
        draw.ellipse(
            [centre - radius, centre - radius, centre + radius, centre + radius],
            outline=(255, 226, max(0, 150 - index * 12)),
            width=max(2, size // 77),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    _COVER_CACHE[seed] = data
    return data


def demo_track(index: int) -> Track:
    title, artists, album, duration = DEMO_TRACKS[index % len(DEMO_TRACKS)]
    return Track(
        key=f"demo:{index % len(DEMO_TRACKS)}",
        title=title,
        artists=artists,
        album=album,
        duration=duration,
        cover=render_cover(index % len(DEMO_TRACKS)),
    )


DEMO_LYRICS: dict[int, tuple[tuple[int, str, str], ...]] = {
    0: (
        (0, "Sippin' through dreamland like a tourist", "像游客一样在梦境里漫步"),
        (9200, "I'm on my way to somewhere I don't know", "我正前往一个我不知道的地方"),
        (17400, "The ceiling's making shapes above the bed", "天花板在床上方变幻着形状"),
        (25600, "And every color's louder in my head", "每一种颜色在脑海里都更响亮"),
        (33200, "Dreamland, dreamland, take me where the night is kind", "梦境，梦境，带我去夜晚温柔的地方"),
        (42000, "I've been sleeping with the lights on all this time", "这段时间我一直开着灯入睡"),
        (50800, "Wake me when the morning comes to get me", ""),
        (59600, "或者就让我留在这首歌结束之前再多待一会儿", ""),
        (70000, "Sippin' through dreamland like a tourist", "像游客一样在梦境里漫步"),
        (79000, "Nothing here is real and nothing hurts", "这里没有真实，也没有疼痛"),
    ),
    1: (
        (0, "夜航西飞", ""),
        (7800, "乘客们都睡了，只剩下引擎的低语", ""),
        (16200, "舷窗外是没有尽头的一片深蓝", ""),
        (24600, "我数着云层上面那些不肯熄灭的星", ""),
        (33000, "忽然想起很多年前的一句告别", ""),
        (42000, "我们都在夜里飞行，飞向各自的清晨", ""),
        (52000, "有些话说完就成了行李，一直背着", ""),
        (62000, "天亮之前，请让我再想一会儿", ""),
        (72000, "夜航西飞，谁不是这样飞过来的", ""),
    ),
    2: (
        (0, "Waiting in a car, waiting for a ride in the dark", "在车里等待，等待夜色中的一段旅程"),
        (9200, "The city's been asleep since I don't know when", "城市不知从什么时候开始沉睡"),
        (17600, "And the streetlights look like a necklace on the hill", "街灯像山丘上的一串项链"),
        (26400, "I'll be coming home to you, coming home to you", "我会回到你身边，回到你身边"),
        (35200, "Midnight city, the lights are calling out my name", "午夜之城，灯光在呼唤我的名字"),
        (44400, "And I keep driving faster just to keep the feeling sane", "我越开越快，只为让这种感觉保持清醒"),
        (53200, "Waiting in a car, waiting for a ride in the dark", "在车里等待，等待夜色中的一段旅程"),
    ),
    3: (
        (0, "Breathe in", ""),
        (12000, "Breathe out", ""),
        (24000, "Let the weight fall off your shoulders", "让重量从肩膀上滑落"),
        (38000, "The room is warm and nothing here is asking anything of you", "房间里很暖，这里没有任何事在要求你"),
        (54000, "Breathe in", ""),
        (66000, "Breathe out", ""),
        (80000, "You can stay as long as you need to", "你可以待多久都行"),
    ),
}


def demo_lyrics(key: str) -> list[dict] | None:
    """Hand-written lyrics for the demo tracks, so demo mode always shows a real marquee."""
    try:
        index = int(key.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    raw = DEMO_LYRICS.get(index % len(DEMO_TRACKS))
    if raw is None:
        return None
    from .lyrics import fill_durations

    lines = [
        {"t": time, "o": text, "tr": translation, "d": 0} for time, text, translation in raw
    ]
    return fill_durations(lines)


class DemoSource:
    """Fake playback that advances on its own, so the UI can be exercised offline."""

    name = "demo"

    def __init__(self) -> None:
        self._index = 0
        self._playing = True
        self._position = 6.0
        self._mode_index = 0
        self._last = time.monotonic()

    def start(self) -> None:
        self._last = time.monotonic()

    def stop(self) -> None:
        pass

    def _advance(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last)
        self._last = now
        if not self._playing:
            return
        self._position += elapsed
        track = demo_track(self._index)
        while self._position >= track.duration:
            self._position -= track.duration
            self._index += 1
            track = demo_track(self._index)

    def snapshot(self) -> Snapshot:
        self._advance()
        mode = MODE_CYCLE[self._mode_index % len(MODE_CYCLE)]
        return Snapshot(
            track=demo_track(self._index),
            playing=self._playing,
            position=self._position,
            sampled_at=time.monotonic(),
            repeat={"repeat": "list", "one": "track", "random": "list", "order": "none"}[mode],
            shuffle=mode == "random",
            app="demo",
            source="demo",
        )

    def lyrics(self) -> LyricPayload:
        """The demo tracks ship with lyrics, so there is nothing to look up."""
        track = demo_track(self._index)
        lines = demo_lyrics(track.key) or []
        return LyricPayload(
            key=track.key,
            rev=self._index % len(DEMO_TRACKS),
            lines=lines,
            none=not lines,
        )

    def command(self, action: str) -> None:
        self._advance()
        if action == "toggle":
            self._playing = not self._playing
        elif action == "play":
            self._playing = True
        elif action == "pause":
            self._playing = False
        elif action in ("next", "prev"):
            step = 1 if action == "next" else -1
            self._index = (self._index + step) % len(DEMO_TRACKS)
            self._position = 0.0
        elif action == "mode":
            self._mode_index += 1
