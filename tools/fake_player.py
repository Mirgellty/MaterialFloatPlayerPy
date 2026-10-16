"""Publish a synthetic media session so the widget can be tested without a player.

    .venv\\Scripts\\python.exe tools\\fake_player.py
    .venv\\Scripts\\python.exe tools\\fake_player.py --verify

Creates a MediaPlayer that loops a generated tone and registers with the Windows
media controls, complete with title/artist/album, a generated cover image and a
timeline. The widget reads exactly those fields, so the whole read path
(metadata, cover bytes, position, duration, playback status, repeat/shuffle) can
be exercised while a real player is idle.

Two Windows quirks are handled here, both of which the widget's reader depends on:

* DisplayUpdater.MusicProperties returns a *new* COM object on every access, so
  all fields have to be written through one held reference before update().
* An unpackaged desktop MediaPlayer never publishes timeline properties, so the
  position is pushed through UpdateTimelineProperties on a timer.

Ctrl+C stops it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import math
import struct
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw
from winrt.windows.foundation import Uri
from winrt.windows.media import (
    MediaPlaybackAutoRepeatMode,
    MediaPlaybackType,
    SystemMediaTransportControlsTimelineProperties,
)
from winrt.windows.media.core import MediaSource
from winrt.windows.media.playback import MediaPlayer
from winrt.windows.storage.streams import (
    DataWriter,
    InMemoryRandomAccessStream,
    RandomAccessStreamReference,
)

SAMPLE_RATE = 44100
TRACKS = (
    ("Dreamland", "Glass Animals", "Dreamland"),
    ("Midnight City", "M83", "Hurry Up, We're Dreaming"),
    ("Weightless", "Marconi Union", "Weightless"),
)


def clamp(value: float, low: float = 0.0, high: float = 255.0) -> float:
    return min(high, max(low, value))


def timedelta(seconds: float) -> datetime.timedelta:
    return datetime.timedelta(seconds=seconds)


def write_tone(path: Path, seconds: float = 12.0) -> None:
    """A soft restrained chord - audible confirmation that audio is really running."""
    notes = (220.0, 277.18, 329.63, 440.0)
    frames = bytearray()
    total = int(SAMPLE_RATE * seconds)
    for index in range(total):
        t = index / SAMPLE_RATE
        fade = min(1.0, t / 0.8) * min(1.0, (seconds - t) / 0.8)
        value = sum(math.sin(2 * math.pi * note * t) for note in notes) / len(notes)
        frames += struct.pack("<h", int(value * fade * 5200))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def write_cover(path: Path, seed: int) -> None:
    """A cover with a clear hue family, so the palette code has something to chew on."""
    size = 384
    hue = (seed * 97) % 360
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            mix = x / size
            wave_value = 0.5 + 0.5 * math.sin((y / size) * math.pi * 1.4 + mix * 3.0)
            angle = math.radians(hue + 40 * mix)
            pixels[x, y] = (
                int(clamp(120 + 110 * wave_value + 60 * math.cos(angle))),
                int(clamp(70 + 90 * (1 - mix) + 40 * math.sin(angle))),
                int(clamp(150 + 80 * mix + 50 * math.cos(angle + 2))),
            )
    draw = ImageDraw.Draw(image)
    for index in range(7):
        radius = 48 + index * 20
        draw.ellipse(
            [192 - radius, 192 - radius, 192 + radius, 192 + radius],
            outline=(255, 226, 150 - index * 12),
            width=5,
        )
    image.save(path)


def stream_reference(path: Path) -> RandomAccessStreamReference | None:
    payload = path.read_bytes()
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(payload)
    writer.store_async().get()
    print("  stream size after store:", stream.size)
    stream.seek(0)
    from_stream = RandomAccessStreamReference.create_from_stream(stream)
    from_uri = RandomAccessStreamReference.create_from_uri(Uri(path.as_uri()))
    print(
        f"  cover: {len(payload)} bytes -> from_stream={from_stream is not None} "
        f"from_uri={from_uri is not None}"
    )
    return from_uri


def publish(player: MediaPlayer, title: str, artist: str, album: str, cover: Path) -> None:
    """Write metadata through one held properties reference, then publish."""
    updater = player.system_media_transport_controls.display_updater
    updater.type = MediaPlaybackType.MUSIC
    music = updater.music_properties
    music.title = title
    music.artist = artist
    music.album_title = album
    reference = stream_reference(cover)
    if reference is not None:
        updater.thumbnail = reference
        print("  updater.thumbnail readback:", updater.thumbnail)
    updater.update()


def push_timeline(smtc, position: float, duration: float) -> None:
    timeline = SystemMediaTransportControlsTimelineProperties()
    timeline.start_time = timedelta(0.0)
    timeline.end_time = timedelta(duration)
    timeline.position = timedelta(position)
    timeline.min_seek_time = timedelta(0.0)
    timeline.max_seek_time = timedelta(duration)
    smtc.update_timeline_properties(timeline)


async def pump(
    player: MediaPlayer,
    republish=None,
    duration: float = 12.0,
    seconds: float = 0.0,
) -> None:
    """Keep SMTC's timeline in step with the player, as a real player would.

    MediaPlayer resets the display updater when playback starts, so metadata has
    to be published again after every playback state change; republish does that.
    """
    smtc = player.system_media_transport_controls
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds if seconds else None
    last_state = None
    while True:
        await asyncio.sleep(0.5)
        state = player.playback_session.playback_state
        if republish is not None and state != last_state:
            republish()
        last_state = state
        push_timeline(smtc, player.position.total_seconds(), duration)
        if deadline is not None and loop.time() >= deadline:
            return


async def verify(player: MediaPlayer, republish=None) -> None:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    await pump(player, republish=republish, seconds=3.0)
    manager = await SessionManager.request_async()
    session = manager.get_current_session()
    print("session           :", session.source_app_user_model_id if session else None)
    if session is None:
        return
    props = await session.try_get_media_properties_async()
    print("title             :", repr(props.title))
    print("artist            :", repr(props.artist))
    print("album_title       :", repr(props.album_title))
    print("thumbnail         :", props.thumbnail)
    if props.thumbnail is not None:
        await thumbnail_bytes(props.thumbnail)
    timeline = session.get_timeline_properties()
    print("timeline.position :", timeline.position, "end:", timeline.end_time)
    info = session.get_playback_info()
    print("playback_status   :", info.playback_status, "repeat:", info.auto_repeat_mode)


async def thumbnail_bytes(reference) -> None:
    """Find out which pywinrt idiom actually turns an IBuffer into bytes."""
    from winrt.windows.storage.streams import Buffer, DataReader

    stream = await reference.open_read_async()
    size = stream.size
    print("  stream size     :", size)

    try:
        reader = DataReader(stream)
        await reader.load_async(size)
        buffer = reader.read_buffer(size)
        view = memoryview(buffer)
        print("  DataReader+memoryview:", len(view), bytes(view[:8]).hex())
        return
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print("  DataReader+memoryview failed:", type(exc).__name__, exc)

    try:
        data = bytearray(size)
        reader = DataReader(stream)
        await reader.load_async(size)
        print("  DataReader.read_bytes:", reader.read_bytes(data), bytes(data[:8]).hex())
        return
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print("  DataReader.read_bytes failed:", type(exc).__name__, exc)

    try:
        buffer = Buffer(size)
        input_stream = stream.get_input_stream_at(0)
        read = input_stream.read_async(buffer, size, 0).get()
        view = memoryview(buffer)
        print("  InputStream.read_async:", read, bytes(view[:8]).hex())
        return
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print("  InputStream.read_async failed:", type(exc).__name__, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", type=int, default=0, help="index into the demo tracks")
    parser.add_argument("--mute", action="store_true", help="silence the test tone")
    parser.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")
    parser.add_argument("--verify", action="store_true", help="read the session back")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mfp-fake-"))
    audio_path = work / "tone.wav"
    cover_path = work / "cover.png"
    write_tone(audio_path)
    write_cover(cover_path, args.track)

    player = MediaPlayer()
    player.source = MediaSource.create_from_uri(Uri(audio_path.as_uri()))
    player.is_looping_enabled = True
    player.volume = 0.0 if args.mute else 0.14

    title, artist, album = TRACKS[args.track % len(TRACKS)]
    smtc = player.system_media_transport_controls
    smtc.is_enabled = True
    publish(player, title, artist, album, cover_path)
    smtc.auto_repeat_mode = MediaPlaybackAutoRepeatMode.LIST
    player.command_manager.is_enabled = True
    player.play()

    print(f"fake session published: {artist} - {title}  (Ctrl+C to stop)")
    try:
        republish = lambda: publish(player, title, artist, album, cover_path)  # noqa: E731
        if args.verify:
            asyncio.run(verify(player, republish=republish))
        else:
            asyncio.run(pump(player, republish=republish, seconds=args.seconds))
    except KeyboardInterrupt:
        pass
    finally:
        player.pause()


if __name__ == "__main__":
    main()
