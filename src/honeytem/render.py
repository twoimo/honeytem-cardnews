"""Deterministic, rights-preserving magazine cards.

The renderer is deliberately local and pure: it accepts already-verified data
and original image/font bytes, and returns PNG bytes plus measurable geometry.
It never downloads, crops, resizes, recolours, removes backgrounds, generates
product imagery, invents prices, or substitutes a system font. All rectangles
in the QA result are half-open ``[left, top, right, bottom]`` pixel bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import unicodedata
from typing import Any, Sequence

from fontTools.ttLib import TTFont, TTLibError
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


CANVAS_SIZE = (1080, 1350)
MAX_ASSET_WIDTH = 800
MAX_ASSET_HEIGHT = 560
ASSET_MAX_SIZE = (MAX_ASSET_WIDTH, MAX_ASSET_HEIGHT)
MASTHEAD = "HONEYTEM / OBJECT JOURNAL"
AI_LABEL = "콘셉트 샘플 · AI 이미지"
FIXTURE_LABEL = "합성 테스트 이미지 · 판매용 아님"
AFFILIATE_NOTICE = "광고 · 제휴 링크 구매 시 수수료 지급"
VARIANT_NAMES = (
    "Quiet Center",
    "Editorial Index",
    "Gallery Frame",
    "Side Note",
    "Grid Study",
    "Collector Label",
    "Ink Edition",
    "Corner Notes",
    "Soft Archive",
    "Bold Baseline",
)

WHITE = "#FFFFFF"
OFFWHITE = "#F5F4F0"
LIGHTGRAY = "#ECEDEC"
INK = "#171917"
SECONDARY = "#50564F"
RULE = "#C7CAC4"
ACCENT = "#7B8375"  # Only small dots and short rules, never a panel.


def rectangles_intersect(a: Sequence[int], b: Sequence[int]) -> bool:
    """Return whether two half-open rectangles have a positive-area overlap."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _font_cmap(data: bytes, index: int) -> set[int]:
    if not isinstance(data, bytes) or not data:
        raise ValueError("Font bytes must be supplied; system-font fallback is disabled")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("Font face index must be a non-negative integer")
    try:
        with TTFont(BytesIO(data), fontNumber=index, lazy=True) as font:
            cmap = font.getBestCmap()
            if not cmap:
                raise ValueError("Provided font has no usable Unicode character map")
            return {code for code, glyph in cmap.items() if glyph != ".notdef"}
    except (TTLibError, OSError, IndexError) as exc:
        raise ValueError(f"Cannot read the supplied font face {index}: {exc}") from exc


def _check_characters(text: str, cmap: set[int], label: str) -> None:
    illegal = {c for c in text if unicodedata.category(c).startswith("C") and c != "\n"}
    if illegal:
        codes = ", ".join(f"U+{ord(c):04X}" for c in sorted(illegal))
        raise ValueError(f"{label} contains unsupported control characters: {codes}")
    missing = {ord(c) for c in text if not c.isspace() and ord(c) not in cmap}
    if missing:
        codes = ", ".join(f"U+{code:04X}" for code in sorted(missing))
        raise ValueError(f"{label}: provided font lacks glyphs {codes}; no fallback is used")


def check_glyph_coverage(text: str, font_bytes: bytes, font_index: int = 0) -> None:
    """Raise ValueError for missing glyphs rather than rendering tofu/fallback."""
    _check_characters(text, _font_cmap(font_bytes, font_index), "Text")


class _Fonts:
    def __init__(self, regular: bytes, bold: bytes, regular_index: int, bold_index: int):
        self.data = {False: regular, True: bold}
        self.indices = {False: regular_index, True: bold_index}
        self.cmaps = {
            False: _font_cmap(regular, regular_index),
            True: _font_cmap(bold, bold_index),
        }
        self.cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}

    def get(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (bold, size)
        if key not in self.cache:
            try:
                self.cache[key] = ImageFont.truetype(
                    BytesIO(self.data[bold]),
                    size=size,
                    index=self.indices[bold],
                    layout_engine=ImageFont.Layout.BASIC,
                )
            except (OSError, ValueError) as exc:
                raise ValueError("Cannot load supplied font; fallback is disabled") from exc
        return self.cache[key]

    def check(self, text: str, bold: bool, name: str) -> None:
        _check_characters(text, self.cmaps[bold], name)


def _width(text: str, font: ImageFont.FreeTypeFont) -> int:
    box = font.getbbox(text)
    return box[2] - box[0]


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int, name: str) -> list[str]:
    """Wrap Korean/Latin copy at whitespace when possible, without ellipses."""
    if max_lines == 1:
        if "\n" in text or _width(text, font) > width:
            raise ValueError(f"{name} must fit one line at {font.size}px within {width}px")
        return [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            if _width(char, font) > width:
                raise ValueError(f"{name}: one glyph exceeds its {width}px text area")
            candidate = current + char
            if _width(candidate, font) <= width:
                current = candidate
                continue
            # Do not split ordinary English words unless the word itself is
            # longer than a line. Korean can wrap at character boundaries.
            split = current.rfind(" ")
            if split >= len(current) // 3 and split > 0:
                lines.append(current[:split].rstrip())
                current = current[split + 1:] + char
            else:
                lines.append(current.rstrip())
                current = char.lstrip()
            if len(lines) >= max_lines:
                raise ValueError(f"{name} exceeds {max_lines} lines at {font.size}px")
        lines.append(current)
    if len(lines) > max_lines:
        raise ValueError(f"{name} exceeds {max_lines} lines at {font.size}px")
    return lines


@dataclass(frozen=True)
class _Style:
    background: str
    foreground: str
    align: str
    product_frame: tuple[int, int, int, int]
    cover_frame: tuple[int, int, int, int]


_STYLES = (
    _Style(WHITE, INK, "center", (140, 232, 940, 792), (140, 416, 940, 976)),
    _Style(WHITE, INK, "left", (164, 234, 964, 794), (164, 418, 964, 978)),
    _Style(OFFWHITE, INK, "center", (140, 244, 940, 804), (140, 420, 940, 980)),
    _Style(WHITE, INK, "left", (170, 230, 970, 790), (170, 418, 970, 978)),
    _Style(LIGHTGRAY, INK, "left", (140, 238, 940, 798), (140, 418, 940, 978)),
    _Style(WHITE, INK, "left", (140, 242, 940, 802), (140, 420, 940, 980)),
    _Style(INK, WHITE, "center", (140, 234, 940, 794), (140, 418, 940, 978)),
    _Style(OFFWHITE, INK, "left", (112, 232, 912, 792), (112, 418, 912, 978)),
    _Style(LIGHTGRAY, INK, "center", (140, 242, 940, 802), (140, 422, 940, 982)),
    _Style(WHITE, INK, "left", (140, 228, 940, 788), (140, 420, 940, 980)),
)


class _Page:
    def __init__(self, fonts: _Fonts, variant: int, role: str, product: dict[str, Any], filename: str):
        self.fonts = fonts
        self.variant = variant
        self.style = _STYLES[variant]
        self.role = role
        self.image = Image.new("RGB", CANVAS_SIZE, self.style.background)
        self.draw = ImageDraw.Draw(self.image)
        self.frame = self.style.cover_frame if role == "cover" else self.style.product_frame
        self.qa: dict[str, Any] = {
            "filename": filename,
            "role": role,
            "product_id": product["id"],
            "variant": variant,
            "variant_name": VARIANT_NAMES[variant],
            "background": self.style.background,
            "texts": [],
            "decorations": [],
            "asset": None,
            "mandatory_label": None,
        }

    def rectangle(self, bounds: Sequence[int], fill: str | None = None, outline: str | None = None,
                  width: int = 1, radius: int = 0) -> None:
        # Pillow rectangle endpoints are inclusive; the public coordinates are not.
        b = (bounds[0], bounds[1], bounds[2] - 1, bounds[3] - 1)
        if radius:
            self.draw.rounded_rectangle(b, radius, fill=fill, outline=outline, width=width)
        else:
            self.draw.rectangle(b, fill=fill, outline=outline, width=width)
        self.qa["decorations"].append({"kind": "rectangle", "bounds": list(bounds), "fill": fill})

    def line(self, xy: Sequence[int], fill: str = RULE, width: int = 1) -> None:
        self.draw.line(tuple(xy), fill=fill, width=width)
        self.qa["decorations"].append({"kind": "line", "coordinates": list(xy), "color": fill})

    def dot(self, x: int, y: int, radius: int = 5, color: str = ACCENT) -> None:
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        self.qa["decorations"].append({"kind": "dot", "center": [x, y], "radius": radius, "color": color})

    def text(self, name: str, text: str, x: int, y: int, width: int, *, size: int,
             bold: bool = False, max_lines: int = 1, align: str = "left",
             color: str | None = None, kind: str = "label", line_height: int | None = None,
             bottom: int | None = None) -> int:
        if not isinstance(text, str):
            raise ValueError(f"{name} must be text")
        if not text:
            return y
        self.fonts.check(text, bold, name)
        font = self.fonts.get(size, bold)
        lines = _wrap(text, font, width, max_lines, name)
        line_height = line_height or math.ceil(size * 1.22)
        all_boxes: list[list[int]] = []
        for line_number, line in enumerate(lines):
            if not line:
                continue
            metrics = font.getbbox(line)
            w, h = metrics[2] - metrics[0], metrics[3] - metrics[1]
            if h > line_height:
                raise ValueError(f"{name}: font ink exceeds fixed line height")
            px = x if align == "left" else x + ((width - w) // 2 if align == "center" else width - w)
            py = y + line_number * line_height
            box = [px, py, px + w, py + h]
            if min(box[0], box[1]) < 0 or box[2] > CANVAS_SIZE[0] or box[3] > CANVAS_SIZE[1]:
                raise ValueError(f"{name} extends beyond the canvas")
            if bottom is not None and box[3] > bottom:
                raise ValueError(f"{name} overflows its text area at {size}px")
            asset = self.qa["asset"]
            if asset and rectangles_intersect(box, asset["bounds"]):
                raise ValueError(f"{name} overlaps the original image area")
            self.draw.text((px - metrics[0], py - metrics[1]), line, font=font,
                           fill=color or self.style.foreground)
            all_boxes.append(box)
        if all_boxes:
            bounds = [min(b[0] for b in all_boxes), min(b[1] for b in all_boxes),
                      max(b[2] for b in all_boxes), max(b[3] for b in all_boxes)]
        else:
            bounds = [x, y, x, y]
        self.qa["texts"].append({
            "name": name, "text": text, "bounds": bounds, "line_bounds": all_boxes,
            "font_size": size, "bold": bold, "lines": lines, "kind": kind,
            "color": color or self.style.foreground,
        })
        return y + len(lines) * line_height

    def paste_original(self, product: dict[str, Any], raw: bytes, original: Image.Image) -> None:
        x0, y0, x1, y1 = self.frame
        w, h = original.size
        if w > x1 - x0 or h > y1 - y0:
            raise ValueError(f"Asset {product['id']} is too large for the native image frame; resizing is forbidden")
        left, top = x0 + (x1 - x0 - w) // 2, y0 + (y1 - y0 - h) // 2
        bounds = [left, top, left + w, top + h]
        for block in self.qa["texts"]:
            if rectangles_intersect(block["bounds"], bounds):
                raise ValueError(f"{block['name']} overlaps the original image area")
        self.image.paste(original, (left, top))  # No mask, conversion, resampling or EXIF transform.
        pixels = original.tobytes()
        self.qa["asset"] = {
            "product_id": product["id"],
            "kind": product["asset"]["kind"],
            "bounds": bounds,
            "frame": list(self.frame),
            "native_size": [w, h],
            "sha256": sha256(raw).hexdigest(),
            "pixel_sha256": sha256(pixels).hexdigest(),
            "pixel_preserved": self.image.crop(tuple(bounds)).tobytes() == pixels,
            "transforms": [],
        }

    def finish(self, original: Image.Image) -> bytes:
        asset = self.qa["asset"]
        if asset is None or self.image.crop(tuple(asset["bounds"])).tobytes() != original.tobytes():
            raise ValueError("Original asset pixels were changed during rendering")
        for block in self.qa["texts"]:
            if rectangles_intersect(block["bounds"], asset["bounds"]):
                raise ValueError(f"{block['name']} overlaps the original image area")
        buffer = BytesIO()
        # No timestamps, EXIF, profiles or platform-dependent ancillary metadata.
        self.image.save(buffer, format="PNG", optimize=False, compress_level=9)
        data = buffer.getvalue()
        self.qa["png_sha256"] = sha256(data).hexdigest()
        self.qa["asset"]["pixel_preserved"] = True
        return data


def _decoration(page: _Page) -> None:
    """Ten genuinely different compositions, confined to a neutral palette."""
    v, d = page.variant, page
    x0, y0, x1, y1 = page.frame
    cover = page.role == "cover"
    if v == 0:
        d.line((68, 101, 1012, 101), INK)
        d.line((356, 1020 if cover else 817, 724, 1020 if cover else 817), RULE)
        d.dot(540, 129, 4)
    elif v == 1:
        d.rectangle((68, 99, 226, 105), fill=INK)
        d.line((68, y0, 68, y1), INK, 2)
        d.line((68, y1, 105, y1), INK, 2)
        d.dot(1006, 101, 5)
    elif v == 2:
        d.rectangle((40, 112, 1040, 1028 if cover else 1116), outline=INK)
        d.rectangle((x0 - 18, y0 - 18, x1 + 18, y1 + 18), fill=WHITE, outline=RULE)
        d.line((454, 129, 626, 129), INK, 2)
    elif v == 3:
        d.rectangle((0, 0, 24, 1140), fill=INK)
        d.rectangle((52, 105, 104, 111), fill=ACCENT)
        d.line((55, 218 if not cover else 402, 55, 1021 if cover else 817), RULE)
        d.line((82, 821, 1012, 821), RULE) if not cover else None
    elif v == 4:
        d.rectangle((44, 30, 1036, 1025 if cover else 1118), outline=INK)
        d.line((44, 102, 1035, 102), INK)
        d.line((44, 207 if not cover else 401, 1035, 207 if not cover else 401), RULE)
        d.rectangle((x0 - 14, y0 - 14, x1 + 14, y1 + 14), fill=WHITE)
        d.line((44, 823 if not cover else 990, 1035, 823 if not cover else 990), RULE)
    elif v == 5:
        d.line((68, 100, 1012, 100), INK, 2)
        d.line((68, 108, 1012, 108), INK)
        d.rectangle((68, 135, 188, 192), outline=INK, radius=28)
        d.line((68, 820 if not cover else 1019, 1012, 820 if not cover else 1019), INK)
        d.dot(995, 136, 4)
    elif v == 6:
        d.line((68, 103, 1012, 103), "#777C75")
        d.rectangle((x0 - 18, y0 - 18, x1 + 18, y1 + 18), fill=WHITE)
        d.line((68, 821 if not cover else 1018, 1012, 821 if not cover else 1018), "#777C75")
        d.dot(1004, 130, 5, "#B8BDB4")
    elif v == 7:
        d.line((68, 103, 622, 103), INK)
        for x, y, sx, sy in ((x0 - 14, y0 - 14, 1, 1), (x1 + 14, y0 - 14, -1, 1),
                             (x0 - 14, y1 + 14, 1, -1), (x1 + 14, y1 + 14, -1, -1)):
            d.line((x, y, x + sx * 35, y), INK, 3)
            d.line((x, y, x, y + sy * 35), INK, 3)
        d.rectangle((975, 135, 1008, 168), outline=INK, width=2)
    elif v == 8:
        d.rectangle((36, 116, 1044, 1028 if cover else 1118), fill=WHITE, radius=30)
        d.dot(70, 140, 4, INK)
        d.dot(86, 140, 4, INK)
        d.line((68, 103, 1012, 103), "#B5BAB2")
        d.line((320, 1020 if cover else 820, 760, 1020 if cover else 820), RULE)
    elif v == 9:
        d.rectangle((0, 0, 1080, 111), fill=INK)
        if cover:
            d.rectangle((40, 145, 1040, 400), fill=INK)
        else:
            d.rectangle((40, 824, 1040, 1118), fill=INK)
        d.rectangle((68, 124, 156, 128), fill=ACCENT)


def _header(page: _Page, manifest: dict[str, Any], number: int, count: int) -> None:
    top_color = WHITE if page.variant in (6, 9) else INK
    page.text("masthead", MASTHEAD, 68, 49, 560, size=22, bold=True, color=top_color)
    page.text("edition", manifest["edition"], 650, 51, 362, size=20, align="right", color=top_color)
    label = f"COVER / {count + 1:02d}" if page.role == "cover" else f"{number:02d} / {count:02d}"
    if page.variant == 5:
        page.text("page_number", "00" if page.role == "cover" else f"{number:02d}",
                  80, 149, 96, size=28, bold=True, align="center")
        # Cover title begins below this compact issue label.
        if page.role != "cover":
            page.text("section", "OBJECT / EDITION", 220, 155, 650, size=22)
    elif page.variant == 1 and page.role != "cover":
        page.text("page_number", f"{number:02d}", 814, 119, 198, size=92, bold=True, align="right")
        page.text("section", "OBJECT / EDITION", 68, 158, 620, size=22)
    else:
        y = 127 if page.role == "cover" else 162
        page.text("page_number", label, 68, y, 360, size=22, bold=True)
        if page.role != "cover":
            page.text("section", "OBJECT / EDITION", 610, y, 402, size=22, align="right")


def _footer(page: _Page, manifest: dict[str, Any], product: dict[str, Any]) -> None:
    cover = page.role == "cover"
    top = 1040 if cover else 1140
    page.rectangle((0, top, 1080, 1350), fill=WHITE)
    page.line((68, top, 1012, top), INK)
    page.qa["disclosure_panel"] = {"bounds": [0, top, 1080, 1350], "background": WHITE}
    kind = product["asset"]["kind"]
    label = AI_LABEL if kind == "ai_concept" else FIXTURE_LABEL if kind == "synthetic_fixture" else None
    page.qa["mandatory_label"] = label
    y = top + 25
    if manifest["mode"] == "affiliate":
        y = page.text("affiliate_notice", AFFILIATE_NOTICE, 68, y, 944, size=26, bold=True,
                      color=INK, kind="disclosure", bottom=1314)
        y += 12
        if cover:
            for i, disclosure in enumerate(manifest["disclosures"]):
                y = page.text(f"disclosure_{i + 1}", disclosure["text"], 68, y, 944,
                              size=24, max_lines=7, line_height=30, color=INK,
                              kind="disclosure", bottom=1314)
                y += 9
        else:
            y = page.text("purchase_conditions", "구매 전 상품 페이지의 최종 조건을 확인하세요.",
                          68, y + 3, 944, size=24, color=SECONDARY, kind="disclosure", bottom=1314)
        if label:
            # The validation layer disallows fixture/concept imagery in sales
            # mode. Still never omit a mandatory image label if used directly.
            page.text("image_label", label, 68, y + 8, 944, size=26, bold=True,
                      color=INK, kind="disclosure", bottom=1314)
    elif label:
        y = page.text("image_label", label, 68, y, 944, size=28, bold=True,
                      color=INK, kind="disclosure", bottom=1314)
        detail = ("실제 판매 상품을 소개하는 이미지가 아닙니다." if kind == "ai_concept"
                  else "레이아웃과 원본 보존을 확인하기 위한 테스트 카드입니다.")
        page.text("image_label_detail", detail, 68, y + 13, 944, size=24,
                  color=SECONDARY, kind="disclosure", bottom=1314)
    else:
        page.text("concept_notice", "콘셉트 초안 · 판매용 아님", 68, y, 944, size=28,
                  bold=True, color=INK, kind="disclosure", bottom=1314)
    page.text("footer_folio", f"HONEYTEM  /  {page.variant:02d}", 68, 1317, 944,
              size=18, color=SECONDARY, align="right")


def _cover(page: _Page, manifest: dict[str, Any], product: dict[str, Any]) -> None:
    align = page.style.align
    # The Collector label sits at 135..192; its cover gets a compact single
    # line title below the label rather than overlapping it.
    title_y, title_lines = (212, 1) if page.variant == 5 else (165, 2)
    text_color = WHITE if page.variant in (6, 9) else INK
    page.text("cover_title", manifest["title"], 68, title_y, 944, size=68, bold=True,
              max_lines=title_lines, align=align, color=text_color, kind="heading",
              line_height=78, bottom=316)
    page.text("cover_subtitle", manifest["subtitle"], 68, 332, 944, size=30,
              max_lines=2, align=align, color=text_color, kind="body", line_height=36, bottom=400)
    page.text("cover_asset_caption", f"{product['id']} / {product['title']}", 68, 996, 944,
              size=22, align=align, color=page.style.foreground)


def _product(page: _Page, manifest: dict[str, Any], product: dict[str, Any]) -> None:
    align = page.style.align
    color = WHITE if page.variant in (6, 9) else INK
    secondary = "#D5D8D2" if page.variant in (6, 9) else SECONDARY
    page.text("product_title", product["title"], 68, 851, 944, size=56, bold=True,
              align=align, color=color, kind="heading", bottom=916)
    price = product.get("price")
    if price is not None:
        page.text("price", f"{price['amount']:,}원", 68, 930, 944, size=36, bold=True,
                  align=align, color=color, kind="body", bottom=975)
        comment_top = 988
    else:
        comment_top = 945
    page.text("comment", product["comment"], 68, comment_top, 944, size=32,
              max_lines=2, line_height=39, align=align, color=secondary, kind="body", bottom=1070)
    if price is not None:
        page.text("price_conditions_hint", "가격·옵션·배송 조건은 캡션 확인", 68, 1080, 944,
                  size=24, align=align, color=secondary, kind="body", bottom=1110)
    else:
        page.text("object_number", f"OBJECT {product['id']}  /  {len(manifest['products']):02d}",
                  68, 1068, 944, size=22, align=align, color=secondary)


def _read_original(raw: bytes, product_id: str) -> Image.Image:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError(f"Asset {product_id} must be non-empty original bytes")
    try:
        image = Image.open(BytesIO(raw))
        if image.format not in ("PNG", "JPEG"):
            raise ValueError(f"Asset {product_id}: only original PNG/JPEG files are supported")
        if image.width > MAX_ASSET_WIDTH or image.height > MAX_ASSET_HEIGHT:
            raise ValueError(f"Asset {product_id} is {image.width}×{image.height}; maximum native size is "
                             f"{MAX_ASSET_WIDTH}×{MAX_ASSET_HEIGHT}. Resizing is forbidden")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError(f"Asset {product_id}: animated/multiframe imagery is not supported")
        if image.mode != "RGB":
            raise ValueError(f"Asset {product_id} must already be RGB; automatic conversion is forbidden")
        if image.info.get("icc_profile") or any(key in image.info for key in ("gamma", "chromaticity", "srgb")):
            raise ValueError(f"Asset {product_id} has unsupported color metadata; automatic color conversion is forbidden")
        exif = image.getexif()
        if exif.get(274, 1) != 1:
            raise ValueError(f"Asset {product_id} has EXIF rotation; automatic rotation is forbidden")
        # ColorSpace commonly lives in the nested EXIF IFD, not IFD0. Check
        # either representation; an explicit non-sRGB value is unsupported.
        color_spaces = [exif.get(40961)]
        if 34665 in exif:
            color_spaces.append(exif.get_ifd(34665).get(40961))
        if any(value is not None and value != 1 for value in color_spaces):
            raise ValueError(f"Asset {product_id} has a non-sRGB EXIF color space; automatic color conversion is forbidden")
        image.load()
        return image
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Asset {product_id} is not a readable original image: {exc}") from exc


def render(manifest: dict, assets: dict[str, bytes], font_bytes: bytes,
           bold_font_bytes: bytes) -> tuple[dict[str, bytes], dict]:
    """Render one cover and one numbered card per product.

    The schema/evidence validator remains responsible for publication rights and
    factual checks. This boundary independently fails closed on unsupported
    fonts, missing glyphs, text overflow, non-RGB/oversized images, and any change
    to the decoded original pixels. It makes no network or filesystem calls.
    """
    variant = manifest.get("variant")
    if not isinstance(variant, int) or isinstance(variant, bool) or variant not in range(10):
        raise ValueError("variant must be an integer from 0 to 9")
    if manifest.get("mode") not in ("concept", "affiliate"):
        raise ValueError("mode must be concept or affiliate")
    for key in ("edition", "title", "subtitle"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"{key} must be non-empty text")
    products = manifest.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("At least one product is required")
    if manifest["mode"] == "affiliate" and not manifest.get("disclosures"):
        raise ValueError("Affiliate cover requires full disclosure text")
    for i, product in enumerate(products, 1):
        if product.get("id") != f"{i:02d}":
            raise ValueError("Product IDs must be sequential zero-padded numbers starting with 01")
        for key in ("title", "comment"):
            if not isinstance(product.get(key), str) or not product[key].strip():
                raise ValueError(f"Product {product['id']} {key} must be non-empty text")
        if product.get("asset", {}).get("kind") not in ("actual", "ai_concept", "synthetic_fixture"):
            raise ValueError(f"Product {product['id']} has an unsupported asset kind")
        if manifest["mode"] == "affiliate" and product["asset"]["kind"] != "actual":
            raise ValueError("Affiliate cards require actual authorized imagery, never AI concepts or fixtures")
        price = product.get("price")
        if price is not None and (not isinstance(price, dict) or price.get("currency") != "KRW"
                                  or not isinstance(price.get("amount"), int)
                                  or isinstance(price.get("amount"), bool) or price["amount"] < 0):
            raise ValueError(f"Product {product['id']} requires a verified non-negative KRW integer price")
        if manifest["mode"] == "concept" and price is not None:
            raise ValueError("Concept cards must not display a sales price")
    if set(assets) != {product["id"] for product in products}:
        raise ValueError("Asset IDs must exactly match the manifest product IDs")
    try:
        indices = manifest["fonts"]
        fonts = _Fonts(font_bytes, bold_font_bytes, indices["regular"]["index"], indices["bold"]["index"])
    except (KeyError, TypeError) as exc:
        raise ValueError("Explicit regular and bold font face indices are required") from exc
    originals = {product["id"]: _read_original(assets[product["id"]], product["id"])
                 for product in products}
    images: dict[str, bytes] = {}
    qa: dict[str, Any] = {
        "canvas": list(CANVAS_SIZE), "variant": variant, "variant_name": VARIANT_NAMES[variant],
        "asset_max_size": list(ASSET_MAX_SIZE), "rect_convention": "half-open [left, top, right, bottom]",
        "fonts": {
            "regular": {"sha256": sha256(font_bytes).hexdigest(), "index": indices["regular"]["index"]},
            "bold": {"sha256": sha256(bold_font_bytes).hexdigest(), "index": indices["bold"]["index"]},
            "fallback": False, "glyph_coverage_checked": True,
        },
        "cards": [],
    }
    pages = [("cover", products[0], "cover.png", 0)]
    pages += [("product", product, f"product-{product['id']}.png", i)
              for i, product in enumerate(products, 1)]
    for role, product, filename, number in pages:
        page = _Page(fonts, variant, role, product, filename)
        _decoration(page)
        _header(page, manifest, number, len(products))
        page.paste_original(product, assets[product["id"]], originals[product["id"]])
        if role == "cover":
            _cover(page, manifest, product)
        else:
            _product(page, manifest, product)
        _footer(page, manifest, product)
        images[filename] = page.finish(originals[product["id"]])
        qa["cards"].append(page.qa)
    return images, qa
