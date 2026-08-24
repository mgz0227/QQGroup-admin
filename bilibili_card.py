from __future__ import annotations

from collections.abc import Mapping
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
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
    """Only fetch Bilibili media hosts when building a local card."""

    url = str(value or "").strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    roots = {"hdslb.com", "bilivideo.com", "biliimg.com", "bilibili.com"}
    if host not in roots and not host.endswith(
        (".hdslb.com", ".bilivideo.com", ".biliimg.com", ".bilibili.com")
    ):
        return ""
    return url


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


def _download_image(value: object):
    from PIL import Image, ImageOps

    url = _image_url(value)
    if not url:
        return None
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
                return None
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            return None
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1600, 1000))
        return image
    except (OSError, SyntaxError, URLError, ValueError):
        return None


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
    fitted = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius, fill=255)
    base.paste(fitted, (left, top), mask)


def render_bilibili_card(card_data: Mapping[str, Any]) -> bytes:
    """Render a self-contained Bilibili notification image with Pillow."""

    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    width = 720
    margin = 24
    card_left, card_right = margin, width - margin
    inner_left, inner_right = 54, width - 54
    inner_width = inner_right - inner_left
    author = _plain(card_data.get("author"), 52) or "B站用户"
    kind = _plain(card_data.get("kind"), 18) or "动态"
    timestamp = _plain(card_data.get("timestamp"), 24)
    title = _plain(card_data.get("title"), 180)
    summary = str(card_data.get("summary") or "").strip()[:520]
    status = _plain(card_data.get("status"), 20)
    link = _plain(card_data.get("link"), 500)
    avatar = _download_image(card_data.get("avatar"))
    cover = _download_image(card_data.get("cover"))

    regular = _font(18)
    small = _font(15)
    bold = _font(23, bold=True)
    title_font = _font(28, bold=True)
    button_font = _font(16, bold=True)

    measure = Image.new("RGB", (width, 120), "white")
    draw = ImageDraw.Draw(measure)
    title_lines = _wrap_text(draw, title, title_font, inner_width, 3)
    summary_lines = _wrap_text(draw, summary, regular, inner_width - 42, 6)
    if not title_lines and not summary_lines and cover is None:
        title_lines = ["发布了一条新动态"]

    content_height = 0
    if status:
        content_height += 32
    if title_lines:
        content_height += len(title_lines) * 36 + 10
    if summary_lines:
        content_height += len(summary_lines) * 28 + 24
    if cover is not None:
        content_height += 292
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
    brand = "B站动态"
    brand_w = draw.textlength(brand, font=small)
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
    if cover is not None:
        _rounded_image(canvas, cover, (inner_left, y, inner_right, y + 276), 14)
        y += 292

    footer_top = margin + card_height - 62
    draw.line(
        (inner_left, footer_top, inner_right, footer_top), fill="#f0f1f2", width=1
    )
    draw.text(
        (inner_left, footer_top + 23), "哔哩哔哩 · 动态推送", font=small, fill="#a6abb2"
    )
    if link:
        label = "查看原动态  →"
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

    avatar_markup = (
        f'<img class="avatar" src="{avatar_url}" alt="">'
        if avatar_url
        else '<span class="avatar fallback-avatar">B</span>'
    )
    cover_markup = (
        f'<div class="cover-wrap"><img class="cover" src="{cover_url}" alt=""></div>'
        if cover_url
        else ""
    )
    status_markup = f'<div class="status">{status_text}</div>' if status_text else ""
    if title_text:
        title_markup = f'<div class="title">{title_text}</div>'
    elif not summary_html and not cover_url:
        title_markup = f'<div class="title">发布了一条{kind_text}动态</div>'
    else:
        title_markup = ""
    summary_markup = (
        f'<div class="summary">{summary_html}</div>' if summary_html else ""
    )
    link_markup = (
        f'<a class="open-link" href="{link_url}">查看原动态 <span>↗</span></a>'
        if link_url
        else ""
    )

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
  width: 720px;
  padding: 22px;
  color: #18191c;
  background: #f3f5f7;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: 676px;
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
.cover-wrap {{ overflow: hidden; margin-top: 14px; border-radius: 14px; background: #f1f2f3; line-height: 0; }}
.cover {{ display: block; width: 100%; max-height: 360px; object-fit: contain; background: #f1f2f3; }}
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
      <div class="brand">B站动态</div>
    </div>
    <div class="content">
      {status_markup}
      {title_markup}
      {summary_markup}
      {cover_markup}
    </div>
    <div class="footer">
      <span class="source">B站动态推送</span>
      {link_markup}
    </div>
  </div>
</body>
</html>"""
