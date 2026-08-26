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
        self.repeat_last_signature: dict[str, str] = {}
        self.repeat_recall_ids: dict[str, list[str]] = {}
        self.rates: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)

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

    def add_rate(
        self,
        group_openid: str,
        member_openid: str,
        message_id: str,
        *,
        threshold: int,
        window: int,
        recall_limit: int = 0,
        now: float | None = None,
    ) -> list[str]:
        """Track ordinary messages for a bounded per-member rate window."""

        if not group_openid or not member_openid or not message_id:
            return []
        now = time.monotonic() if now is None else now
        key = (group_openid, member_openid)
        events = [
            event for event in self.rates[key] if event[0] >= now - max(1, window)
        ]
        events.append((now, message_id))
        # ponytail: retain a small fixed tail; larger flood limits belong in a
        # dedicated counter if a deployment ever needs them.
        self.rates[key] = events[-100:]
        if len(events) < max(1, threshold):
            return []
        self.rates.pop(key, None)
        ids = list(dict.fromkeys(event[1] for event in events if event[1]))
        return ids[-recall_limit:] if recall_limit else ids

    def break_rate(self, group_openid: str, member_openid: str = "") -> None:
        if member_openid:
            self.rates.pop((group_openid, member_openid), None)
            return
        for key in [key for key in self.rates if key[0] == group_openid]:
            self.rates.pop(key, None)

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
        if group_openid in self.repeat_recall_ids:
            remaining = [
                message_id
                for message_id in self.repeat_recall_ids[group_openid]
                if message_id not in removed
            ]
            if remaining:
                self.repeat_recall_ids[group_openid] = remaining
            else:
                self.repeat_recall_ids.pop(group_openid, None)
        for key in [key for key in self.rates if key[0] == group_openid]:
            remaining = [
                event for event in self.rates[key] if event[1] not in removed
            ]
            if remaining:
                self.rates[key] = remaining
            else:
                self.rates.pop(key, None)

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
            self._reset_repeat_group(group_openid)
            return []
        now = time.monotonic() if now is None else now
        if self.repeat_last_signature.get(group_openid) != signature:
            self._reset_repeat_group(group_openid)
            self.repeat_last_signature[group_openid] = signature
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
        self.repeat_recall_ids[group_openid] = list(
            dict.fromkeys(event[3] for event in events if event[3])
        )[-100:]
        return members

    def consume_repeat_message_ids(self, group_openid: str) -> list[str]:
        """Return and clear the message IDs from the latest triggered round."""

        return self.repeat_recall_ids.pop(group_openid, [])

    def _reset_repeat_group(self, group_openid: str) -> None:
        self.repeat_last_signature.pop(group_openid, None)
        self.repeat_recall_ids.pop(group_openid, None)
        # ponytail: scan at most 2,000 phrase keys; index by group only if this
        # bound becomes a measurable hot path.
        for key in [key for key in self.repeats if key[0] == group_openid]:
            self.repeats.pop(key, None)

    def break_repeat(self, group_openid: str) -> None:
        self._reset_repeat_group(group_openid)


def valid_state_dict(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, dict) and str(key)
    }
