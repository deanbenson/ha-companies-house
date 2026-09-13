"""Render the brand PNGs from the SVG sources.

Run from the repository root:

    .venv/bin/python brand/source/render.py

Requires cairosvg and pillow (dev-only, not runtime requirements).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "brand" / "source"
OUT = ROOT / "custom_components" / "companies_house" / "brand"

ICON_SIZES = {"icon.png": 256, "icon@2x.png": 512}
LOGO_HEIGHTS = {"logo.png": 256, "logo@2x.png": 512}
DARK_TEXT = "#FFFFFF"
LIGHT_TEXT = "#1B2A33"


def _png(svg: str, width: int, height: int) -> bytes:
    return cairosvg.svg2png(
        bytestring=svg.encode(), output_width=width, output_height=height
    )


def _optimise(data: bytes) -> bytes:
    """Re-encode as an optimised, interlaced PNG."""
    image = Image.open(BytesIO(data)).convert("RGBA")
    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True, interlace=1)
    return buf.getvalue()


def main() -> None:
    """Render every brand asset."""
    OUT.mkdir(parents=True, exist_ok=True)
    icon_svg = (SOURCE / "icon.svg").read_text()
    logo_svg = (SOURCE / "logo.svg").read_text()

    for name, size in ICON_SIZES.items():
        (OUT / name).write_bytes(_optimise(_png(icon_svg, size, size)))
    # The icon carries its own tile so it reads on any theme.
    (OUT / "dark_icon.png").write_bytes((OUT / "icon.png").read_bytes())
    (OUT / "dark_icon@2x.png").write_bytes((OUT / "icon@2x.png").read_bytes())

    for name, height in LOGO_HEIGHTS.items():
        width = int(height * 1060 / 256)
        light = logo_svg.replace("TEXT_COLOUR", LIGHT_TEXT)
        dark = logo_svg.replace("TEXT_COLOUR", DARK_TEXT)
        (OUT / name).write_bytes(_optimise(_png(light, width, height)))
        (OUT / f"dark_{name}").write_bytes(_optimise(_png(dark, width, height)))

    for path in sorted(OUT.glob("*.png")):
        with Image.open(path) as img:
            print(f"{path.name:20} {img.size[0]}x{img.size[1]} {path.stat().st_size} B")


if __name__ == "__main__":
    main()
