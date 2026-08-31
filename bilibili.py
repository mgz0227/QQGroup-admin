from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
)
MIXIN_KEY_ENC_TAB = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class BilibiliConfigError(ValueError):
    pass


class BilibiliAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class BilibiliQRLogin:
    qrcode_key: str
    url: str
    cookies: CookieJar
    expires_at: float


def _uid(value: str | int) -> str:
    value = str(value).strip()
    if not value.isdigit() or not 1 <= len(value) <= 20 or int(value) == 0:
        raise ValueError("B 站 UID 必须是正整数")
    return value


def parse_bilibili_uids(value: str, *, max_items: int = 100) -> list[str]:
    values = list(
        dict.fromkeys(item for item in re.split(r"[\s,，;；]+", value.strip()) if item)
    )
    if len(values) > max_items:
        raise ValueError(f"B 站 UID 最多 {max_items} 个")
    return [_uid(item) for item in values]


def _data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BilibiliAPIError("B 站返回格式异常")
    if payload.get("code") != 0:
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("msg") or "未知错误")
        raise BilibiliAPIError(f"B 站 API 返回错误：{message}（{code}）")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BilibiliAPIError("B 站返回格式异常")
    return data


def _get_json(url: str, *, cookie: str = "", timeout: float = 10) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise BilibiliAPIError(f"B 站 API HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BilibiliAPIError(f"B 站 API 请求失败：{type(exc).__name__}") from exc
    _data(payload)
    return payload


def _qr_json(url: str, cookies: CookieJar, timeout: float) -> dict[str, Any]:
    opener = build_opener(HTTPCookieProcessor(cookies))
    try:
        with opener.open(
            Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout
        ) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise BilibiliAPIError(f"B 站登录 API HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BilibiliAPIError(f"B 站登录 API 请求失败：{type(exc).__name__}") from exc
    return _data(payload)


def start_qr_login(*, timeout: float = 10) -> BilibiliQRLogin:
    cookies = CookieJar()
    data = _qr_json(QR_GENERATE_URL, cookies, timeout)
    url = str(data.get("url") or "").strip()
    qrcode_key = str(data.get("qrcode_key") or "").strip()
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{32}", qrcode_key):
        raise BilibiliAPIError("B 站登录二维码格式异常")
    return BilibiliQRLogin(qrcode_key, url, cookies, time.monotonic() + 180)


def poll_qr_login(
    login: BilibiliQRLogin,
    *,
    timeout: float = 10,
) -> tuple[Literal["waiting", "scanned", "expired", "confirmed"], str]:
    if time.monotonic() >= login.expires_at:
        return "expired", ""
    data = _qr_json(
        f"{QR_POLL_URL}?{urlencode({'qrcode_key': login.qrcode_key})}",
        login.cookies,
        timeout,
    )
    try:
        code = int(data.get("code"))
    except (TypeError, ValueError) as exc:
        raise BilibiliAPIError("B 站扫码状态格式异常") from exc
    if code == 86101:
        return "waiting", ""
    if code == 86090:
        return "scanned", ""
    if code == 86038:
        return "expired", ""
    if code != 0:
        raise BilibiliAPIError(f"B 站扫码登录失败（{code}）")
    cookie = "; ".join(
        f"{item.name}={item.value}"
        for item in login.cookies
        if item.name
        in {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"}
    )
    if "SESSDATA=" not in cookie or "bili_jct=" not in cookie:
        raise BilibiliAPIError("B 站登录成功但未返回完整 Cookie")
    return "confirmed", cookie


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    if len(raw) < 64:
        raise BilibiliAPIError("B 站 WBI 密钥格式异常")
    return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(
    params: Mapping[str, Any],
    img_key: str,
    sub_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, Any]:
    signed = {str(key): value for key, value in params.items()}
    signed["wts"] = int(time.time()) if timestamp is None else int(timestamp)
    filtered = {
        key: "".join(char for char in str(value) if char not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urlencode(filtered, quote_via=quote)
    signed["w_rid"] = hashlib.md5(
        (query + _mixin_key(img_key, sub_key)).encode()
    ).hexdigest()
    return signed


def fetch_wbi_keys(cookie: str, *, timeout: float = 10) -> tuple[str, str]:
    if not cookie.strip():
        raise BilibiliConfigError("B 站空间动态需要配置完整 Cookie")
    data = _data(_get_json(NAV_URL, cookie=cookie, timeout=timeout))
    wbi_img = data.get("wbi_img")
    if not isinstance(wbi_img, dict):
        raise BilibiliAPIError("B 站未返回 WBI 密钥")

    def filename(value: Any) -> str:
        return urlsplit(str(value or "")).path.rsplit("/", 1)[-1].split(".", 1)[0]

    img_key = filename(wbi_img.get("img_url"))
    sub_key = filename(wbi_img.get("sub_url"))
    _mixin_key(img_key, sub_key)
    return img_key, sub_key


def fetch_space_dynamics(
    uid: str | int,
    cookie: str,
    *,
    timeout: float = 10,
    wbi_keys: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if not cookie.strip():
        raise BilibiliConfigError("B 站空间动态需要配置完整 Cookie")
    keys = wbi_keys or fetch_wbi_keys(cookie, timeout=timeout)
    params = sign_wbi(
        {
            "host_mid": _uid(uid),
            "platform": "web",
            "timezone_offset": -480,
            "features": "itemOpusStyle",
        },
        *keys,
    )
    url = f"{DYNAMIC_URL}?{urlencode(params, quote_via=quote)}"
    return _get_json(url, cookie=cookie, timeout=timeout)


def _media_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or any(char in url for char in "\r\n()")
    ):
        return ""
    return url


def _clean_dynamic_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"-", "--", "—", "暂无", "暂无内容"} else text


def _rich_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("rich_text_nodes") or value.get("nodes")
    nodes = value if isinstance(value, list) else []
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_type = str(node.get("type") or "")
        if node_type == "RICH_TEXT_NODE_TYPE_WEB":
            raw_text = (
                node.get("jump_url") or node.get("orig_text") or node.get("text")
            )
        else:
            raw_text = (
                node.get("text") or node.get("orig_text") or node.get("content")
            )
        text = str(raw_text or "")
        if not text.strip():
            jump_url = str(node.get("jump_url") or "").strip()
            if jump_url.startswith("//"):
                jump_url = "https:" + jump_url
            elif jump_url.startswith("/"):
                jump_url = "https://www.bilibili.com" + jump_url
            if jump_url.startswith(("https://", "http://")):
                text = jump_url
        elif node_type == "RICH_TEXT_NODE_TYPE_WEB" and text.startswith("//"):
            text = "https:" + text
        if node_type == "RICH_TEXT_NODE_TYPE_WEB":
            parsed = urlsplit(text)
            redirect = parse_qs(parsed.query).get("redirect_url", [])
            target = str(redirect[0] if redirect else "").strip()
            target_parsed = urlsplit(target)
            if target_parsed.scheme in {"http", "https"} and target_parsed.netloc:
                text = target
        if _clean_dynamic_text(text):
            # Rich-text nodes are contiguous source fragments.  Preserve their
            # original whitespace so paragraph breaks and link boundaries are
            # not flattened into a single line.
            parts.append(text)
    return _clean_dynamic_text("".join(parts))


def _first_cover_info(card: dict[str, Any]) -> tuple[str, int, int]:
    def dimensions(value: dict[str, Any], fallback: dict[str, Any] | None = None) -> tuple[int, int]:
        fallback = fallback or {}
        width = next(
            (
                value.get(key) or fallback.get(key)
                for key in (
                    "width",
                    "img_width",
                    "image_width",
                    "pic_width",
                    "cover_width",
                )
                if value.get(key) or fallback.get(key)
            ),
            0,
        )
        height = next(
            (
                value.get(key) or fallback.get(key)
                for key in (
                    "height",
                    "img_height",
                    "image_height",
                    "pic_height",
                    "cover_height",
                )
                if value.get(key) or fallback.get(key)
            ),
            0,
        )
        try:
            return max(0, int(width)), max(0, int(height))
        except (TypeError, ValueError):
            return 0, 0

    direct_cover = ""
    direct_width = direct_height = 0
    for key in (
        "cover",
        "cover_url",
        "pic",
        "image",
        "image_url",
        "thumbnail",
        "thumb",
    ):
        raw = card.get(key)
        if isinstance(raw, dict):
            cover = _media_url(
                raw.get("url")
                or raw.get("src")
                or raw.get("img_src")
                or raw.get("image_url")
            )
            direct_width, direct_height = dimensions(raw, card)
        else:
            cover = _media_url(raw)
            direct_width, direct_height = dimensions(card)
        if cover:
            direct_cover = cover
            break
    list_cover = ""
    list_width = list_height = 0
    for key in ("pics", "covers", "images", "items"):
        values = card.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                width = value.get("width") or value.get("img_width") or 0
                height = value.get("height") or value.get("img_height") or 0
                value = (
                    value.get("url")
                    or value.get("src")
                    or value.get("img_src")
                    or value.get("image_url")
                )
            else:
                width = height = 0
            cover = _media_url(value)
            if cover:
                try:
                    width = int(width)
                    height = int(height)
                except (TypeError, ValueError):
                    width = height = 0
                if width > 0 and height > 0:
                    return cover, width, height
                if not list_cover:
                    list_cover = cover
                    list_width, list_height = width, height
    if direct_cover and direct_width > 0 and direct_height > 0:
        return direct_cover, direct_width, direct_height
    if direct_cover:
        return direct_cover, direct_width, direct_height
    if list_cover:
        return list_cover, list_width, list_height
    return "", 0, 0


def _cover_infos(card: dict[str, Any], *, max_items: int = 3) -> list[tuple[str, int, int]]:
    """Return a bounded gallery while preserving the legacy first-cover choice."""

    if not isinstance(card, dict) or max_items <= 0:
        return []
    values: list[tuple[str, int, int]] = []
    for key in ("pics", "covers", "images", "items"):
        raw_values = card.get(key)
        if not isinstance(raw_values, list):
            continue
        for raw in raw_values:
            value = _first_cover_info({key: [raw]})
            if value[0]:
                values.append(value)
    if not values:
        url, width, height = _first_cover_info(card)
        return [(url, width, height)] if url else []
    # The first dimensioned gallery item is the same item selected by the
    # existing parser; put it first, then retain the remaining gallery order.
    dimensioned = next(
        (index for index, item in enumerate(values) if item[1] and item[2]),
        None,
    )
    if dimensioned is not None:
        values = [values[dimensioned], *values[:dimensioned], *values[dimensioned + 1 :]]
    else:
        first, width, height = _first_cover_info(card)
        if first:
            values = [(first, width, height), *values]
    result: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for value in values:
        if value[0] in seen:
            continue
        seen.add(value[0])
        result.append(value)
        if len(result) >= max_items:
            break
    return result


def _first_cover(card: dict[str, Any]) -> str:
    return _first_cover_info(card)[0]


def parse_dynamic_items(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    items = _data(payload).get("items")
    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict) or item.get("visible") is False:
            continue
        dynamic_id = str(item.get("id_str") or "").strip()
        if not dynamic_id:
            continue
        modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
        author = (
            modules.get("module_author")
            if isinstance(modules.get("module_author"), dict)
            else {}
        )
        dynamic = (
            modules.get("module_dynamic")
            if isinstance(modules.get("module_dynamic"), dict)
            else {}
        )
        desc = dynamic.get("desc") if isinstance(dynamic.get("desc"), dict) else {}
        major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
        dynamic_type = str(item.get("type") or "")
        orig = (
            item.get("orig")
            if dynamic_type == "DYNAMIC_TYPE_FORWARD"
            and isinstance(item.get("orig"), dict)
            else {}
        )
        orig_modules = (
            orig.get("modules")
            if isinstance(orig.get("modules"), dict)
            else {}
        )
        orig_dynamic = (
            orig_modules.get("module_dynamic")
            if isinstance(orig_modules.get("module_dynamic"), dict)
            else {}
        )
        orig_desc = (
            orig_dynamic.get("desc")
            if isinstance(orig_dynamic.get("desc"), dict)
            else {}
        )
        orig_major = (
            orig_dynamic.get("major")
            if isinstance(orig_dynamic.get("major"), dict)
            else {}
        )

        def first_card(value: dict[str, Any]) -> dict[str, Any]:
            return next(
                (
                    value[name]
                    for name in (
                        "opus",
                        "archive",
                        "article",
                        "draw",
                        "ugc_season",
                        "live",
                        "common",
                        "music",
                        "pgc",
                        "courses",
                        "forward",
                    )
                    if isinstance(value.get(name), dict)
                ),
                {},
            )

        primary_card = first_card(major)
        original_card = first_card(orig_major)
        cards = [card for card in (primary_card, original_card) if card]
        summaries = [
            card.get("summary")
            for card in cards
            if isinstance(card.get("summary"), dict)
        ]
        title = next(
            (
                value
                for card in cards
                for value in (
                    _clean_dynamic_text(card.get("title")),
                    _clean_dynamic_text(card.get("name")),
                )
                if value
            ),
            "",
        )
        card_text = [
            value
            for card in cards
            for value in (
                _clean_dynamic_text(card.get("desc")),
                _clean_dynamic_text(card.get("description")),
            )
            if value
        ]
        desc_text = [
            value
            for current_desc in (desc, orig_desc)
            for value in (
                _rich_text(current_desc),
                _clean_dynamic_text(current_desc.get("text")),
            )
            if value
        ]
        summary_text = [
            value
            for summary in summaries
            for value in (
                _rich_text(summary),
                _clean_dynamic_text(summary.get("text")),
            )
            if value
        ]
        has_opus = any(
            isinstance(value.get("opus"), dict) for value in (major, orig_major)
        )
        if has_opus and title and any(
            value != title and value.startswith(title) for value in summary_text
        ):
            # The Opus feed often exposes the first few body characters as a
            # synthetic title.  Rendering it above the full summary repeats
            # the same sentence in QQ, so retain only genuinely distinct
            # titles.
            title = ""
        primary_opus = isinstance(major.get("opus"), dict)
        if dynamic_type == "DYNAMIC_TYPE_AV":
            text_candidates = (*card_text, *desc_text, *summary_text)
        elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
            original_summary = (
                original_card.get("summary")
                if isinstance(original_card.get("summary"), dict)
                else {}
            )
            forward_note = next(
                (
                    value
                    for value in (
                        _rich_text(desc),
                        _clean_dynamic_text(desc.get("text")),
                    )
                    if value and value != title
                ),
                "",
            )
            original_values = (
                (
                    _rich_text(original_summary),
                    _clean_dynamic_text(original_summary.get("text")),
                    _rich_text(orig_desc),
                    _clean_dynamic_text(orig_desc.get("text")),
                )
                if isinstance(orig_major.get("opus"), dict)
                else (
                    _rich_text(orig_desc),
                    _clean_dynamic_text(orig_desc.get("text")),
                    _rich_text(original_summary),
                    _clean_dynamic_text(original_summary.get("text")),
                )
            )
            original_text = next(
                (
                    value
                    for value in (
                        *original_values,
                        _clean_dynamic_text(original_card.get("desc")),
                        _clean_dynamic_text(original_card.get("description")),
                    )
                    if value and value != title
                ),
                "",
            )
            text_candidates = (
                "\n\n".join(dict.fromkeys(filter(None, (forward_note, original_text)))),
            )
        elif primary_opus:
            text_candidates = (*summary_text, *desc_text, *card_text)
        else:
            text_candidates = (*desc_text, *summary_text, *card_text)
        text = next(
            (
                value
                for value in text_candidates
                if value and value != title
            ),
            "",
        )
        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
        orig_basic = orig.get("basic") if isinstance(orig.get("basic"), dict) else {}
        jump_urls = (
            (
                *(card.get("jump_url") for card in cards),
                basic.get("jump_url"),
                orig_basic.get("jump_url"),
            )
            if dynamic_type == "DYNAMIC_TYPE_AV"
            else (
                basic.get("jump_url"),
                *(card.get("jump_url") for card in cards),
                orig_basic.get("jump_url"),
            )
        )
        url = next(
            (
                str(value).strip()
                for value in jump_urls
                if str(value or "").strip()
            ),
            "",
        )
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.bilibili.com" + url
        elif not url:
            url = f"https://www.bilibili.com/opus/{dynamic_id}"
        try:
            pub_ts = int(author.get("pub_ts") or 0)
        except (TypeError, ValueError):
            pub_ts = 0
        cover = ""
        cover_width = cover_height = 0
        gallery: list[tuple[str, int, int]] = []
        image_count = 0
        for candidate in cards:
            all_images = _cover_infos(candidate, max_items=99)
            if all_images:
                gallery = all_images[:3]
                image_count = len(all_images)
                cover, cover_width, cover_height = gallery[0]
                break
        parsed_item = {
            "id": dynamic_id,
            "type": dynamic_type,
            "uid": str(author.get("mid") or ""),
            "author": str(author.get("name") or ""),
            "pub_ts": pub_ts,
            "title": title,
            "text": text,
            "url": url,
            "cover": cover,
        }
        if cover_width and cover_height:
            parsed_item["cover_width"] = cover_width
            parsed_item["cover_height"] = cover_height
        if image_count > 1:
            parsed_item["image_count"] = image_count
        if len(gallery) > 1:
            parsed_item["images"] = [
                {"url": url, "width": width, "height": height}
                for url, width, height in gallery
            ]
        avatar = _media_url(author.get("face"))
        if avatar:
            parsed_item["avatar"] = avatar
        result.append(parsed_item)
    return result


def fetch_live_statuses(
    uids: Sequence[str | int], *, timeout: float = 10
) -> dict[str, dict[str, Any]]:
    normalized = list(dict.fromkeys(_uid(uid) for uid in uids))
    if not normalized:
        raise ValueError("至少需要一个 B 站 UID")
    if len(normalized) > 100:
        raise ValueError("单次最多查询 100 个 B 站 UID")
    query = urlencode([("uids[]", uid) for uid in normalized], quote_via=quote)
    data = _data(_get_json(f"{LIVE_STATUS_URL}?{query}", timeout=timeout))
    return {
        str(uid): status for uid, status in data.items() if isinstance(status, dict)
    }


def live_transition(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> Literal["start", "stop"] | None:
    if previous is None or current is None:
        return None
    try:
        before = int(previous.get("live_status") or 0)
        now = int(current.get("live_status") or 0)
    except (TypeError, ValueError):
        return None
    if before != 1 and now == 1:
        return "start"
    if before == 1 and now != 1:
        return "stop"
    if before == now == 1:
        old_time = previous.get("live_time")
        new_time = current.get("live_time")
        if old_time and new_time and str(old_time) != str(new_time):
            return "start"
    return None
