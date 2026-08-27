from __future__ import annotations

from collections.abc import Mapping
from html import escape
from io import BytesIO
from pathlib import Path
import re
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return escape(text[:limit])


def _body(value: object, limit: int) -> str:
    text = str(value or "").strip()[:limit]
    if not text:
        return ""
    return "<br>".join(
        escape(line.strip()) for line in text.splitlines() if line.strip()
    )


def _url(value: object, *, bilibili_media: bool = False) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if bilibili_media and not _image_url(url):
        return ""
    return escape(url, quote=True)


def _plain(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _image_url(value: object) -> str:
    """Return a normalized, allow-listed Bilibili media URL."""

    url = str(value or "").strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
    ):
        return ""
    roots = {"hdslb.com", "bilivideo.com", "biliimg.com", "bilibili.com"}
    if host not in roots and not host.endswith(
        (".hdslb.com", ".bilivideo.com", ".biliimg.com", ".bilibili.com")
    ):
        return ""
    # CDN links in dynamic feeds are often emitted as http or with a resized
    # ``@672w_...`` suffix.  HTTPS is more reliable in server environments;
    # the suffix-free candidate below lets callers request the readable source
    # poster without changing the displayed URL.
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _image_url_candidates(value: object) -> list[str]:
    """Return a small ordered set of safe CDN URLs (normal, then original)."""

    normalized = _image_url(value)
    if not normalized:
        return []
    candidates = [normalized]
    parsed = urlsplit(normalized)
    filename = parsed.path.rsplit("/", 1)[-1]
    marker = filename.find("@")
    if marker > 0 and re.search(r"@[^/]*\d+[wh]", filename[marker:], re.I):
        suffix = ""
        if "." in filename[marker:]:
            suffix = filename.rsplit(".", 1)[-1]
        prefix = filename[:marker]
        # Some feeds put the source extension before the resize marker, e.g.
        # ``poster.png@672w_1c.webp``.  Keep that extension instead of
        # producing the invalid ``poster.png.webp`` candidate.
        original_filename = (
            prefix if "." in prefix else prefix + (f".{suffix}" if suffix else "")
        )
        parent = parsed.path[: -len(filename)] if filename else parsed.path
        original_path = parent + original_filename
        original = urlunsplit(
            (parsed.scheme, parsed.netloc, original_path, parsed.query, "")
        )
        if original != normalized:
            candidates.append(original)
    return candidates


def bilibili_media_url_candidates(value: object) -> list[str]:
    """Return safe CDN URLs suitable for QQ's public-URL media endpoint."""

    return _image_url_candidates(value)


class _BilibiliRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = super().redirect_request(req, fp, code, msg, headers, newurl)
        if target is None or not _image_url(target.full_url):
            raise URLError("Bilibili image redirect leaves the allowlist")
        return target


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates: list[str] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_path = Path(get_astrbot_data_path())
        candidates.append(str(data_path / "font.ttf"))
    except (ImportError, OSError, RuntimeError):
        candidates = []
    names = (
        ("msyhbd.ttc", "msyhbd_0.ttc", "NotoSansCJK-Bold.ttc", "DejaVuSans-Bold.ttf")
        if bold
        else ("msyh.ttc", "msyh_0.ttc", "NotoSansCJK-Regular.ttc", "DejaVuSans.ttf")
    )
    candidates.extend(str(Path("C:/Windows/Fonts") / name) for name in names)
    candidates.extend(
        str(Path(prefix) / name)
        for prefix in (
            "/usr/share/fonts/opentype/noto",
            "/usr/share/fonts/truetype/noto",
            "/usr/share/fonts/truetype/dejavu",
        )
        for name in names
    )
    candidates.extend(names)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _download_image(value: object, *, max_size: tuple[int, int] = (1600, 1000)):
    from PIL import Image, ImageOps

    urls = _image_url_candidates(value)
    if not urls:
        return None
    for url in urls:
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 BilibiliPush/1.0",
                    "Referer": "https://www.bilibili.com/",
                },
            )
            with build_opener(_BilibiliRedirectHandler()).open(request, timeout=4) as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > 4 * 1024 * 1024:
                    continue
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                continue
            image = Image.open(BytesIO(data))
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(max_size)
            return image
        except (
            OSError,
            SyntaxError,
            URLError,
            ValueError,
            Image.DecompressionBombError,
        ):
            continue
    return None


def download_bilibili_image(value: object, *, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
    """Download a Bilibili poster for a native QQ image message.

    The rich card renderer intentionally creates a small notification canvas.
    Poster-only dynamics should instead keep their original readable width, so
    this helper returns a bounded, orientation-correct image without exposing
    arbitrary remote URLs.
    """

    from PIL import Image, ImageOps

    urls = _image_url_candidates(value)
    if not urls or max_bytes <= 0:
        return None
    # A feed often exposes a resized ``@672w`` URL first.  For the native
    # poster path prefer the suffix-free source so QQ does not magnify a low
    # resolution thumbnail; the original URL remains a bounded fallback when
    # the source poster is unavailable.
    for url in reversed(urls):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 BilibiliPush/1.0",
                    "Referer": "https://www.bilibili.com/",
                },
            )
            with build_opener(_BilibiliRedirectHandler()).open(request, timeout=4) as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > max_bytes:
                    continue
                data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                continue
            image = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
            # QQ clients display very tall originals poorly.  Keep the full poster
            # while bounding its longest edge and upload size.
            image.thumbnail((1600, 2400), resample=Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            encoded = output.getvalue()
            if len(encoded) <= max_bytes:
                return encoded
            output = BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            encoded = output.getvalue()
            if len(encoded) <= max_bytes:
                return encoded
        except (
            OSError,
            SyntaxError,
            URLError,
            ValueError,
            Image.DecompressionBombError,
        ):
            continue
    return None


def split_bilibili_poster(
    image_data: bytes,
    *,
    max_parts: int = 3,
    max_part_height: int = 900,
) -> list[bytes]:
    """Split very tall posters so QQ clients do not shrink them to a thumbnail."""

    if not isinstance(image_data, bytes) or not image_data:
        return []
    if max_parts < 2 or max_part_height < 1:
        return [image_data]
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return [image_data]
    try:
        source = ImageOps.exif_transpose(Image.open(BytesIO(image_data))).convert("RGB")
        width, height = source.size
        if height <= max_part_height or height / max(width, 1) <= 1.45:
            return [image_data]
        parts = min(max_parts, max(2, (height + max_part_height - 1) // max_part_height))
        part_height = (height + parts - 1) // parts
        result: list[bytes] = []
        for index in range(parts):
            top = index * part_height
            bottom = min(height, (index + 1) * part_height)
            if top >= bottom:
                continue
            output = BytesIO()
            source.crop((0, top, width, bottom)).save(
                output,
                format="JPEG",
                quality=92,
                optimize=True,
            )
            encoded = output.getvalue()
            if not encoded or len(encoded) > 8 * 1024 * 1024:
                return [image_data]
            result.append(encoded)
        return result or [image_data]
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return [image_data]


def _wrap_text(
    draw: Any,
    text: str,
    font: Any,
    width: int,
    max_lines: int,
) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    last = lines[-1]
    while last and draw.textlength(last + "…", font=font) > width:
        last = last[:-1]
    lines[-1] = (last or "…") + "…"
    return lines


def _rounded_image(
    base: Any,
    image: Any,
    box: tuple[int, int, int, int],
    radius: int,
) -> None:
    from PIL import Image, ImageDraw, ImageOps

    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if image is None:
        base_draw = ImageDraw.Draw(base)
        base_draw.rounded_rectangle(box, radius, fill="#f1f2f3")
        return
    # Keep the full source image.  ``fit`` cropped portrait screenshots and
    # made the notification card look like an unrelated fragment.
    fitted = ImageOps.contain(image, (width, height), method=Image.Resampling.LANCZOS)
    fitted_left = left + (width - fitted.width) // 2
    fitted_top = top + (height - fitted.height) // 2
    mask = Image.new("L", fitted.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, fitted.width, fitted.height),
        min(radius, fitted.width // 2, fitted.height // 2),
        fill=255,
    )
    base.paste(fitted, (fitted_left, fitted_top), mask)


def _card_labels(kind: object) -> tuple[str, str, str]:
    """Return brand, source and link labels for each notification type."""

    normalized = str(kind or "").strip()
    if normalized == "直播":
        return "B站直播", "哔哩哔哩 · 直播推送", "进入直播间"
    if normalized == "视频":
        return "B站视频", "哔哩哔哩 · 视频推送", "查看视频"
    return "B站动态", "哔哩哔哩 · 动态推送", "查看原动态"


def render_bilibili_card(card_data: Mapping[str, Any]) -> bytes:
    """Render a self-contained Bilibili notification image with Pillow."""

    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    width = 720
    margin = 24
    author = _plain(card_data.get("author"), 52) or "B站用户"
    kind = _plain(card_data.get("kind"), 18) or "动态"
    timestamp = _plain(card_data.get("timestamp"), 24)
    title = _plain(card_data.get("title"), 180)
    summary = str(card_data.get("summary") or "").strip()[:520]
    status = _plain(card_data.get("status"), 20)
    link = _plain(card_data.get("link"), 500)
    brand, source, default_link_label = _card_labels(kind)
    link_label = _plain(card_data.get("link_label"), 40) or default_link_label
    focus_requested = str(card_data.get("focus_cover") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    } or str(card_data.get("image_only") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    avatar = _download_image(card_data.get("avatar"))
    cover = _download_image(
        card_data.get("cover"),
        max_size=(1600, 2400) if focus_requested else (1600, 1000),
    )

    # A draw dynamic can contain all of its copy in one portrait poster.  A
    # wide side-by-side card makes that poster occupy too little of the QQ
    # message bubble, so use a narrower focus canvas for this case only.
    source_width = source_height = 0
    if cover is not None:
        source_width, source_height = cover.size
    portrait_source = bool(
        source_width > 0
        and source_height > 0
        and source_width / source_height < 0.78
    )
    image_only = bool(
        cover is not None
        and not title
        and not summary
        and str(card_data.get("image_only") or "").lower()
        in {"1", "true", "yes", "on"}
    )
    focus_cover = bool(
        cover is not None
        and str(card_data.get("focus_cover") or "").lower()
        in {"1", "true", "yes", "on"}
    )
    focus_layout = image_only or focus_cover
    if focus_layout:
        width = 560
        margin = 18

    card_left, card_right = margin, width - margin
    inner_padding = 34 if focus_layout else 54
    inner_left, inner_right = inner_padding, width - inner_padding
    inner_width = inner_right - inner_left

    regular = _font(18)
    small = _font(15)
    bold = _font(23, bold=True)
    title_font = _font(28, bold=True)
    button_font = _font(16, bold=True)

    measure = Image.new("RGB", (width, 120), "white")
    draw = ImageDraw.Draw(measure)
    title_lines = _wrap_text(draw, title, title_font, inner_width, 3)
    summary_lines = _wrap_text(draw, summary, regular, inner_width - 42, 6)
    if not title_lines and not summary_lines and cover is not None:
        title_lines = [f"发布了一条{kind}动态"]
    cover_display_size = None
    portrait_layout = False
    if cover is not None and source_width > 0 and source_height > 0:
        portrait_layout = portrait_source and not focus_layout
        max_width = inner_width if focus_layout else (278 if portrait_layout else inner_width)
        max_height = 760 if image_only else (760 if focus_layout else (470 if portrait_layout else 520))
        scale = min(max_width / source_width, max_height / source_height)
        cover_display_size = (
            max(1, int(source_width * scale)),
            max(1, int(source_height * scale)),
        )

    side_title_lines: list[str] = []
    side_summary_lines: list[str] = []
    if image_only:
        # The poster already contains the original text; leave the side copy
        # empty and add a short, unambiguous label above the image instead.
        title_lines = []
        summary_lines = []
    if portrait_layout and cover_display_size is not None:
        side_width = max(180, inner_width - cover_display_size[0] - 24)
        side_title_lines = _wrap_text(draw, title, title_font, side_width, 4)
        side_summary_lines = _wrap_text(draw, summary, regular, side_width - 18, 8)
        if not image_only and not side_title_lines and not side_summary_lines:
            side_title_lines = [f"发布了一条{kind}动态"]

    content_height = 0
    if status:
        content_height += 32
    if image_only and cover_display_size is not None:
        content_height += 36 + cover_display_size[1] + 16
    elif portrait_layout and cover_display_size is not None:
        side_height = len(side_title_lines) * 36 + (10 if side_title_lines else 0)
        side_height += len(side_summary_lines) * 28 + (24 if side_summary_lines else 0)
        content_height += max(cover_display_size[1], side_height) + 16
    else:
        if title_lines:
            content_height += len(title_lines) * 36 + 10
        if summary_lines:
            content_height += len(summary_lines) * 28 + 24
        if cover_display_size is not None:
            content_height += cover_display_size[1] + 16
    content_height = max(content_height, 64)
    card_height = 148 + content_height + 78

    canvas = Image.new("RGBA", (width, card_height + margin * 2), "#f3f5f7")
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (card_left + 2, margin + 8, card_right + 2, margin + card_height + 8),
        radius=22,
        fill=(28, 39, 54, 38),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    canvas.alpha_composite(shadow)
    draw = ImageDraw.Draw(canvas)
    card_box = (card_left, margin, card_right, margin + card_height)
    draw.rounded_rectangle(
        card_box, radius=22, fill="#ffffff", outline="#e8ebef", width=1
    )

    draw.rounded_rectangle(
        (card_left + 22, margin + 15, card_right - 92, margin + 18),
        2,
        fill="#fb7299",
    )
    draw.rounded_rectangle(
        (card_right - 88, margin + 15, card_right - 22, margin + 18),
        2,
        fill="#00aeec",
    )

    avatar_box = (inner_left, margin + 36, inner_left + 58, margin + 94)
    if avatar is not None:
        mask = Image.new("L", (58, 58), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 58, 58), fill=255)
        canvas.paste(
            ImageOps.fit(avatar, (58, 58)), (avatar_box[0], avatar_box[1]), mask
        )
    else:
        draw.ellipse(avatar_box, fill="#fb7299")
        letter = "B"
        bbox = draw.textbbox((0, 0), letter, font=bold)
        draw.text(
            (
                avatar_box[0] + (58 - bbox[2]) / 2,
                avatar_box[1] + (58 - (bbox[3] - bbox[1])) / 2 - bbox[1],
            ),
            letter,
            font=bold,
            fill="white",
        )

    text_x = inner_left + 74
    brand_w = draw.textlength(brand, font=small)
    author_width = max(180, int(inner_right - text_x - brand_w - 24))
    author = (_wrap_text(draw, author, bold, author_width, 1) or ["B站用户"])[0]
    draw.text((text_x, margin + 37), author, font=bold, fill="#18191c")
    meta_y = margin + 75
    pill_text = kind
    pill_width = int(draw.textlength(pill_text, font=small)) + 20
    draw.rounded_rectangle(
        (text_x, meta_y, text_x + pill_width, meta_y + 25),
        12,
        fill="#fff1f5",
    )
    draw.text((text_x + 10, meta_y + 4), pill_text, font=small, fill="#e85d87")
    if timestamp:
        draw.text(
            (text_x + pill_width + 10, meta_y + 4),
            timestamp,
            font=small,
            fill="#9499a0",
        )
    draw.text((inner_right - brand_w, margin + 48), brand, font=small, fill="#00aeec")
    draw.line(
        (inner_left, margin + 116, inner_right, margin + 116), fill="#f0f1f2", width=1
    )

    y = margin + 136
    if status:
        status_width = int(draw.textlength(status, font=small)) + 24
        draw.rounded_rectangle(
            (inner_left, y, inner_left + status_width, y + 26),
            13,
            fill="#fff0f0",
        )
        draw.text((inner_left + 12, y + 4), status, font=small, fill="#e85d5d")
        y += 38
    if image_only and cover_display_size is not None:
        note = f"{kind}动态 · 正文已包含在海报中"
        note_width = int(draw.textlength(note, font=small)) + 24
        draw.rounded_rectangle(
            (inner_left, y, inner_left + note_width, y + 26),
            13,
            fill="#fff1f5",
        )
        draw.text((inner_left + 12, y + 4), note, font=small, fill="#e85d87")
        y += 38
        display_width, display_height = cover_display_size
        cover_left = inner_left + (inner_width - display_width) // 2
        _rounded_image(
            canvas,
            cover,
            (cover_left, y, cover_left + display_width, y + display_height),
            14,
        )
    elif portrait_layout and cover_display_size is not None:
        display_width, display_height = cover_display_size
        _rounded_image(
            canvas,
            cover,
            (inner_left, y, inner_left + display_width, y + display_height),
            14,
        )
        side_x = inner_left + display_width + 24
        side_right = inner_right
        side_y = y
        for line in side_title_lines:
            draw.text((side_x, side_y), line, font=title_font, fill="#18191c")
            side_y += 36
        if side_title_lines:
            side_y += 4
        if side_summary_lines:
            box_top = side_y
            box_bottom = side_y + len(side_summary_lines) * 28 + 18
            draw.rounded_rectangle(
                (side_x, box_top, side_right, box_bottom),
                12,
                fill="#f7f8fa",
            )
            draw.rounded_rectangle(
                (side_x, box_top, side_x + 4, box_bottom),
                2,
                fill="#fb7299",
            )
            text_y = box_top + 9
            for line in side_summary_lines:
                draw.text((side_x + 18, text_y), line, font=regular, fill="#61666d")
                text_y += 28
    else:
        for line in title_lines:
            draw.text((inner_left, y), line, font=title_font, fill="#18191c")
            y += 36
        if title_lines:
            y += 4
        if summary_lines:
            box_top = y
            box_bottom = y + len(summary_lines) * 28 + 18
            draw.rounded_rectangle(
                (inner_left, box_top, inner_right, box_bottom),
                12,
                fill="#f7f8fa",
            )
            draw.rounded_rectangle(
                (inner_left, box_top, inner_left + 4, box_bottom),
                2,
                fill="#fb7299",
            )
            text_y = box_top + 9
            for line in summary_lines:
                draw.text((inner_left + 18, text_y), line, font=regular, fill="#61666d")
                text_y += 28
            y = box_bottom + 16
        elif not cover and title_lines:
            y += 10
        if cover_display_size is not None:
            display_width, display_height = cover_display_size
            cover_left = inner_left + (inner_width - display_width) // 2
            _rounded_image(
                canvas,
                cover,
                (cover_left, y, cover_left + display_width, y + display_height),
                14,
            )

    footer_top = margin + card_height - 62
    draw.line(
        (inner_left, footer_top, inner_right, footer_top), fill="#f0f1f2", width=1
    )
    draw.text(
        (inner_left, footer_top + 23), source, font=small, fill="#a6abb2"
    )
    if link:
        label = f"{link_label}  →"
        button_width = int(draw.textlength(label, font=button_font)) + 28
        button_left = inner_right - button_width
        draw.rounded_rectangle(
            (button_left, footer_top + 14, inner_right, footer_top + 48),
            10,
            fill="#00aeec",
        )
        draw.text(
            (button_left + 14, footer_top + 22),
            label,
            font=button_font,
            fill="white",
        )

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_bilibili_card(
    *,
    author: object,
    kind: object,
    timestamp: object = "",
    title: object = "",
    summary: object = "",
    cover: object = "",
    avatar: object = "",
    status: object = "",
    link: object = "",
    link_label: object = "",
    cover_width: object = 0,
    cover_height: object = 0,
    image_only: object = False,
    focus_cover: object = False,
) -> str:
    """Build a compact card for AstrBot's built-in HTML-to-image renderer."""

    author_text = _text(author, 80) or "B站用户"
    kind_text = _text(kind, 24) or "动态"
    timestamp_text = _text(timestamp, 32)
    status_text = _text(status, 24)
    title_text = _text(title, 180)
    summary_html = _body(summary, 420)
    cover_url = _url(cover, bilibili_media=True)
    avatar_url = _url(avatar, bilibili_media=True)
    link_url = _url(link)
    brand, source, default_link_label = _card_labels(kind)
    link_label_text = _text(link_label, 40) or default_link_label
    try:
        source_width = int(cover_width)
        source_height = int(cover_height)
    except (TypeError, ValueError):
        source_width = source_height = 0
    portrait_cover = bool(
        cover_url
        and source_width > 0
        and source_height > 0
        and source_width / source_height < 0.78
    )
    image_only_requested = bool(
        cover_url
        and not title_text
        and not summary_html
        and str(image_only or "").lower() in {"1", "true", "yes", "on"}
    )
    # Some Bilibili responses expose only the cover URL.  The browser knows
    # the intrinsic aspect ratio after loading it, so do not send these
    # poster-only dynamics back through the wide legacy layout just because
    # width/height metadata was omitted by the API.
    image_only_cover = image_only_requested
    focus_cover_requested = bool(
        cover_url
        and str(focus_cover or "").lower() in {"1", "true", "yes", "on"}
    )
    focus_layout = image_only_cover or focus_cover_requested
    if focus_cover_requested:
        # A failed native upload must still keep a portrait poster wide and
        # complete; the old side-by-side layout made its text unreadable.
        portrait_cover = False
    portrait_cover_style = ""
    if portrait_cover:
        # Focus cards are 560px wide with 28px content padding on each side.
        max_width = 460 if image_only_cover else 278
        max_height = 760 if image_only_cover else 470
        scale = min(max_width / source_width, max_height / source_height)
        display_width = max(1, int(source_width * scale))
        display_height = max(1, int(source_height * scale))
        portrait_cover_style = (
            f' style="width:{display_width}px;height:{display_height}px"'
        )

    avatar_markup = (
        f'<img class="avatar" src="{avatar_url}" alt="">'
        if avatar_url
        else '<span class="avatar fallback-avatar">B</span>'
    )
    cover_markup = (
        f'<div class="cover-wrap"><img class="cover"{portrait_cover_style} src="{cover_url}" alt=""></div>'
        if cover_url
        else ""
    )
    status_markup = f'<div class="status">{status_text}</div>' if status_text else ""
    if title_text:
        title_markup = f'<div class="title">{title_text}</div>'
    elif cover_url and not summary_html:
        title_markup = f'<div class="title">发布了一条{kind_text}动态</div>'
    else:
        title_markup = ""
    summary_markup = (
        f'<div class="summary">{summary_html}</div>' if summary_html else ""
    )
    link_markup = (
        f'<a class="open-link" href="{link_url}">{link_label_text} <span>↗</span></a>'
        if link_url
        else ""
    )
    if image_only_cover:
        note_markup = '<div class="focus-note">图文动态 · 正文已包含在海报中</div>'
        content_markup = f'<div class="focus-content">{note_markup}{cover_markup}</div>'
    elif focus_cover_requested:
        note_markup = '<div class="focus-note">图文动态 · 海报优先展示</div>'
        content_markup = (
            f'<div class="focus-content">{title_markup}{summary_markup}'
            f"{note_markup}{cover_markup}</div>"
        )
    elif portrait_cover:
        content_markup = (
            '<div class="portrait-content">'
            f'<div class="portrait-cover">{cover_markup}</div>'
            f'<div class="portrait-copy">{title_markup}{summary_markup}</div>'
            '</div>'
        )
    else:
        content_markup = f"{title_markup}{summary_markup}{cover_markup}"

    body_width = 560 if focus_layout else 720
    card_width = body_width - 44
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{
  min-height: 0;
  margin: 0;
  padding: 0;
  background: transparent;
}}
body {{
  width: {body_width}px;
  padding: 22px;
  color: #18191c;
  background: #f3f5f7;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: {card_width}px;
  overflow: hidden;
  border: 1px solid #e8ebef;
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 8px 20px rgba(28, 39, 54, .08);
}}
.accent {{ height: 4px; background: #fb7299; }}
.header {{ display: flex; align-items: center; padding: 24px 28px 18px; }}
.avatar {{ width: 52px; height: 52px; flex: 0 0 52px; border-radius: 50%; object-fit: cover; background: #fb7299; }}
.fallback-avatar {{ display: grid; place-items: center; color: #fff; font-size: 24px; font-weight: 800; }}
.identity {{ min-width: 0; margin-left: 14px; }}
.author {{ overflow: hidden; color: #18191c; font-size: 21px; font-weight: 750; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }}
.meta {{ display: flex; align-items: center; gap: 8px; margin-top: 7px; color: #9499a0; font-size: 13px; line-height: 1.3; }}
.pill {{ padding: 3px 9px; border: 1px solid rgba(251,114,153,.28); border-radius: 999px; background: #fff1f5; color: #e85d87; font-weight: 650; }}
.brand {{ margin-left: auto; color: #00aeec; font-size: 13px; font-weight: 650; }}
.content {{ padding: 0 28px 24px; }}
.status {{ margin: 0 0 13px; color: #fb7299; font-size: 13px; font-weight: 750; letter-spacing: .4px; }}
.title {{ margin-bottom: 10px; color: #18191c; font-size: 22px; font-weight: 750; line-height: 1.45; word-break: break-word; }}
 .summary {{ margin-bottom: 18px; padding: 11px 14px; border-left: 4px solid #fb7299; border-radius: 0 10px 10px 0; background: #f7f8fa; color: #61666d; font-size: 16px; line-height: 1.7; word-break: break-word; }}
 .portrait-content {{ display: flex; align-items: flex-start; gap: 22px; }}
 .portrait-cover {{ flex: 0 0 auto; }}
 .portrait-copy {{ min-width: 0; flex: 1 1 auto; }}
.focus-content {{ text-align: center; }}
.focus-note {{ display: inline-block; margin: 0 auto 14px; padding: 5px 12px; border-radius: 999px; background: #fff1f5; color: #e85d87; font-size: 13px; font-weight: 700; }}
.focus-content .cover-wrap {{ width: 100%; margin-top: 0; }}
.cover-wrap {{ display: flex; justify-content: center; overflow: hidden; margin-top: 14px; border-radius: 14px; background: #f1f2f3; line-height: 0; }}
.portrait-cover .cover-wrap {{ margin-top: 0; }}
.cover {{ display: block; width: auto; max-width: 100%; max-height: {760 if image_only_cover else (760 if focus_cover_requested else 520)}px; height: auto; object-fit: contain; background: #f1f2f3; }}
.footer {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 28px 20px; border-top: 1px solid #f0f1f2; background: #fcfcfd; }}
.source {{ color: #c0c4cc; font-size: 12px; letter-spacing: .5px; }}
.open-link {{ padding: 8px 14px; border-radius: 9px; background: #eaf8ff; color: #008ac5; font-size: 14px; font-weight: 700; text-decoration: none; white-space: nowrap; }}
.open-link span {{ font-size: 16px; }}
</style>
</head>
<body>
  <div class="card">
    <div class="accent"></div>
    <div class="header">
      {avatar_markup}
      <div class="identity">
        <div class="author">{author_text}</div>
        <div class="meta"><span class="pill">{kind_text}</span>{timestamp_text}</div>
      </div>
      <div class="brand">{brand}</div>
    </div>
    <div class="content">
      {status_markup}
      {content_markup}
    </div>
    <div class="footer">
      <span class="source">{source}</span>
      {link_markup}
    </div>
  </div>
</body>
</html>"""
