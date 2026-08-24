#!/usr/bin/env python3
"""Rasterize the frozen source-rendered terminal frames into honest visual evidence."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "terminal-render.txt"
OUT = HERE / "screenshots"
FONT = Path("/System/Library/Fonts/SFNSMono.ttf")


def section(name: str) -> list[str]:
    text = SOURCE.read_text()
    marker = f"=== {name} ===\n"
    body = text.split(marker, 1)[1].split("\n=== ", 1)[0].strip("\n")
    return body.splitlines()


def render(name: str, columns: int, rows: int, lines: list[str], color: bool) -> None:
    font = ImageFont.truetype(str(FONT), 18)
    cell_w, cell_h, margin = 11, 22, 24
    image = Image.new("RGB", (columns * cell_w + margin * 2, rows * cell_h + margin * 2), "#0b0d10")
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        ink = "#f2ede3"
        if color and ("B O S S" in line or "◆" in line or "/ops" in line):
            ink = "#b7f34a"
        if "tools missing" in line:
            ink = "#ffcf70"
        draw.text((margin, margin + row * cell_h), line, font=font, fill=ink)
    receipt = f"source-rendered | {columns}x{rows} | not a live capture"
    draw.text((margin, image.height - margin - cell_h), receipt, font=font, fill="#6f7782")
    image.save(OUT / name, optimize=True)


def main() -> None:
    if not FONT.exists():
        raise SystemExit(f"required evidence font unavailable: {FONT}")
    OUT.mkdir(parents=True, exist_ok=True)
    render("default-100x30.png", 100, 30, section("WIDE / 100 COLUMNS"), True)
    render("minimum-50x12-no-color.png", 50, 12, section("MINIMUM / 50 COLUMNS"), False)
    render("degraded-60x12-plain.png", 60, 12, section("DEGRADED / 60 COLUMNS"), False)


if __name__ == "__main__":
    main()
