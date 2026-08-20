from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BILIBILI_CARD_URL = "https://api.bilibili.com/x/web-interface/card?mid={}"
PURE_UID_PATTERN = re.compile(r"[1-9]\d{0,19}")
LABELED_UID_PATTERN = re.compile(
    r"(?i)(?<![A-Z])(?:UID|BILI(?:BILI)?(?:\s*UID)?)\s*[:：]?\s*"
    r"([1-9]\d{0,19})(?!\d)"
)


class BilibiliLookupError(RuntimeError):
    pass


def verification_text(request: dict[str, Any]) -> str:
    info = request.get("verify_info") or {}
    if not isinstance(info, dict):
        return ""
    message = str(info.get("verify_message") or "").strip()
    if message:
        return message
    answers = [
        str(item.get("answer") or "").strip()
        for item in info.get("review_qa_list") or []
        if isinstance(item, dict) and str(item.get("answer") or "").strip()
    ]
    return "\n".join(answers)


def parse_bilibili_uid(text: str) -> str | None:
    match = LABELED_UID_PATTERN.search(text)
    if match:
        return match.group(1)
    text = text.strip()
    return text if PURE_UID_PATTERN.fullmatch(text) else None


def parse_request_bilibili_uid(request: dict[str, Any]) -> str | None:
    info = request.get("verify_info") or {}
    if not isinstance(info, dict):
        return None
    candidates = [str(info.get("verify_message") or "").strip()]
    candidates.extend(
        str(item.get("answer") or "").strip()
        for item in info.get("review_qa_list") or []
        if isinstance(item, dict)
    )
    return next(
        (uid for value in candidates if (uid := parse_bilibili_uid(value))),
        None,
    )


def parse_keywords(value: str) -> list[str]:
    keywords = [
        item.strip() for item in re.split(r"[,，;；\r\n]+", value) if item.strip()
    ]
    keywords = list(dict.fromkeys(keywords))
    if len(keywords) > 100:
        raise ValueError("拒绝关键词最多 100 个")
    if any(len(keyword) > 64 for keyword in keywords):
        raise ValueError("单个拒绝关键词最多 64 个字符")
    return keywords


def matched_keyword(text: str, keywords: list[str]) -> str | None:
    folded = text.casefold()
    return next(
        (keyword for keyword in keywords if keyword.casefold() in folded),
        None,
    )


def keyword_reply_for_message(
    text: str,
    group_openid: str,
    group_rules: object,
    global_rules: object,
) -> str | None:
    """Return the first matching group reply, then fall back to global rules."""

    message = text.strip()
    if not message:
        return None

    def matches(rules: object, *, global_scope: bool) -> str | None:
        if not isinstance(rules, list):
            return None
        folded = message.casefold()
        for rule in rules[:100]:
            if not isinstance(rule, dict) or not bool(rule.get("enabled", True)):
                continue
            keyword = str(rule.get("keyword") or "").strip()
            reply = str(rule.get("reply") or "").strip()
            if not keyword or not reply or len(keyword) > 100 or len(reply) > 1000:
                continue
            if global_scope:
                targets = rule.get("group_openids")
                if isinstance(targets, str):
                    targets = re.split(r"[\s,，;；]+", targets.strip())
                if isinstance(targets, list):
                    targets = {
                        str(item).strip() for item in targets if str(item).strip()
                    }
                    if targets and "*" not in targets and group_openid not in targets:
                        continue
            match_type = str(rule.get("match_type") or "contains")
            keyword_folded = keyword.casefold()
            if (match_type == "exact" and folded == keyword_folded) or (
                match_type == "contains" and keyword_folded in folded
            ):
                return reply
        return None

    return matches(group_rules, global_scope=False) or matches(
        global_rules,
        global_scope=True,
    )


def parse_bilibili_response(uid: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        raise BilibiliLookupError("B 站返回格式异常")
    code = payload.get("code")
    if code == -404:
        return False
    if code != 0:
        message = str(payload.get("message") or "未知错误")
        raise BilibiliLookupError(f"B 站 UID 查询暂不可用：{message}（{code}）")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("card"), dict):
        raise BilibiliLookupError("B 站返回格式异常")
    card = data["card"]
    if str(card.get("mid") or "") != uid:
        raise BilibiliLookupError("B 站返回的 UID 与请求不一致")
    return True


def _lookup_bilibili_uid(uid: str, timeout: float) -> bool:
    request = Request(
        BILIBILI_CARD_URL.format(uid),
        headers={
            "User-Agent": "Mozilla/5.0 QQGroup-admin/1.1",
            "Referer": "https://www.bilibili.com/",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BilibiliLookupError(f"B 站 UID 查询失败：{exc}") from exc
    return parse_bilibili_response(uid, payload)


async def bilibili_uid_exists(uid: str, *, timeout: float = 10) -> bool:
    if not PURE_UID_PATTERN.fullmatch(uid):
        raise ValueError("B 站 UID 必须是正整数")
    return await asyncio.to_thread(_lookup_bilibili_uid, uid, timeout)
