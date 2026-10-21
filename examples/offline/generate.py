#!/usr/bin/env python3
"""Create deterministic, visibly synthetic offline images and an input manifest.

Only local files are read or written. The geometric drawings are test fixtures,
not AI images or photographs of real products. No font files are distributed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw, ImageFont

SIZE = (600, 420)
BACKGROUND = (247, 246, 242)
INK = (39, 43, 42)
ACCENTS = [(161, 168, 151), (177, 151, 135), (133, 154, 167)]


def make_images(directory: Path, regular_font: Path, *, font_index: int = 0) -> list[Path]:
    """Draw three labelled abstract objects with exact 600 x 420 RGB pixels."""
    directory.mkdir(parents=True, exist_ok=True)
    small = ImageFont.truetype(str(regular_font), 16, index=font_index)
    label = ImageFont.truetype(str(regular_font), 20, index=font_index)
    paths = []
    for index, accent in enumerate(ACCENTS, 1):
        image = Image.new("RGB", SIZE, BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.line((34, 55, 566, 55), fill=(213, 213, 206), width=1)
        draw.text((34, 24), f"SYNTHETIC FIXTURE / {index:02d}", font=small, fill=INK)
        draw.line((34, 365, 566, 365), fill=(213, 213, 206), width=1)
        draw.text((34, 380), "ABSTRACT GEOMETRY / NOT A REAL PRODUCT", font=small, fill=INK)
        # Deliberately flat geometric marks, never product-like photographs.
        if index == 1:
            draw.ellipse((197, 104, 403, 310), fill=accent)
            draw.rectangle((291, 104, 309, 310), fill=BACKGROUND)
            draw.line((172, 207, 428, 207), fill=INK, width=3)
        elif index == 2:
            draw.polygon(((300, 91), (426, 309), (174, 309)), fill=accent)
            draw.ellipse((272, 194, 328, 250), fill=BACKGROUND)
            draw.line((161, 325, 439, 325), fill=INK, width=3)
        else:
            draw.rounded_rectangle((192, 110, 408, 306), radius=21, fill=accent)
            draw.rectangle((226, 144, 374, 272), fill=BACKGROUND)
            draw.line((163, 208, 438, 208), fill=INK, width=3)
        draw.text((474, 321), f"{index:02d}", font=label, fill=INK)
        path = directory / f"geometry-{index:02d}.png"
        image.save(path, format="PNG", compress_level=9)
        paths.append(path)
    return paths


def safe_path(value: Path) -> Path:
    path = value.absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Unsafe path")
    return path


def copy_font(source: Path, destination: Path) -> str:
    source, destination = safe_path(source), safe_path(destination)
    if not source.is_file() or source.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Font must be a local regular file no larger than 32 MiB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination.name


def generate(destination: Path, regular_font: Path, bold_font: Path, *,
             regular_index: int = 0, bold_index: int = 0) -> Path:
    """Write a self-contained fixture directory, including copied local fonts."""
    destination = safe_path(destination)
    regular_font, bold_font = safe_path(regular_font), safe_path(bold_font)
    for path in (regular_font, bold_font):
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Font must be a local regular file no larger than 32 MiB")
    for index in (regular_index, bold_index):
        if type(index) is not int or not 0 <= index <= 32:
            raise ValueError("Font index must be an integer between 0 and 32")
    # Validate both font faces before writing any output.
    ImageFont.truetype(str(regular_font), 16, index=regular_index)
    ImageFont.truetype(str(bold_font), 16, index=bold_index)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    regular_copy = destination / "input" / "fonts" / ("regular" + regular_font.suffix)
    bold_copy = destination / "input" / "fonts" / ("bold" + bold_font.suffix)
    copy_font(regular_font, regular_copy)
    copy_font(bold_font, bold_copy)
    paths = make_images(destination / "input" / "images", regular_copy, font_index=regular_index)
    products = []
    names = ["나뉜 원", "작은 삼각형", "열린 사각형"]
    comments = ["두 조각의 균형을 살펴보는 도형 연습.",
                "차분한 점토색으로 그린 간결한 도형.",
                "선과 여백으로 만든 오프라인 테스트 이미지."]
    for index, path in enumerate(paths):
        products.append({
            "id": f"{index + 1:02d}", "title": names[index], "comment": comments[index],
            "program": None, "seller": None, "option": None, "quantity": None,
            "price": None, "original_url": None, "affiliate_url": None, "evidence": None,
            "asset": {"path": path.relative_to(destination).as_posix(),
                      "kind": "synthetic_fixture",
                      "source": "Generated offline by examples/offline/generate.py; abstract geometry only.",
                      "license": "Original geometric fixture; use and modify for testing.",
                      "usage_confirmed": True, "layout_permission_confirmed": True},
        })
    manifest = {
        "schema_version": 1, "mode": "concept", "fixture": True,
        "edition": "OFFLINE STUDY 001", "title": "작은 도형, 좋은 균형",
        "subtitle": "실제 상품이 아닌 오프라인 합성 도형 테스트입니다.",
        "variant": 0, "checked_at": "2026-10-21T12:00:00+09:00",
        "fonts": {"regular": {"path": regular_copy.relative_to(destination).as_posix(), "index": regular_index},
                  "bold": {"path": bold_copy.relative_to(destination).as_posix(), "index": bold_index}},
        "channel": {"name": "instagram", "status": "not_applicable", "evidence_url": None,
                    "profile_links_confirmed": False},
        "disclosures": [], "products": products,
    }
    path = destination / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regular-font", type=Path, required=True,
                        help="Installed font with Korean glyphs. No network download is performed.")
    parser.add_argument("--bold-font", type=Path, required=True)
    parser.add_argument("--regular-index", type=int, default=0, help="TTC face index; default 0")
    parser.add_argument("--bold-index", type=int, default=0, help="TTC face index; default 0")
    parser.add_argument("--destination", type=Path, required=True,
                        help="Directory for generated manifest, images, and copied fonts")
    args = parser.parse_args()
    if args.regular_index < 0 or args.bold_index < 0:
        parser.error("font indices must be non-negative")
    try:
        path = generate(args.destination, args.regular_font, args.bold_font,
                        regular_index=args.regular_index, bold_index=args.bold_index)
    except (OSError, ValueError):
        parser.exit(1, "Fixture generation failed: use safe local fonts and a new or empty destination.\n")
    print(path)
    print("Synthetic fixture only: no real products, prices, affiliate links, or AI images.")
    print(f"Next: PYTHONPATH=src python -m honeytem build '{path}' --output OUTPUT_DIR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
