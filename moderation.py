from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any


def normalize_message(text: str, image_count: int = 0) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    if image_count:
        text = f"{text} {'[图片]' * image_count}".strip()
    return text[:500]


class ModerationWindows:
    """Small in-memory windows; persistent evidence belongs in AstrBot KV."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str, str, str], tuple[float, bool]] = {}
        self.images: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
        self.group_images: dict[str, list[tuple[float, str, str]]] = defaultdict(
            list
        )
        self.recent_messages: dict[
            str, list[tuple[float, str, str, str]]
        ] = defaultdict(list)
        self.repeats: dict[tuple[str, str], list[tuple[float, str, str, str]]] = (
            defaultdict(list)
        )

    def duplicate(
        self,
        key: tuple[str, str, str, str],
        *,
        now: float | None = None,
    ) -> bool | None:
        now = time.monotonic() if now is None else now
        cached = self.seen.get(key)
        if cached and cached[0] > now:
            return cached[1]
        self.seen.pop(key, None)
        if len(self.seen) > 2000:
            self.seen = {
                key: value for key, value in self.seen.items() if value[0] > now
            }
        return None

    def remember(
        self,
        key: tuple[str, str, str, str],
        consumed: bool,
        *,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        if len(self.seen) >= 2000 and key not in self.seen:
            self.seen.pop(min(self.seen, key=lambda item: self.seen[item][0]), None)
        self.seen[key] = (now + 180, consumed)

    def add_images(
        self,
        group_openid: str,
        member_openid: str,
        message_id: str,
        count: int,
        *,
        threshold: int,
        window: int,
        recall_limit: int = 0,
        now: float | None = None,
    ) -> list[str]:
        if count <= 0:
            self.images.pop((group_openid, member_openid), None)
            return []
        now = time.monotonic() if now is None else now
        key = (group_openid, member_openid)
        events = [event for event in self.images[key] if event[0] >= now - window]
        events.extend((now, message_id) for _ in range(count))
        self.images[key] = events
        if len(events) < threshold:
            return []
        self.images.pop(key, None)
        message_ids = list(dict.fromkeys(item[1] for item in events if item[1]))
        return message_ids[-recall_limit:] if recall_limit else message_ids

    def add_group_images(
        self,
        group_openid: str,
        member_openid: str,
        message_id: str,
        count: int,
        *,
        threshold: int,
        min_members: int,
        window: int,
        recall_limit: int = 0,
        now: float | None = None,
    ) -> list[str]:
        if count <= 0:
            self.group_images.pop(group_openid, None)
            return []
        now = time.monotonic() if now is None else now
        events = [
            event
            for event in self.group_images[group_openid]
            if event[0] >= now - window
        ]
        events.extend((now, message_id, member_openid) for _ in range(count))
        self.group_images[group_openid] = events[-100:]
        members = {event[2] for event in events if event[2]}
        if len(events) < threshold or len(members) < min_members:
            return []
        self.group_images.pop(group_openid, None)
        message_ids = list(dict.fromkeys(event[1] for event in events if event[1]))
        return message_ids[-recall_limit:] if recall_limit else message_ids

    def break_image_chain(self, group_openid: str, member_openid: str) -> None:
        self.images.pop((group_openid, member_openid), None)
        self.group_images.pop(group_openid, None)

    def record_message(
        self,
        group_openid: str,
        member_openid: str,
        message_id: str,
        member_role: str,
        *,
        now: float | None = None,
    ) -> None:
        if not group_openid or not member_openid or not message_id:
            return
        now = time.monotonic() if now is None else now
        events = [
            event
            for event in self.recent_messages[group_openid]
            if event[0] >= now - 120
        ]
        events.append((now, message_id, member_openid, member_role))
        self.recent_messages[group_openid] = events[-200:]

    def newest_message_ids(
        self,
        group_openid: str,
        limit: int = 50,
        *,
        member_openid: str = "",
        exclude_message_id: str = "",
        now: float | None = None,
    ) -> list[str]:
        now = time.monotonic() if now is None else now
        events = [
            event
            for event in self.recent_messages.get(group_openid, [])
            if event[0] >= now - 120
        ]
        self.recent_messages[group_openid] = events
        return [
            event[1]
            for event in reversed(events)
            if event[1] != exclude_message_id
            and event[3] not in {"admin", "owner"}
            and (not member_openid or event[2] == member_openid)
        ][: min(50, max(0, limit))]

    def forget_messages(self, group_openid: str, message_ids: list[str]) -> None:
        removed = set(message_ids)
        if not removed:
            return
        self.recent_messages[group_openid] = [
            event
            for event in self.recent_messages.get(group_openid, [])
            if event[1] not in removed
        ]
        self.group_images[group_openid] = [
            event
            for event in self.group_images.get(group_openid, [])
            if event[1] not in removed
        ]
        for key in [key for key in self.images if key[0] == group_openid]:
            self.images[key] = [
                event for event in self.images[key] if event[1] not in removed
            ]
        for key in [key for key in self.repeats if key[0] == group_openid]:
            self.repeats[key] = [
                event for event in self.repeats[key] if event[3] not in removed
            ]

    def add_repeat(
        self,
        group_openid: str,
        signature: str,
        member_openid: str,
        member_role: str,
        message_id: str,
        *,
        threshold: int,
        window: int,
        now: float | None = None,
    ) -> list[str]:
        if not signature:
            return []
        now = time.monotonic() if now is None else now
        key = (group_openid, signature)
        if len(self.repeats) >= 2000 and key not in self.repeats:
            # ponytail: a hard cap may forget an old pattern; persistent evidence
            # belongs in storage if 2000 concurrent phrases becomes a real limit.
            self.repeats.pop(next(iter(self.repeats)), None)
        events = [event for event in self.repeats[key] if event[0] >= now - window]
        events.append((now, member_openid, member_role, message_id))
        self.repeats[key] = events
        members = list(
            dict.fromkeys(
                event[1]
                for event in events
                if event[1] and event[2] not in {"admin", "owner"}
            )
        )
        if len(events) < threshold or len(members) < 2:
            return []
        self.repeats.pop(key, None)
        return members


def valid_state_dict(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, dict) and str(key)
    }
