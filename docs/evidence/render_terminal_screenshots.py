#!/usr/bin/env python3
"""Rasterize the frozen source-rendered terminal frames into honest visual evidence."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "terminal-render.txt"
OUT = HERE / "screenshots"
FONT = Path("/System/Library/Fonts/SFNSMono.ttf")
INK = "#f2ede3"
MUTED = "#8c949f"
SIGNAL = "#b7f34a"
WARNING = "#ffcf70"
BACKGROUND = "#0b0d10"

BASELINE_WIDE = [
    "      ┏━━╮     B O S S",
    "      ┣━━┫     Y O U R   A I   C O O",
    "      ┗━━╯     O P E R A T I O N S   D E S K",
    "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    "  ◆  3 projects · team ready · 2 running · 1 queued · 1 need you",
    "     /ops  ·  /inbox  ·  /wake 20m",
]


def section(name: str) -> list[str]:
    text = SOURCE.read_text()
    marker = f"=== {name} ===\n"
    body = text.split(marker, 1)[1].split("\n=== ", 1)[0].strip("\n")
    return body.splitlines()


def char_ink(line: str, index: int, color: bool) -> str:
    if not color:
        return INK
    if "tools missing" in line:
        return WARNING
    first = len(line) - len(line.lstrip())
    if index == first and line[index:index + 1] == "◆":
        return SIGNAL
    mark_width = 7 if line[first:first + 1] == "█" else 4
    if any(glyph in line[first:first + mark_width] for glyph in ("█", "┏", "┣", "┗")) and index < first + mark_width:
        return SIGNAL
    for command in ("/ops", "/inbox", "/wake 20m"):
        start = line.find(command)
        if start >= 0 and start <= index < start + len(command):
            return SIGNAL
    if any(label in line for label in ("Y O U R   A I   C O O", "O P E R A T I O N S   D E S K", "projects ·")):
        return MUTED
    return INK


def draw_terminal_line(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, line: str,
                       x: int, y: int, cell_w: int, color: bool) -> None:
    for index, char in enumerate(line):
        draw.text((x + index * cell_w, y), char, font=font, fill=char_ink(line, index, color))


def render(name: str, columns: int, rows: int, lines: list[str], color: bool) -> None:
    font = ImageFont.truetype(str(FONT), 18)
    cell_w, cell_h, margin = 11, 22, 24
    image = Image.new("RGB", (columns * cell_w + margin * 2, rows * cell_h + margin * 2), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw_terminal_line(draw, font, line, margin, margin + row * cell_h, cell_w, color)
    receipt = f"source-rendered | {columns}x{rows} | not a live capture"
    draw.text((margin, image.height - margin - cell_h), receipt, font=font, fill="#6f7782")
    image.save(OUT / name, optimize=True)


def render_comparison() -> None:
    font = ImageFont.truetype(str(FONT), 16)
    label_font = ImageFont.truetype(str(FONT), 15)
    cell_w, cell_h, margin, panel_w = 10, 20, 32, 790
    image = Image.new("RGB", (panel_w * 2 + margin * 3, 250), BACKGROUND)
    draw = ImageDraw.Draw(image)
    panels = [
        ("BEFORE · outlined box mark", BASELINE_WIDE),
        ("AFTER · solid-spine B lockup", section("WIDE / 100 COLUMNS")),
    ]
    for panel, (label, lines) in enumerate(panels):
        x = margin + panel * (panel_w + margin)
        draw.text((x, 22), label, font=label_font, fill=MUTED)
        for row, line in enumerate(lines):
            draw_terminal_line(draw, font, line[:76], x, 54 + row * cell_h, cell_w, True)
    divider_x = margin + panel_w + margin // 2
    draw.line((divider_x, 18, divider_x, 202), fill="#252a31", width=1)
    draw.text((margin, 220), "source-rendered comparison | exact final frame at right | not a live capture", font=label_font, fill="#6f7782")
    image.save(OUT / "terminal-mark-before-after.png", optimize=True)


def main() -> None:
    if not FONT.exists():
        raise SystemExit(f"required evidence font unavailable: {FONT}")
    OUT.mkdir(parents=True, exist_ok=True)
    render("default-100x30.png", 100, 30, section("WIDE / 100 COLUMNS"), True)
    render("minimum-50x12-no-color.png", 50, 12, section("MINIMUM / 50 COLUMNS"), False)
    render("degraded-60x12-plain.png", 60, 12, section("DEGRADED / 60 COLUMNS"), False)
    render_comparison()


if __name__ == "__main__":
    main()
