"""Extract and validate screenshots from a YARF ``log.html`` results file.

YARF embeds its log messages as zlib-compressed, base64-encoded blobs
inside the generated HTML; each blob decodes to HTML containing
``data:image/png;base64,...`` URIs. This mirrors the extraction logic used
by the GitHub Actions workflow (see
``snap_dashboard.testing.workflow_template``) so both dispatch targets
(GitHub Actions and this runner) produce byte-identical screenshot
handling and the same brightness-based validity check.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from dataclasses import dataclass

from PIL import Image, ImageStat

_BLOB_RE = re.compile(r'"(eN[A-Za-z0-9+/=]+)"')
_PNG_DATA_URI_RE = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")

# Screenshots this dark are almost certainly a blank/black window (crash,
# unrendered surface, or the bare desktop background) rather than a real
# app screenshot — flag them as invalid so auto-promotion can be blocked.
_BLACK_BRIGHTNESS_THRESHOLD = 8.0


@dataclass
class ExtractedScreenshot:
    image_name: str
    png_bytes: bytes
    width: int
    height: int
    brightness_mean: float
    is_valid: bool


def extract_screenshots(log_html: str) -> list[ExtractedScreenshot]:
    """Parse a YARF ``log.html`` document and return its embedded screenshots.

    Never raises — a malformed or screenshot-less log simply yields an
    empty list, since the caller (the main runner loop) must not crash a
    whole test job over a parsing hiccup.
    """
    results: list[ExtractedScreenshot] = []
    raw_pngs: list[bytes] = []

    for blob in _BLOB_RE.findall(log_html):
        try:
            decompressed = zlib.decompress(base64.b64decode(blob)).decode(
                "utf-8", errors="replace"
            )
        except (zlib.error, ValueError):
            continue
        for png_b64 in _PNG_DATA_URI_RE.findall(decompressed):
            try:
                raw_pngs.append(base64.b64decode(png_b64))
            except ValueError:
                continue

    for i, raw in enumerate(raw_pngs):
        try:
            img = Image.open(io.BytesIO(raw))
            rgb = img.convert("RGB")
            brightness = sum(ImageStat.Stat(rgb).mean) / 3
            # Crop out the black desktop background, keeping only the app window.
            bbox = rgb.getbbox()
            cropped = img.crop(bbox) if bbox else img
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            png_bytes = buf.getvalue()
            width, height = cropped.size
        except Exception:
            continue

        results.append(
            ExtractedScreenshot(
                image_name=f"screenshot-{i + 1:03d}.png",
                png_bytes=png_bytes,
                width=width,
                height=height,
                brightness_mean=brightness,
                is_valid=brightness >= _BLACK_BRIGHTNESS_THRESHOLD,
            )
        )

    return results
