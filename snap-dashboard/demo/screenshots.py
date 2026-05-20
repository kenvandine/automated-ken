"""Generate synthetic before/after app screenshots for the demo seed."""

from __future__ import annotations

import io
import math
from PIL import Image, ImageDraw

try:
    from PIL import ImageFont
    def _font(path: str, size: int):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    _MONO = _font("/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf", 13)
    _MONO_B = _font("/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf", 13)
    _SANS = _font("/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf", 13)
    _SANS_B = _font("/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf", 14)
    _TITLE = _font("/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf", 12)
    _SMALL = _font("/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf", 11)
except Exception:
    from PIL import ImageFont
    _MONO = _MONO_B = _SANS = _SANS_B = _TITLE = _SMALL = ImageFont.load_default()

W, H = 1280, 760

# Design-system colours matching the dashboard
BG       = (13,  17,  23)    # #0d1117  — terminal/app background
BG2      = (22,  27,  34)    # #161b22  — panel / titlebar
BORDER   = (48,  54,  61)    # #30363d
FG       = (230, 237, 243)   # #e6edf3  — primary text
FG_DIM   = (110, 118, 129)   # #6e7681  — muted
GREEN    = ( 63, 185, 80)    # #3fb950
CYAN     = ( 56, 189, 248)   # #38bdf8
YELLOW   = (210, 153,  34)   # #d29922
RED      = (248,  81,  73)   # #f85149
PURPLE   = (188, 140, 255)   # #bc8cff
PINK     = (255,  27, 141)   # #ff1b8d
ORANGE   = (249, 115,  22)   # #f97316


def _bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _window(draw: ImageDraw.ImageDraw, title: str, subtitle: str = "") -> None:
    """Draw a window chrome titlebar."""
    draw.rectangle([0, 0, W, 36], fill=BG2)
    draw.rectangle([0, 36, W, 37], fill=BORDER)
    # Traffic lights
    for x, col in [(20, (255, 95, 87)), (44, (255, 189, 46)), (68, (40, 200, 64))]:
        draw.ellipse([x - 7, 36 // 2 - 7, x + 7, 36 // 2 + 7], fill=col)
    # Title
    draw.text((W // 2, 18), title, fill=FG, font=_TITLE, anchor="mm")
    if subtitle:
        draw.text((W // 2, 18), subtitle, fill=FG_DIM, font=_SMALL, anchor="mm")


# ---------------------------------------------------------------------------
# ghostty — terminal emulator
# ---------------------------------------------------------------------------

def _ghostty(version: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _window(draw, f"ghostty  —  ~/projects  —  {version}")

    lines = [
        ("ken@ultrabox", GREEN, ""), (":", FG_DIM, ""), ("~/projects", CYAN, ""), ("$ ", FG, ""),
        ("git", CYAN, ""), (" fetch --all --tags", FG, "\n"),
        ("Fetching origin", FG_DIM, "\n"),
        ("Fetching upstream", FG_DIM, "\n"),
        (f"From https://github.com/ghostty-org/ghostty", FG_DIM, "\n"),
        (f" * [new tag]         {version} -> {version}", GREEN, "\n"),
        ("", FG, "\n"),
        ("ken@ultrabox", GREEN, ""), (":", FG_DIM, ""), ("~/projects", CYAN, ""), ("$ ", FG, ""),
        ("ghostty", CYAN, ""), (" --version", FG, "\n"),
        (f"ghostty {version}", FG, "\n"),
        ("", FG, "\n"),
        ("ken@ultrabox", GREEN, ""), (":", FG_DIM, ""), ("~/projects/ghostty", CYAN, ""), ("$ ", FG, ""),
        ("./build/ghostty", CYAN, ""), (" bench startup", FG, "\n"),
        ("Benchmark: cold startup latency", FG_DIM, "\n"),
        ("  iterations:  1000", FG_DIM, "\n"),
        ("  mean:        47.2ms", GREEN, "\n"),
        ("  p99:         63.1ms", GREEN, "\n"),
        ("  max:         81.4ms", FG_DIM, "\n"),
    ]

    x, y = 14, 52
    lh = 20
    for item in lines:
        text, color, end = item
        if end == "\n":
            if text:
                draw.text((x, y), text, fill=color, font=_MONO)
            x = 14
            y += lh
        else:
            draw.text((x, y), text, fill=color, font=_MONO)
            try:
                x += draw.textlength(text, font=_MONO)
            except Exception:
                x += len(text) * 8

    return img


def ghostty_before() -> bytes:
    return _bytes(_ghostty("v1.3.1"))


def ghostty_after() -> bytes:
    return _bytes(_ghostty("v1.3.2"))


# ---------------------------------------------------------------------------
# lemonade-desktop — AI desktop app
# ---------------------------------------------------------------------------

def _lemonade_desktop(version: str, is_after: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), (10, 10, 20))
    draw = ImageDraw.Draw(img)

    # Subtle gradient blobs in background
    for bx, by, br, col in [
        (200, 300, 180, (255, 27, 141, 15)),
        (900, 200, 220, (168, 85, 247, 12)),
        (1100, 600, 160, (6, 182, 212, 10)),
    ]:
        for r in range(br, 0, -4):
            alpha = int(col[3] * (r / br))
            draw.ellipse([bx - r, by - r, bx + r, by + r],
                         fill=(col[0], col[1], col[2]))

    _window(draw, f"Lemonade Desktop  {version}")

    # Sidebar
    draw.rectangle([0, 37, 220, H], fill=(14, 14, 28))
    draw.rectangle([220, 37, 221, H], fill=BORDER)
    draw.text((16, 56), "Lemonade Desktop", fill=FG, font=_SANS_B)
    draw.text((16, 76), version, fill=PURPLE, font=_SMALL)

    # Sidebar items
    for i, (label, active) in enumerate([
        ("New Chat", True),
        ("Chat History", False),
        ("Models", False),
        ("Settings", False),
    ]):
        y = 110 + i * 38
        if active:
            draw.rounded_rectangle([8, y - 6, 212, y + 24], radius=6,
                                    fill=(40, 20, 60))
        draw.text((18, y), label, fill=FG if active else FG_DIM, font=_SANS)

    # Chat area header
    draw.rectangle([221, 37, W, 80], fill=(12, 12, 26))
    draw.rectangle([221, 80, W, 81], fill=BORDER)
    draw.text((240, 58), "Chat with llama3.2-vision", fill=FG, font=_SANS_B)

    # Chat messages
    msgs = [
        ("You", "Describe what you see in this screenshot.", False),
        ("Lemonade", "I can see a terminal application showing a build process.\nThe output indicates a successful compilation with all tests passing.", True),
        ("You", "Can you compare the two versions?", False),
        ("Lemonade",
         ("The two screenshots show the same application at different versions.\n"
          "The UI layout, colour scheme, and overall structure are consistent.\n"
          + ("No visual regressions detected." if is_after else "Running analysis…")),
         True),
    ]

    cy = 96
    for sender, text, is_ai in msgs:
        if is_ai:
            draw.rounded_rectangle([230, cy, W - 20, cy + len(text.split("\n")) * 20 + 32],
                                    radius=10, fill=(20, 20, 40), outline=BORDER)
            draw.text((248, cy + 10), "Lemonade", fill=PURPLE, font=_SANS_B)
        else:
            draw.rounded_rectangle([230, cy, W - 20, cy + 44],
                                    radius=10, fill=(30, 30, 55), outline=BORDER)
            draw.text((248, cy + 10), "You", fill=CYAN, font=_SANS_B)

        for li, line in enumerate(text.split("\n")):
            draw.text((248, cy + 28 + li * 18), line, fill=FG, font=_SANS)
        cy += len(text.split("\n")) * 20 + 52

    # Input box
    draw.rounded_rectangle([230, H - 64, W - 20, H - 16],
                            radius=10, fill=(18, 18, 36), outline=BORDER)
    draw.text((248, H - 44), "Ask Lemonade anything…", fill=FG_DIM, font=_SANS)

    return img


def lemonade_desktop_before() -> bytes:
    return _bytes(_lemonade_desktop("v10.3.0", is_after=False))


def lemonade_desktop_after() -> bytes:
    return _bytes(_lemonade_desktop("v10.4.0", is_after=True))


# ---------------------------------------------------------------------------
# thrive — game app (with deliberate regression in the "after")
# ---------------------------------------------------------------------------

def _thrive(version: str, crashed: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), (8, 12, 8))
    draw = ImageDraw.Draw(img)
    _window(draw, f"Thrive  —  {version}")

    # Game background — deep ocean feel
    draw.rectangle([0, 37, W, H], fill=(8, 18, 8))

    # Hexagonal grid (simplified)
    for row in range(8):
        for col in range(10):
            cx = 80 + col * 118 + (60 if row % 2 else 0)
            cy = 80 + row * 68
            pts = [(cx + int(40 * math.cos(math.radians(60 * i - 30))),
                    cy + int(40 * math.sin(math.radians(60 * i - 30)))) for i in range(6)]
            draw.polygon(pts, fill=(12, 30, 12), outline=(30, 80, 30))

    # UI panel bottom
    draw.rectangle([0, H - 90, W, H], fill=(10, 20, 10))
    draw.rectangle([0, H - 91, W, H - 90], fill=(30, 80, 30))
    draw.text((20, H - 72), "ATP:  1247", fill=GREEN, font=_SANS_B)
    draw.text((20, H - 52), "Compounds absorbed: 34", fill=FG_DIM, font=_SANS)
    draw.text((200, H - 72), "Generation  12", fill=CYAN, font=_SANS_B)
    draw.text((420, H - 72), f"Thrive  {version}", fill=FG_DIM, font=_SMALL)

    # Some organism blobs
    for ox, oy, oc in [(400, 300, GREEN), (650, 220, CYAN), (900, 350, YELLOW)]:
        draw.ellipse([ox - 18, oy - 18, ox + 18, oy + 18], fill=oc)
        draw.ellipse([ox - 8, oy - 8, ox + 8, oy + 8], fill=FG)

    if crashed:
        # Crash dialog overlay — clear regression signal
        draw.rectangle([0, 0, W, H], fill=(0, 0, 0, 160))
        draw.rounded_rectangle([360, 260, 920, 500], radius=12,
                                fill=(28, 16, 16), outline=RED, width=2)
        draw.text((640, 295), "⚠  Application Crashed", fill=RED, font=_SANS_B, anchor="mm")
        draw.line([370, 316, 910, 316], fill=BORDER, width=1)
        draw.text((640, 342), "Segmentation fault (core dumped)", fill=FG, font=_MONO, anchor="mm")
        draw.text((640, 368),
                  "Signal: SIGSEGV  Address: 0x000000000000",
                  fill=FG_DIM, font=_SMALL, anchor="mm")
        draw.text((640, 394),
                  "This version of Thrive has crashed.\nThe snap package may need to be rebuilt.",
                  fill=FG_DIM, font=_SANS, anchor="mm")
        draw.rounded_rectangle([480, 448, 760, 482], radius=6, fill=RED)
        draw.text((620, 465), "Close", fill=FG, font=_SANS_B, anchor="mm")

    return img


def thrive_before() -> bytes:
    return _bytes(_thrive("v0.9.2", crashed=False))


def thrive_after() -> bytes:
    return _bytes(_thrive("v1.0.1", crashed=True))
