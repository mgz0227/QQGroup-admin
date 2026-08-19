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
        return list(dict.fromkeys(item[1] for item in events if item[1]))

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
