from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
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
                    "ugc_season",
                    "live",
                    "common",
                    "music",
                    "pgc",
                    "courses",
                )
                if isinstance(major.get(name), dict)
            ),
            {},
        )
        summary = card.get("summary") if isinstance(card.get("summary"), dict) else {}
        title = str(card.get("title") or "").strip()
        text = str(
            desc.get("text") or summary.get("text") or card.get("desc") or title
        ).strip()
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
        result.append(
            {
                "id": dynamic_id,
                "type": str(item.get("type") or ""),
                "uid": str(author.get("mid") or ""),
                "author": str(author.get("name") or ""),
                "pub_ts": pub_ts,
                "title": title,
                "text": text,
                "url": url,
            }
        )
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
