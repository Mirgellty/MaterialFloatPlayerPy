"""Self-check for the cover reader.

    .venv\\Scripts\\python.exe tools\\probe_thumbnail.py

SMTC hands the album art over as an IRandomAccessStreamReference; this builds one
from known bytes and asserts mfp_py.smtc.read_stream_bytes gives them back. It
also decodes the result with Pillow, which is what the widget does next.

Kept separate from probe_smtc because the fake player cannot publish a cover
(MediaPlayer never forwards DisplayUpdater.Thumbnail to an unpackaged desktop
session), so the live probe has no artwork to read.
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from winrt.windows.storage.streams import (  # noqa: E402
    DataWriter,
    InMemoryRandomAccessStream,
    RandomAccessStreamReference,
)

from mfp_py.color import build_palette, dominant_color, rgb_to_hex  # noqa: E402
from mfp_py.demo import render_cover  # noqa: E402
from mfp_py.smtc import read_stream_bytes  # noqa: E402


def reference_from(payload: bytes):
    """Return the reference *and* the objects behind it.

    The stream must outlive the reference: pywinrt releases the local wrapper as
    soon as it goes out of scope, and the stream then reports itself closed.
    (A real cover comes from another process, so the widget never creates one.)
    """
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(payload)
    writer.store_async().get()
    stream.seek(0)
    return RandomAccessStreamReference.create_from_stream(stream), stream, writer


def main() -> int:
    payload = render_cover(0)
    reference, stream, writer = reference_from(payload)

    import asyncio

    got = asyncio.run(read_stream_bytes(reference))
    assert stream is not None and writer is not None  # keep the COM objects alive
    print(f"source bytes : {len(payload)}  sha1={hashlib.sha1(payload).hexdigest()[:12]}")
    print(f"read bytes   : {len(got)}  sha1={hashlib.sha1(got).hexdigest()[:12]}")
    if got != payload:
        print("FAIL: cover bytes did not round-trip")
        return 1

    image = Image.open(io.BytesIO(got))
    rgb = dominant_color(image)
    palette = build_palette(rgb, keep_surface=False, light=False)
    print(f"decoded      : {image.format} {image.size[0]}x{image.size[1]}")
    print(f"dominant     : {rgb_to_hex(rgb)}")
    print(f"dark palette : surface {palette.color} -> {palette.color_alt}, accent {palette.accent}")
    light = build_palette(rgb, keep_surface=False, light=True)
    print(f"light palette: surface {light.color} -> {light.color_alt}, accent {light.accent}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
