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
from urllib.parse import quote, urlencode, urlsplit
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
        {"host_mid": _uid(uid), "platform": "web", "timezone_offset": -480},
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
    nodes = value if isinstance(value, list) else []
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = _clean_dynamic_text(node.get("text") or node.get("orig_text"))
        if text:
            parts.append(text)
    return _clean_dynamic_text(" ".join(parts))


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
    if direct_cover and direct_width > 0 and direct_height > 0:
        return direct_cover, direct_width, direct_height
    list_cover = ""
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
                list_cover = list_cover or cover
    if direct_cover:
        return direct_cover, direct_width, direct_height
    if list_cover:
        return list_cover, 0, 0
    return "", 0, 0


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
        card = next(
            (
                major[name]
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
                if isinstance(major.get(name), dict)
            ),
            {},
        )
        summary = card.get("summary") if isinstance(card.get("summary"), dict) else {}
        title = next(
            (
                value
                for value in (
                    _clean_dynamic_text(card.get("title")),
                    _clean_dynamic_text(card.get("name")),
                )
                if value
            ),
            "",
        )
        dynamic_type = str(item.get("type") or "")
        text_candidates = (
            (
                _clean_dynamic_text(card.get("desc")),
                _clean_dynamic_text(card.get("description")),
                _clean_dynamic_text(desc.get("text")),
                _rich_text(desc.get("rich_text_nodes")),
                _clean_dynamic_text(summary.get("text")),
            )
            if dynamic_type == "DYNAMIC_TYPE_AV"
            else (
                _clean_dynamic_text(desc.get("text")),
                _rich_text(desc.get("rich_text_nodes")),
                _clean_dynamic_text(summary.get("text")),
                _clean_dynamic_text(card.get("desc")),
                _clean_dynamic_text(card.get("description")),
            )
        )
        text = next(
            (
                value
                for value in text_candidates
                if value and value != title
            ),
            "",
        )
        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
        url = str(basic.get("jump_url") or card.get("jump_url") or "").strip()
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
        cover, cover_width, cover_height = _first_cover_info(card)
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
