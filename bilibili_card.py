from __future__ import annotations

from html import escape
from urllib.parse import urlsplit


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


def _url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(url, quote=True)


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
    cover_url = _url(cover)
    avatar_url = _url(avatar)
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
    title_markup = f'<div class="title">{title_text}</div>' if title_text else ""
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
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: 676px;
  overflow: hidden;
  border: 1px solid #e5e8ed;
  border-radius: 22px;
  background: #fff;
  box-shadow: 0 10px 28px rgba(28, 39, 54, .10);
}}
.accent {{ height: 8px; background: linear-gradient(90deg, #fb7299 0%, #fb7299 58%, #23ade5 100%); }}
.header {{ display: flex; align-items: center; padding: 24px 28px 18px; }}
.avatar {{ width: 52px; height: 52px; flex: 0 0 52px; border-radius: 50%; object-fit: cover; background: #fb7299; }}
.fallback-avatar {{ display: grid; place-items: center; color: #fff; font-size: 24px; font-weight: 800; }}
.identity {{ min-width: 0; margin-left: 14px; }}
.author {{ overflow: hidden; color: #18191c; font-size: 21px; font-weight: 750; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }}
.meta {{ display: flex; align-items: center; gap: 8px; margin-top: 7px; color: #9499a0; font-size: 13px; line-height: 1.3; }}
.pill {{ padding: 3px 9px; border: 1px solid rgba(251,114,153,.28); border-radius: 999px; background: #fff1f5; color: #e85d87; font-weight: 650; }}
.brand {{ margin-left: auto; color: #fb7299; font-size: 13px; font-weight: 800; letter-spacing: 1.2px; }}
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
      <div class="brand">BILIBILI</div>
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
