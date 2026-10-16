"""Album-art colour extraction and Material palette building.

This is a port of the plugin's OkLab/OkLCh code (MaterialFloatPlayer/src/main.js)
so the standalone player produces the same colours for the same cover: sample
the artwork, pick the hue family that is both common and saturated, then rebuild
surface/accent colours at a fixed lightness for the chosen day/night target.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image

DEFAULT_ALBUM_COLOR = "#6750a4"

Rgb = tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    """One resolved colour set, ready to paint."""

    color: str          # surface gradient start (hex)
    color_alt: str      # surface gradient end (hex)
    accent: str         # current lyric line, play button
    accent_on: str      # icon colour on top of the accent
    on_color: str       # body text
    ink: Rgb            # hairlines, hover fills, progress track


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def hex_to_rgb(value: str) -> Rgb | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def rgb_to_hex(rgb: Rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(clamp(round(v), 0, 255)) for v in rgb)


def mix_hex(first: str, second: str, t: float) -> str:
    """Straight sRGB mix - the same thing a CSS colour transition does."""
    a = hex_to_rgb(first) or (0, 0, 0)
    b = hex_to_rgb(second) or (0, 0, 0)
    t = clamp(t, 0.0, 1.0)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def mix_palette(first: "Palette", second: "Palette", t: float) -> "Palette":
    """Half-way palette between two of them, used for the crossfade on track change.

    Mixing the palette itself (instead of painting one over the other) means the
    text and accent colours travel with the surface, which is what the plugin's
    CSS transitions did - and no third colour appears in between.
    """
    t = clamp(t, 0.0, 1.0)
    ink = tuple(
        int(round(first.ink[i] + (second.ink[i] - first.ink[i]) * t)) for i in range(3)
    )
    return Palette(
        color=mix_hex(first.color, second.color, t),
        color_alt=mix_hex(first.color_alt, second.color_alt, t),
        accent=mix_hex(first.accent, second.accent, t),
        accent_on=mix_hex(first.accent_on, second.accent_on, t),
        on_color=mix_hex(first.on_color, second.on_color, t),
        ink=(ink[0], ink[1], ink[2]),
    )


def _srgb_to_linear(value: float) -> float:
    value = value / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb_byte(value: float) -> int:
    srgb = value * 12.92 if value <= 0.0031308 else 1.055 * (value ** (1 / 2.4)) - 0.055
    return int(clamp(round(srgb * 255), 0, 255))


def rgb_to_oklab(rgb: Rgb) -> tuple[float, float, float]:
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def oklab_to_rgb(lab: tuple[float, float, float]) -> Rgb:
    lightness, a, b = lab
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    return (
        _linear_to_srgb_byte(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
        _linear_to_srgb_byte(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
        _linear_to_srgb_byte(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
    )


def oklab_to_lch(lab: tuple[float, float, float]) -> tuple[float, float, float]:
    lightness, a, b = lab
    return (lightness, math.hypot(a, b), math.atan2(b, a))


def lch_to_oklab(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    return (lightness, chroma * math.cos(hue), chroma * math.sin(hue))


def luminance(rgb: Rgb) -> float:
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def build_palette(base_rgb: Rgb, keep_surface: bool, light: bool) -> Palette:
    """Dark: soft dark surface + vivid light accent. Light: pale surface + mid accent."""
    base_lightness, chroma, hue = oklab_to_lch(rgb_to_oklab(base_rgb))

    if light:
        light_l = clamp(base_lightness, 0.88, 0.97) if keep_surface else 0.955
        light_c = (
            clamp(chroma * 0.24, 0.006, 0.04) if keep_surface else clamp(chroma * 0.1, 0.005, 0.026)
        )
        surface = oklab_to_rgb(lch_to_oklab(light_l, light_c, hue))
        surface_alt = oklab_to_rgb(
            lch_to_oklab(clamp(light_l - 0.055, 0.8, 0.93), clamp(light_c * 0.85, 0.005, 0.04), hue)
        )
        accent = oklab_to_rgb(
            lch_to_oklab(0.52, clamp(max(chroma * 1.05, 0.09), 0.07, 0.17), hue)
        )
        on_color = rgb_to_hex(
            oklab_to_rgb(lch_to_oklab(0.3, clamp(chroma * 0.26, 0.008, 0.05), hue))
        )
    else:
        surface_l = clamp(base_lightness, 0.24, 0.68) if keep_surface else 0.27
        surface_c = clamp(chroma * 0.9, 0.02, 0.13) if keep_surface else clamp(chroma * 0.5, 0.02, 0.07)
        surface = oklab_to_rgb(lch_to_oklab(surface_l, surface_c, hue))
        surface_alt = oklab_to_rgb(
            lch_to_oklab(max(0.12, surface_l - 0.09), clamp(surface_c * 0.8, 0.015, 0.13), hue)
        )
        accent = oklab_to_rgb(lch_to_oklab(0.86, clamp(max(chroma * 1.15, 0.08), 0.05, 0.17), hue))
        on_color = "#1b1b1f" if luminance(surface) > 0.42 else "#ffffff"

    return Palette(
        color=rgb_to_hex(surface),
        color_alt=rgb_to_hex(surface_alt),
        accent=rgb_to_hex(accent),
        accent_on="#221a35" if luminance(accent) > 0.5 else "#ffffff",
        on_color=on_color,
        ink=(0, 0, 0) if light else (255, 255, 255),
    )


def dominant_color(image: Image.Image, sample: int = 48) -> Rgb:
    """Pick the dominant saturated hue of a cover image (same scoring as the plugin)."""
    thumb = image.convert("RGB").resize((sample, sample), Image.LANCZOS)
    buckets: dict[int, list[float]] = {}
    fallback = [0.0, 0.0, 0.0, 0]
    for red, green, blue in thumb.getdata():
        lab = rgb_to_oklab((red, green, blue))
        fallback[0] += lab[0]
        fallback[1] += lab[1]
        fallback[2] += lab[2]
        fallback[3] += 1
        lightness, chroma, hue = oklab_to_lch(lab)
        if lightness < 0.18 or lightness > 0.92:
            continue  # near black / near white
        if chroma < 0.03:
            continue  # grey
        key = int(((math.degrees(hue) + 360) % 360) // 15)
        bucket = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += 1
        bucket[1] += lab[0]
        bucket[2] += lab[1]
        bucket[3] += lab[2]
        bucket[4] += chroma

    best: list[float] | None = None
    best_score = 0.0
    for bucket in buckets.values():
        count = bucket[0]
        if not count:
            continue
        # common + saturated = the hue Material You would have picked
        score = (count**0.55) * ((bucket[4] / count) ** 1.1)
        if score > best_score:
            best_score = score
            best = bucket
    if best is not None:
        return oklab_to_rgb((best[1] / best[0], best[2] / best[0], best[3] / best[0]))
    if fallback[3]:
        count = fallback[3]
        return oklab_to_rgb((fallback[0] / count, fallback[1] / count, fallback[2] / count))
    return hex_to_rgb(DEFAULT_ALBUM_COLOR) or (103, 80, 164)
