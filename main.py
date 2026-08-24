from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from contextlib import suppress
from functools import wraps
from io import BytesIO
from typing import Any
from xml.sax.saxutils import quoteattr

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .bilibili import (
    BilibiliAPIError,
    BilibiliConfigError,
    BilibiliQRLogin,
    fetch_live_statuses,
    fetch_space_dynamics,
    fetch_wbi_keys,
    live_transition,
    parse_bilibili_uids,
    parse_dynamic_items,
    poll_qr_login,
    start_qr_login,
)
from .image_ocr import (
    embedded_image_text,
    normalize_vision_image_ref,
    ocr_image_url,
)
from .moderation import ModerationWindows, normalize_message, valid_state_dict
from .qq_api import (
    QQAPIError,
    QQGroupAPI,
    future_rfc3339,
    parse_duration,
    parse_openids,
    parse_qq_number_text,
    parse_qq_numbers,
    select_group_strategy,
    whitelist_diff,
)
from .review import (
    BilibiliLookupError,
    bilibili_uid_exists,
    keyword_reply_for_message,
    matched_keyword,
    parse_keywords,
    parse_request_bilibili_uid,
    verification_text,
)
from .web import GroupAdminWeb

QQ_PLATFORM_TYPES = (
    filter.PlatformAdapterType.QQOFFICIAL
    | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
)
QQ_PLATFORM_NAMES = {"qq_official", "qq_official_webhook"}
INTERACTION_INTENT = 1 << 26
BUTTON_TOKEN_TTL = 15 * 60
SETTINGS_MESSAGE_TTL = 45
VERIFICATION_TOKEN_TTL = 5 * 60
JOIN_LIST_LIMIT = 5
RECENT_RECALL_LIMIT = 50
COMMAND_PANEL_REMARK = "astrbot_plugin_qqgroup_admin managed"
COMMAND_PANEL = {
    "items": [
        {
            "type": "command",
            "name": "/审核设置",
            "desc": "配置审核与消息审查",
            "only_admin": True,
        },
        {
            "type": "command",
            "name": "/申请列表",
            "desc": "查看待处理入群申请",
            "only_admin": True,
        },
        {
            "type": "command",
            "name": "/禁言状态",
            "desc": "查看成员与全员禁言",
            "only_admin": True,
        },
        {
            "type": "command",
            "name": "/机器人状态",
            "desc": "检查机器人群内权限",
            "only_admin": True,
        },
    ],
    "remark": COMMAND_PANEL_REMARK,
}
GROUP_TEMPLATE_KEY = "qq_group"
GLOBAL_AI_ENABLED_KEY = "global_ai_review_enabled"
GLOBAL_AI_PROVIDER_KEY = "global_ai_review_provider_id"
GLOBAL_AI_FALLBACKS_KEY = "global_ai_review_fallback_provider_ids"
GLOBAL_AI_CONFIRM_PROVIDER_KEY = "global_ai_review_confirm_provider_id"
GLOBAL_AI_TIMEOUT_KEY = "global_ai_review_timeout_seconds"
GLOBAL_AI_IMAGES_KEY = "global_ai_review_images_enabled"
GLOBAL_AI_BLOCK_THRESHOLD_KEY = "global_ai_review_block_threshold"
GLOBAL_AI_ACTION_KEY = "global_ai_review_action"
GLOBAL_IMAGE_KEYWORDS_KEY = "global_image_reject_keywords"
GLOBAL_IMAGE_OCR_ENABLED_KEY = "global_image_ocr_enabled"
GLOBAL_IMAGE_OCR_PROVIDER_KEY = "global_image_ocr_provider_id"
GLOBAL_IMAGE_OCR_TIMEOUT_KEY = "global_image_ocr_timeout_seconds"
GLOBAL_IMAGE_OCR_MAX_IMAGES_KEY = "global_image_ocr_max_images"
GLOBAL_AI_MIGRATED_KEY = "global_ai_review_migrated"
MAX_AI_FALLBACK_PROVIDERS = 3
AI_REVIEW_TOTAL_TIMEOUT_SECONDS = 20
AI_REVIEW_DEFAULT_BLOCK_THRESHOLD = 95
AI_REVIEW_ACTIONS = {"recall", "record_only"}
IMAGE_OCR_DEFAULT_TIMEOUT_SECONDS = 4
IMAGE_OCR_DEFAULT_MAX_IMAGES = 1
CONDITION_LOGICS = {"all", "any"}
FALLBACK_ACTIONS = {"pending", "decline", "approve"}
GROUP_ADMIN_ROLES = {"admin", "owner"}
GROUP_PERMISSION_ERROR_CODES = {11282, 40011030}
SETTINGS_ACTIONS = {
    "bind",
    "home",
    "conditions",
    "keywords",
    "bilibili",
    "native",
    "uid",
    "conditional",
    "uid_on",
    "uid_off",
    "direct_on",
    "direct_off",
    "all",
    "any",
    "pending",
    "decline",
    "approve",
    "sync",
    "off",
    "moderation",
    "mod_on",
    "mod_off",
    "ai_on",
    "ai_off",
    "image_on",
    "image_off",
    "repeat_on",
    "repeat_off",
    "verify_on",
    "verify_off",
    "bili_dynamic_on",
    "bili_dynamic_off",
    "bili_live_on",
    "bili_live_off",
}
STATE_KEY = "qqgroup_admin_state_v1"


def normalize_provider_ids(
    value: Any, *, limit: int = MAX_AI_FALLBACK_PROVIDERS
) -> list[str]:
    """Normalize provider IDs from WebUI text/list values while preserving order."""
    values = value if isinstance(value, (list, tuple)) else re.split(
        r"[,，;；\r\n]+", str(value or "")
    )
    result: list[str] = []
    for item in values:
        provider_id = str(item or "").strip()
        if provider_id and provider_id not in result:
            result.append(provider_id)
        if len(result) >= limit:
            break
    return result


def parse_member_list(value: Any, *, max_items: int = 10_000) -> list[str]:
    """Normalize member/union OpenIDs and optional bound Bilibili UIDs."""
    items = [
        item.strip()
        for item in re.split(r"[\s,，;；]+", str(value or ""))
        if item.strip()
    ]
    items = list(dict.fromkeys(items))
    if len(items) > max_items:
        raise ValueError(f"成员名单最多 {max_items} 个")
    if any(len(item) > 128 for item in items):
        raise ValueError("成员 OpenID 最多 128 个字符")
    return items


class WakeCommandFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, _cfg: AstrBotConfig) -> bool:
        return bool(event.is_at_or_wake_command)


def guarded(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        event = args[1]
        try:
            async for result in handler(*args, **kwargs):
                yield result
        except (QQAPIError, TypeError, ValueError, RuntimeError) as exc:
            yield event.plain_result(f"操作失败：{exc}")

    return wrapper


def qq_admin_command(name: str):
    def decorator(handler):
        wrapped = guarded(handler)
        wrapped = filter.command(name)(wrapped)
        wrapped = filter.platform_adapter_type(QQ_PLATFORM_TYPES)(wrapped)
        wrapped = filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)(
            wrapped
        )
        return filter.permission_type(filter.PermissionType.ADMIN)(wrapped)

    return decorator


def qq_group_command(name: str):
    def decorator(handler):
        wrapped = guarded(handler)
        wrapped = filter.command(name)(wrapped)
        wrapped = filter.platform_adapter_type(QQ_PLATFORM_TYPES)(wrapped)
        return filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)(wrapped)

    return decorator


def qq_admin_regex(pattern: str):
    def decorator(handler):
        wrapped = guarded(handler)
        wrapped = filter.regex(pattern)(wrapped)
        wrapped = filter.custom_filter(WakeCommandFilter, False)(wrapped)
        wrapped = filter.platform_adapter_type(QQ_PLATFORM_TYPES)(wrapped)
        wrapped = filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)(
            wrapped
        )
        return filter.permission_type(filter.PermissionType.ADMIN)(wrapped)

    return decorator


def split_message(text: str, limit: int = 3000) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks or [""]


class QQGroupAdmin(Star):
    HELP = """QQ 群聊管理命令
/群信息
/机器人状态
/申请列表 [游标]
/审核设置
/同步指令面板
/禁言状态
/禁言 <成员OpenID|@成员> <60|30m|2h|7d>
/解禁 <成员OpenID|@成员>
/撤回 <数量>|<成员OpenID|@成员> [数量]
/全体禁言
/全体解禁
/自动审核状态
/自动审核开启 <QQ号,...>
/自动审核添加 <QQ号,...>
/自动审核移除 <QQ号,...>
/自动审核同步 确认
/自动审核关闭 确认"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._review_task: asyncio.Task[None] | None = None
        self._recall_tasks: set[asyncio.Task[None]] = set()
        self._approval_lock = asyncio.Lock()
        self._last_approval_at = 0.0
        self._recall_lock = asyncio.Lock()
        self._last_recall_at = 0.0
        self._command_panel_lock = asyncio.Lock()
        self._approval_tokens: dict[str, tuple[float, str, str, str]] = {}
        self._settings_tokens: dict[str, tuple[float, str, str, str]] = {}
        self._verification_tokens: dict[str, tuple[float, str, str, int]] = {}
        self._keyword_reply_ready_at: dict[str, float] = {}
        self._bilibili_logins: dict[str, BilibiliQRLogin] = {}
        self._poll_cursors: dict[tuple[str, str], str] = {}
        self._permission_diagnostics: dict[tuple[str, str], str] = {}
        self._patched_clients: dict[Any, Any] = {}
        self._bilibili_retry_at = 0.0
        self._bilibili_live_retry_at = 0.0
        self._bilibili_dynamic_retry_at: dict[str, float] = {}
        self._bilibili_push_warning_at = 0.0
        self._bilibili_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._uid_bindings: dict[str, dict[str, Any]] = {}
        self._suspicious_members: dict[str, dict[str, Any]] = {}
        self._violation_records: list[dict[str, Any]] = []
        self._last_violation_state_save_at = 0.0
        self._violation_state_dirty = False
        self._bilibili_state: dict[str, dict[str, Any]] = {
            "live": {},
            "dynamic": {},
        }
        self._moderation = ModerationWindows()
        self._ai_semaphore = asyncio.Semaphore(2)
        self._ai_warning_at = 0.0
        self._migrate_config()
        self._web = GroupAdminWeb(self, context)

    def _migrate_config(self) -> None:
        changed = False
        if bool(self.config.get("mute_reply_at_member", False)):
            template = str(
                self.config.get(
                    "mute_success_message",
                    "已设置禁言，至 {expire_at}。",
                )
                or "已设置禁言，至 {expire_at}。"
            )
            if "{at_user}" not in template:
                self.config["mute_success_message"] = f"{{at_user}} {template}"
            self.config["mute_reply_at_member"] = False
            changed = True

        entries = self.config.get("auto_review_groups") or []
        entries_for_ai = entries if isinstance(entries, list) else []
        legacy_ai_entries = [
            entry
            for entry in entries_for_ai
            if isinstance(entry, dict)
            and any(
                key in entry
                for key in (
                    "ai_review_enabled",
                    "ai_review_provider_id",
                    "ai_review_fallback_provider_id",
                )
            )
        ]
        if not bool(self.config.get(GLOBAL_AI_MIGRATED_KEY, False)):
            global_values_are_defaults = not bool(
                self.config.get(GLOBAL_AI_ENABLED_KEY, False)
            ) and not str(self.config.get(GLOBAL_AI_PROVIDER_KEY) or "").strip() and not normalize_provider_ids(
                self.config.get(GLOBAL_AI_FALLBACKS_KEY)
            )
            if legacy_ai_entries and global_values_are_defaults:
                self.config[GLOBAL_AI_ENABLED_KEY] = any(
                    bool(entry.get("ai_review_enabled")) for entry in legacy_ai_entries
                )
                self.config[GLOBAL_AI_PROVIDER_KEY] = next(
                    (
                        str(entry.get("ai_review_provider_id") or "").strip()
                        for entry in legacy_ai_entries
                        if str(entry.get("ai_review_provider_id") or "").strip()
                    ),
                    "",
                )
                fallback_ids: list[str] = []
                for entry in legacy_ai_entries:
                    fallback_ids.extend(
                        normalize_provider_ids(entry.get("ai_review_fallback_provider_id"))
                    )
                self.config[GLOBAL_AI_FALLBACKS_KEY] = normalize_provider_ids(fallback_ids)
                changed = True
            self.config[GLOBAL_AI_MIGRATED_KEY] = True
            changed = True
        if GLOBAL_AI_ENABLED_KEY not in self.config:
            self.config[GLOBAL_AI_ENABLED_KEY] = False
            changed = True
        if GLOBAL_AI_PROVIDER_KEY not in self.config:
            self.config[GLOBAL_AI_PROVIDER_KEY] = ""
            changed = True
        if GLOBAL_AI_FALLBACKS_KEY not in self.config:
            self.config[GLOBAL_AI_FALLBACKS_KEY] = []
            changed = True
        for key, default in (
            (GLOBAL_AI_TIMEOUT_KEY, AI_REVIEW_TOTAL_TIMEOUT_SECONDS),
            (GLOBAL_AI_CONFIRM_PROVIDER_KEY, ""),
            (GLOBAL_AI_IMAGES_KEY, False),
            (GLOBAL_AI_BLOCK_THRESHOLD_KEY, AI_REVIEW_DEFAULT_BLOCK_THRESHOLD),
            (GLOBAL_AI_ACTION_KEY, "record_only"),
            ("global_ai_reject_reply", "消息未通过 AI 内容审核，已撤回。"),
            ("global_ai_reject_at_member", True),
            ("global_message_reject_reply", "消息命中全局禁止关键词，已撤回。"),
            ("global_message_reject_at_member", True),
            (GLOBAL_IMAGE_KEYWORDS_KEY, ""),
            ("global_image_reject_reply", "图片文字命中全局禁止关键词，已撤回。"),
            ("global_image_reject_at_member", True),
            (GLOBAL_IMAGE_OCR_ENABLED_KEY, False),
            (GLOBAL_IMAGE_OCR_PROVIDER_KEY, ""),
            (GLOBAL_IMAGE_OCR_TIMEOUT_KEY, IMAGE_OCR_DEFAULT_TIMEOUT_SECONDS),
            (GLOBAL_IMAGE_OCR_MAX_IMAGES_KEY, IMAGE_OCR_DEFAULT_MAX_IMAGES),
            ("global_member_blacklist", ""),
            ("global_member_whitelist", ""),
            ("global_blacklist_reply", "成员命中群聊黑名单，消息已撤回。"),
            ("global_blacklist_at_member", True),
        ):
            if key not in self.config:
                self.config[key] = default
                changed = True
        normalized_fallbacks = normalize_provider_ids(
            self.config.get(GLOBAL_AI_FALLBACKS_KEY)
        )
        if normalized_fallbacks != self.config.get(GLOBAL_AI_FALLBACKS_KEY):
            self.config[GLOBAL_AI_FALLBACKS_KEY] = normalized_fallbacks
            changed = True
        for entry in legacy_ai_entries:
            for key in (
                "ai_review_enabled",
                "ai_review_provider_id",
                "ai_review_fallback_provider_id",
            ):
                if key in entry:
                    entry.pop(key, None)
                    changed = True
        primary_provider = str(self.config.get(GLOBAL_AI_PROVIDER_KEY) or "").strip()
        filtered_fallbacks = [
            provider_id
            for provider_id in normalize_provider_ids(
                self.config.get(GLOBAL_AI_FALLBACKS_KEY)
            )
            if provider_id != primary_provider
        ]
        if filtered_fallbacks != self.config.get(GLOBAL_AI_FALLBACKS_KEY):
            self.config[GLOBAL_AI_FALLBACKS_KEY] = filtered_fallbacks
            changed = True
        if not isinstance(entries, list):
            if changed:
                self.config.save_config()
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            template_key = str(entry.get("__template_key") or "").strip()
            if not template_key:
                entry["__template_key"] = GROUP_TEMPLATE_KEY
                if entry.get("template") == GROUP_TEMPLATE_KEY:
                    entry.pop("template")
                changed = True
            elif template_key != GROUP_TEMPLATE_KEY:
                self.logger.warning("保留未知的群审核配置模板：%s", template_key)

            if "reject_keywords" not in entry:
                entry["reject_keywords"] = str(
                    entry.pop("uid_reject_keywords", "") or ""
                )
                changed = True
            for key, default in (
                ("uid_check_enabled", True),
                ("uid_exists_auto_approve", False),
                ("approve_keywords", ""),
                ("condition_logic", "all"),
                (
                    "fallback_action",
                    "decline" if entry.get("uid_review_enabled") else "pending",
                ),
                ("fallback_human_verify_enabled", False),
                ("moderation_enabled", False),
                ("moderation_exempt_admins", True),
                ("member_blacklist", ""),
                ("member_whitelist", ""),
                ("blacklist_reply", "成员命中本群黑名单，消息已撤回。"),
                ("blacklist_at_member", True),
                ("message_reject_keywords", ""),
                ("message_reject_reply", "消息命中本群禁止关键词，已撤回。"),
                ("message_reject_at_member", True),
                ("image_keyword_review_enabled", False),
                ("image_reject_keywords", ""),
                ("image_reject_reply", "图片文字命中本群禁止关键词，已撤回。"),
                ("image_reject_at_member", True),
                ("image_spam_enabled", False),
                ("image_spam_count", 5),
                ("image_spam_window_seconds", 15),
                ("image_spam_group_min_members", 2),
                ("image_spam_recall_count", 5),
                ("image_spam_reply", "检测到连续发送图片或表情，相关消息已撤回。"),
                ("image_spam_at_member", True),
                ("repeat_review_enabled", False),
                ("repeat_count", 4),
                ("repeat_window_seconds", 30),
                ("repeat_mute_min_seconds", 60),
                ("repeat_mute_max_seconds", 600),
                ("repeat_reply", "检测到集中复读，已随机禁言一名参与者。"),
                ("repeat_at_member", True),
                ("bilibili_uids", ""),
                ("bilibili_dynamic_enabled", False),
                ("bilibili_live_enabled", False),
                ("keyword_replies", []),
            ):
                if key not in entry:
                    entry[key] = default
                    changed = True
        if changed:
            self.config.save_config()

    async def initialize(self) -> None:
        await self._load_state()
        self._web.register_routes()
        self._patch_qq_clients()
        if self._review_task is None or self._review_task.done():
            self._review_task = asyncio.create_task(
                self._uid_review_loop(),
                name="qqgroup-admin-uid-review",
            )
        if self._bilibili_task is None or self._bilibili_task.done():
            self._bilibili_task = asyncio.create_task(
                self._bilibili_loop(),
                name="qqgroup-admin-bilibili-push",
            )

    async def terminate(self) -> None:
        if self._review_task:
            self._review_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._review_task
            self._review_task = None
        if self._bilibili_task:
            self._bilibili_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._bilibili_task
            self._bilibili_task = None
        recall_tasks = tuple(self._recall_tasks)
        for task in recall_tasks:
            task.cancel()
        if recall_tasks:
            await asyncio.gather(*recall_tasks, return_exceptions=True)
        self._recall_tasks.clear()
        self._bilibili_logins.clear()
        self._keyword_reply_ready_at.clear()
        if self._violation_state_dirty:
            try:
                await self._save_state()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                self.logger.warning("退出时保存违规记录失败：%s", exc)
        for client, previous in self._patched_clients.items():
            handler = getattr(client, "on_interaction_create", None)
            if getattr(handler, "__qqgroup_admin_owner__", None) is self:
                if previous is None:
                    delattr(client, "on_interaction_create")
                else:
                    client.on_interaction_create = previous
        self._patched_clients.clear()

    async def _load_state(self) -> None:
        getter = getattr(self, "get_kv_data", None)
        value = await getter(STATE_KEY, {}) if getter else {}
        if not isinstance(value, dict):
            self.logger.warning("QQ群管理持久状态格式错误，已忽略")
            return
        self._uid_bindings = valid_state_dict(value.get("uid_bindings"))
        self._suspicious_members = valid_state_dict(value.get("suspicious_members"))
        records = value.get("violation_records")
        if isinstance(records, list):
            self._violation_records = [
                dict(item)
                for item in records[-2_000:]
                if isinstance(item, dict)
            ]
        bili = value.get("bilibili")
        if isinstance(bili, dict):
            self._bilibili_state = {
                "live": valid_state_dict(bili.get("live")),
                "dynamic": valid_state_dict(bili.get("dynamic")),
            }

    async def _save_state(self) -> None:
        setter = getattr(self, "put_kv_data", None)
        if setter is None:
            return
        async with self._state_lock:
            await setter(
                STATE_KEY,
                {
                    "uid_bindings": self._uid_bindings,
                    "suspicious_members": self._suspicious_members,
                    "violation_records": self._violation_records[-2_000:],
                    "bilibili": self._bilibili_state,
                },
            )
            self._violation_state_dirty = False

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        self._patch_qq_clients()

    def _context(self, event: AstrMessageEvent) -> tuple[Any, str, str]:
        raw = event.message_obj.raw_message
        group_openid = str(getattr(raw, "group_openid", "") or "")
        author = getattr(raw, "author", None)
        member_openid = str(getattr(author, "member_openid", "") or "")
        if not group_openid or not member_openid:
            raise ValueError("当前会话是 QQ 频道而不是 QQ 群聊")
        return raw, group_openid, member_openid

    def _client(self, event: AstrMessageEvent) -> Any:
        platform = self.context.get_platform_inst(event.get_platform_id())
        client = (
            platform.get_client()
            if platform and hasattr(platform, "get_client")
            else None
        )
        client = client or getattr(event, "bot", None)
        if client is None:
            raise RuntimeError("无法取得 AstrBot QQ 官方客户端")
        return client

    def _api(self, event: AstrMessageEvent) -> QQGroupAPI:
        return QQGroupAPI(self._client(event))

    @staticmethod
    def _mention(member_openid: str) -> str:
        return f"<qqbot-at-user id={quoteattr(member_openid)} />"

    async def _send_group_text(
        self,
        client: Any,
        group_openid: str,
        text: str,
        *,
        message_id: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {
            "group_openid": group_openid,
            "msg_type": 0,
            "content": text[:1000],
        }
        if message_id:
            kwargs["msg_id"] = message_id
        return await client.api.post_group_message(**kwargs)

    async def _send_group_markdown(
        self,
        client: Any,
        group_openid: str,
        text: str,
        *,
        message_id: str = "",
        keyboard: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "group_openid": group_openid,
            "msg_type": 2,
            "markdown": {"content": text[:4000]},
        }
        if keyboard:
            kwargs["keyboard"] = keyboard
        if message_id:
            kwargs["msg_id"] = message_id
        return await client.api.post_group_message(**kwargs)

    async def _send_group_notice(
        self,
        client: Any,
        group_openid: str,
        text: str,
        *,
        member_openid: str = "",
        message_id: str = "",
    ) -> Any:
        """Send a moderation notice without exposing raw QQ mention markup."""

        text = str(text or "").strip()
        if not text and not member_openid:
            return None
        if not member_openid:
            text = text.replace("{at_user}", "").strip()
            if not text:
                return None
            return await self._send_group_text(
                client, group_openid, text, message_id=message_id
            )
        mention = self._mention(member_openid)
        rendered = text.replace("{at_user}", mention)
        if rendered == text and mention not in rendered:
            rendered = f"{mention} {text}".strip()
        # QQ parses the official mention tag in Markdown.  A few accounts do
        # not have Markdown permission, so degrade to readable text on error.
        try:
            return await self._send_group_markdown(
                client,
                group_openid,
                rendered[:4000],
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001 - permission fallback
            self.logger.debug("带艾特提示发送失败，降级为普通文本：%s", exc)
            fallback = rendered.replace(mention, "").replace("{at_user}", "")
            fallback = re.sub(r"[ \t]{2,}", " ", fallback).strip()
            if not fallback:
                return None
            return await self._send_group_text(
                client,
                group_openid,
                fallback,
                message_id=message_id,
            )

    @staticmethod
    def _member_state_key(group_openid: str, member_openid: str) -> str:
        return f"{group_openid}:{member_openid}"

    @staticmethod
    def _request_identity(
        request: dict[str, Any],
        group_openid: str,
        member_openid: str,
    ) -> str:
        union_openid = str(request.get("union_openid") or "").strip()
        return (
            f"union:{union_openid}"
            if union_openid
            else f"member:{group_openid}:{member_openid}"
        )

    def _uid_binding_conflict(
        self,
        uid: str,
        identity: str,
    ) -> dict[str, Any] | None:
        binding = self._uid_bindings.get(uid)
        return binding if binding and binding.get("identity") != identity else None

    async def _bind_uid_identity(
        self,
        uid: str,
        request: dict[str, Any],
        group_openid: str,
        member_openid: str,
    ) -> None:
        identity = self._request_identity(request, group_openid, member_openid)
        current = self._uid_bindings.get(uid) or {}
        groups = list(dict.fromkeys([*(current.get("groups") or []), group_openid]))
        members = dict(current.get("members") or {})
        members[group_openid] = member_openid
        self._uid_bindings[uid] = {
            "uid": uid,
            "identity": identity,
            "union_openid": str(request.get("union_openid") or ""),
            "member_openid": member_openid,
            "members": members,
            "username": str(request.get("username") or ""),
            "groups": groups,
            "bound_at": current.get("bound_at") or int(time.time()),
            "last_seen_at": int(time.time()),
        }
        await self._save_state()

    def _uid_for_member(self, group_openid: str, member_openid: str) -> str:
        for uid, binding in self._uid_bindings.items():
            members = binding.get("members")
            if isinstance(members, dict) and members.get(group_openid) == member_openid:
                return uid
            if binding.get("member_openid") == member_openid and group_openid in (
                binding.get("groups") or []
            ):
                return uid
        return ""

    async def _record_uid_violation(
        self,
        uid: str,
        group_openid: str,
        member_openid: str,
        reason: str,
        *,
        content: str = "",
        message_id: str = "",
        action_member_openid: str = "",
        request: dict[str, Any] | None = None,
        action: str = "recall",
        ai_review: dict[str, Any] | None = None,
    ) -> None:
        binding = self._uid_bindings.get(uid)
        now = int(time.time())
        if binding and action != "record_only":
            binding["violation_count"] = int(binding.get("violation_count") or 0) + 1
            binding["last_violation_at"] = now
            binding["last_violation_group"] = group_openid
            binding["last_violation_member"] = member_openid
            binding["last_violation_reason"] = reason[:200]
            binding["last_violation_content"] = content[:1_000]
        request = request or {}
        entry = self._group_config(group_openid)
        self._violation_records.append(
            {
                "uid": uid,
                "username": str(
                    request.get("username")
                    or (binding or {}).get("username")
                    or ""
                )[:120],
                "identity": (binding or {}).get("identity", ""),
                "union_openid": str(
                    request.get("union_openid")
                    or (binding or {}).get("union_openid")
                    or ""
                )[:128],
                "member_openid": member_openid[:128],
                "action_member_openid": action_member_openid[:128],
                "group_openid": group_openid[:128],
                "group_name": str((entry or {}).get("group_name") or "")[:160],
                "created_at": now,
                "reason": reason[:200],
                "rule": reason[:200],
                "content": content[:1_000],
                "message_id": message_id[:256],
                "action": action[:32],
                "ai_provider": str((ai_review or {}).get("provider") or "")[:128],
                "ai_decision": str((ai_review or {}).get("decision") or "")[:16],
                "ai_confidence": (ai_review or {}).get("confidence"),
                "ai_reason": str((ai_review or {}).get("reason") or "")[:200],
                "ai_confirm_provider": str(
                    (ai_review or {}).get("confirm_provider") or ""
                )[:128],
                "ai_confirm_decision": str(
                    (ai_review or {}).get("confirm_decision") or ""
                )[:16],
                "ai_confirm_confidence": (ai_review or {}).get(
                    "confirm_confidence"
                ),
                "ai_confirm_reason": str(
                    (ai_review or {}).get("confirm_reason") or ""
                )[:200],
            }
        )
        self._violation_records = self._violation_records[-2_000:]
        self._violation_state_dirty = True
        now_monotonic = time.monotonic()
        if binding or now_monotonic - self._last_violation_state_save_at >= 5:
            await self._save_state()
            self._last_violation_state_save_at = now_monotonic

    async def _mark_suspicious(
        self,
        request: dict[str, Any],
        group_openid: str,
        member_openid: str,
        reason: str,
    ) -> None:
        key = self._member_state_key(group_openid, member_openid)
        self._suspicious_members[key] = {
            "group_openid": group_openid,
            "member_openid": member_openid,
            "union_openid": str(request.get("union_openid") or ""),
            "username": str(request.get("username") or ""),
            "reason": reason,
            "created_at": int(time.time()),
        }
        await self._save_state()

    async def _clear_suspicious(
        self,
        group_openid: str,
        member_openid: str,
    ) -> None:
        key = self._member_state_key(group_openid, member_openid)
        if self._suspicious_members.pop(key, None) is not None:
            await self._save_state()

    async def _send_verification_challenge(
        self,
        client: Any,
        group_openid: str,
        member_openid: str,
        *,
        message_id: str = "",
    ) -> None:
        left = 2 + secrets.randbelow(8)
        right = 2 + secrets.randbelow(8)
        answer = left + right
        options = [answer, answer - 2, answer - 1, answer + 1]
        secrets.SystemRandom().shuffle(options)
        previous = {
            key: value
            for key, value in self._verification_tokens.items()
            if value[1:3] == (group_openid, member_openid)
        }
        token = self._verification_token(group_openid, member_openid, answer)
        buttons = []
        for index, value in enumerate(options):
            buttons.append(
                {
                    "id": f"verify-{index}",
                    "render_data": {
                        "label": str(value),
                        "visited_label": str(value),
                        "style": 1,
                    },
                    "action": {
                        "type": 1,
                        "permission": {
                            "type": 0,
                            "specify_user_ids": [member_openid],
                        },
                        "data": f"qqgv:{token}:{value}",
                        "unsupport_tips": "请升级 QQ 后完成真人验证",
                    },
                }
            )
        try:
            await self._send_group_markdown(
                client,
                group_openid,
                (
                    f"# 真人验证\n{self._mention(member_openid)} 请计算 "
                    f"**{left} + {right}**，验证通过前发送的消息会被撤回。"
                ),
                message_id=message_id,
                keyboard={"content": {"rows": [{"buttons": buttons}]}},
            )
        except Exception:
            self._verification_tokens.pop(token, None)
            self._verification_tokens.update(previous)
            raise

    def _qq_platforms(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        return [
            platform
            for platform in (manager.get_insts() if manager else [])
            if platform.meta().name in QQ_PLATFORM_NAMES
        ]

    def _patch_qq_clients(self) -> None:
        for platform in self._qq_platforms():
            try:
                client = platform.get_client()
                existing = getattr(client, "on_interaction_create", None)
                owner = getattr(existing, "__qqgroup_admin_owner__", None)
                if owner is self:
                    continue
                if owner is not None:
                    existing = getattr(
                        existing,
                        "__qqgroup_admin_previous__",
                        existing,
                    )
                if hasattr(client, "intents"):
                    client.intents |= INTERACTION_INTENT

                async def interaction_handler(
                    interaction: Any,
                    bound_client: Any = client,
                    previous_handler: Any = existing,
                ) -> None:
                    handled = await self._handle_interaction(
                        bound_client,
                        interaction,
                    )
                    if not handled and previous_handler is not None:
                        await previous_handler(interaction)

                interaction_handler.__qqgroup_admin_owner__ = self
                interaction_handler.__qqgroup_admin_previous__ = existing
                client.on_interaction_create = interaction_handler
                self._patched_clients[client] = existing
            except Exception as exc:  # noqa: BLE001 - private botpy boundary
                self.logger.warning("安装 QQ 群管理按钮回调失败：%s", exc)

    def _cleanup_tokens(self) -> None:
        now = time.monotonic()
        self._approval_tokens = {
            token: data
            for token, data in self._approval_tokens.items()
            if data[0] > now
        }
        self._settings_tokens = {
            token: data
            for token, data in self._settings_tokens.items()
            if data[0] > now
        }
        self._verification_tokens = {
            token: data
            for token, data in self._verification_tokens.items()
            if data[0] > now
        }

    def _approval_token(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
    ) -> str:
        self._cleanup_tokens()
        token = secrets.token_urlsafe(12)
        self._approval_tokens[token] = (
            time.monotonic() + BUTTON_TOKEN_TTL,
            group_openid,
            member_openid,
            join_request_id,
        )
        return token

    def _settings_token(
        self,
        group_openid: str,
        platform_id: str,
        group_name: str,
    ) -> str:
        self._cleanup_tokens()
        token = secrets.token_urlsafe(12)
        self._settings_tokens[token] = (
            time.monotonic() + BUTTON_TOKEN_TTL,
            group_openid,
            platform_id,
            group_name,
        )
        return token

    def _verification_token(
        self,
        group_openid: str,
        member_openid: str,
        answer: int,
    ) -> str:
        self._cleanup_tokens()
        self._verification_tokens = {
            token: data
            for token, data in self._verification_tokens.items()
            if data[1:3] != (group_openid, member_openid)
        }
        token = secrets.token_urlsafe(12)
        self._verification_tokens[token] = (
            time.monotonic() + VERIFICATION_TOKEN_TTL,
            group_openid,
            member_openid,
            answer,
        )
        return token

    def _forget_request_tokens(
        self,
        group_openid: str,
        join_request_id: str,
    ) -> None:
        self._approval_tokens = {
            token: data
            for token, data in self._approval_tokens.items()
            if data[1] != group_openid or data[3] != join_request_id
        }

    async def _approve_request(
        self,
        api: QQGroupAPI,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        *,
        op: str,
        reject_reason: str = "",
    ) -> None:
        # ponytail: one global pacer is enough at QQ's 60 QPM approval ceiling.
        async with self._approval_lock:
            wait = 1.05 - (time.monotonic() - self._last_approval_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await api.approve_join_request(
                    group_openid,
                    member_openid,
                    op=op,
                    join_request_id=join_request_id,
                    reject_reason=reject_reason,
                )
            finally:
                self._last_approval_at = time.monotonic()
        self._forget_request_tokens(group_openid, join_request_id)

    async def _handle_interaction(self, client: Any, interaction: Any) -> bool:
        interaction_id = str(getattr(interaction, "id", "") or "")
        data = getattr(interaction, "data", None)
        resolved = getattr(data, "resolved", None)
        button_data = str(getattr(resolved, "button_data", "") or "")
        parts = button_data.split(":")
        group_openid = str(getattr(interaction, "group_openid", "") or "")
        prefix = parts[0] if parts else ""
        valid_action = (
            parts[2] in {"approve", "decline"}
            if prefix == "qqga" and len(parts) == 3
            else parts[2] in SETTINGS_ACTIONS
            if prefix == "qqgs" and len(parts) == 3
            else parts[2].isdigit()
            if prefix == "qqgv" and len(parts) == 3
            else False
        )
        if (
            getattr(interaction, "type", None) != 11
            or getattr(interaction, "chat_type", None) != 1
            or not interaction_id
            or not group_openid
            or len(parts) != 3
            or prefix not in {"qqga", "qqgs", "qqgv"}
            or not valid_action
        ):
            return False

        self._cleanup_tokens()
        clicker = str(getattr(interaction, "group_member_openid", "") or "")
        token_data = (
            self._approval_tokens.get(parts[1])
            if prefix == "qqga"
            else self._settings_tokens.get(parts[1])
            if prefix == "qqgs"
            else self._verification_tokens.get(parts[1])
        )
        response_code = 3 if token_data is None else 0
        if token_data is not None and (
            token_data[1] != group_openid
            or (prefix == "qqgv" and token_data[2] != clicker)
        ):
            response_code = 4
        try:
            await client.api.on_interaction_result(interaction_id, response_code)
        except Exception as exc:  # noqa: BLE001 - botpy raises transport errors
            self.logger.warning("回应 QQ 按钮互动事件失败：%s", exc)
        if response_code:
            return True

        try:
            if prefix == "qqga":
                _, _, member_openid, join_request_id = token_data
                entry = self._group_config(group_openid)
                reason = str((entry or {}).get("button_reject_reason") or "管理员拒绝")
                await self._approve_request(
                    QQGroupAPI(client),
                    group_openid,
                    member_openid,
                    join_request_id,
                    op=parts[2],
                    reject_reason=reason if parts[2] == "decline" else "",
                )
            elif prefix == "qqgs":
                panel = {
                    "home": self._send_settings_home,
                    "conditions": self._send_condition_settings,
                    "moderation": self._send_moderation_settings,
                    "keywords": self._send_keyword_settings,
                    "bilibili": self._send_bilibili_settings,
                }.get(parts[2])
                if panel is not None:
                    await panel(
                        client,
                        group_openid,
                        parts[1],
                        token_data[3],
                    )
                else:
                    await self._apply_settings_button(
                        client,
                        group_openid,
                        token_data[2],
                        parts[2],
                        token_data[3],
                    )
            else:
                self._verification_tokens.pop(parts[1], None)
                if int(parts[2]) != token_data[3]:
                    await self._send_verification_challenge(
                        client,
                        group_openid,
                        clicker,
                    )
                else:
                    await self._clear_suspicious(group_openid, clicker)
                    await self._send_group_notice(
                        client,
                        group_openid,
                        "真人验证已通过，可以正常发言。",
                        member_openid=clicker,
                    )
        except QQAPIError as exc:
            self.logger.warning("处理 QQ 群管理按钮失败：%s", exc)
            with suppress(Exception):
                await self._send_group_text(
                    client,
                    group_openid,
                    f"按钮操作失败：{self._plain_text(exc, 200)}",
                )
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            self.logger.warning("处理 QQ 群管理按钮失败：%s", exc)
            with suppress(Exception):
                await self._send_group_text(
                    client,
                    group_openid,
                    f"按钮操作失败：{self._plain_text(exc, 200)}",
                )
        return True

    def _target_member(self, event: AstrMessageEvent, value: str) -> str:
        raw, _, _ = self._context(event)
        if not value.startswith(("@", "<@")):
            return parse_openids(value, max_items=1)[0]

        targets = []
        for mention in getattr(raw, "mentions", None) or []:
            if isinstance(mention, dict):
                member_openid = mention.get("member_openid")
                is_bot = mention.get("is_you") is True
            else:
                member_openid = getattr(mention, "member_openid", None)
                is_bot = getattr(mention, "is_you", False) is True
            if member_openid and not is_bot:
                targets.append(str(member_openid))
        targets = list(dict.fromkeys(targets))
        if len(targets) != 1:
            raise ValueError("使用 @成员 时，消息中必须恰好提及一名非机器人用户")
        return targets[0]

    @staticmethod
    def _confirm(value: str) -> None:
        if value != "确认":
            raise ValueError("危险操作需要在命令末尾输入“确认”")

    @staticmethod
    def _recall_count(value: str) -> int:
        value = str(value or "").strip()
        if not value.isdigit():
            raise ValueError("撤回数量必须是整数")
        count = int(value)
        if not 1 <= count <= RECENT_RECALL_LIMIT:
            raise ValueError(f"每次最多撤回 {RECENT_RECALL_LIMIT} 条消息")
        return count

    @staticmethod
    def _value(value: Any) -> str:
        return str(value) if value not in {None, ""} else "-"

    @staticmethod
    def _list(value: Any) -> str:
        return ", ".join(str(item) for item in (value or [])) or "-"

    @staticmethod
    def _strategy_id(strategy: dict[str, Any]) -> str:
        strategy_id = str(strategy.get("strategy_id") or "")
        if not strategy_id:
            raise RuntimeError("QQ API 未返回自动审核策略 ID")
        return strategy_id

    def _group_config(
        self,
        group_openid: str,
        *,
        required: bool = False,
    ) -> dict[str, Any] | None:
        entries = self.config.get("auto_review_groups") or []
        if not isinstance(entries, list):
            raise TypeError("WebUI 自动审核配置格式错误")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("group_openid") or "").strip() == group_openid
        ]
        if len(matches) > 1:
            raise ValueError("WebUI 中当前群存在重复的自动审核配置")
        if required and not matches:
            raise ValueError("当前群尚未绑定，请发送 /审核设置 并点击绑定")
        return matches[0] if matches else None

    async def _bind_group(
        self,
        client: Any,
        group_openid: str,
        platform_id: str,
        group_name: str = "",
    ) -> dict[str, Any]:
        if not group_name:
            data = await QQGroupAPI(client).get_group_info(group_openid)
            group_name = str(data.get("group_name") or "").strip()
        if not group_name:
            raise RuntimeError("QQ API 未返回群名称，无法完成绑定")
        if any(ord(char) < 32 for char in group_name):
            raise ValueError("群名称包含非法控制字符")

        entry = self._group_config(group_openid)
        if entry is None:
            entries = self.config.get("auto_review_groups") or []
            if not isinstance(entries, list):
                raise TypeError("WebUI 自动审核配置格式错误")
            entry = {
                "__template_key": GROUP_TEMPLATE_KEY,
                "group_name": group_name,
                "group_openid": group_openid,
                "enabled": False,
                "whitelist_qq_numbers": "",
                "scan_pending": True,
                "uid_review_enabled": False,
                "uid_check_enabled": True,
                "uid_exists_auto_approve": False,
                "approve_keywords": "",
                "reject_keywords": "",
                "condition_logic": "all",
                "fallback_action": "pending",
                "fallback_human_verify_enabled": False,
                "button_reject_reason": "管理员拒绝",
                "moderation_enabled": False,
                "moderation_exempt_admins": True,
                "member_blacklist": "",
                "member_whitelist": "",
                "blacklist_reply": "成员命中本群黑名单，消息已撤回。",
                "blacklist_at_member": True,
                "message_reject_keywords": "",
                "message_reject_reply": "消息命中本群禁止关键词，已撤回。",
                "message_reject_at_member": True,
                "image_keyword_review_enabled": False,
                "image_reject_keywords": "",
                "image_reject_reply": "图片文字命中本群禁止关键词，已撤回。",
                "image_reject_at_member": True,
                "image_spam_enabled": False,
                "image_spam_count": 5,
                "image_spam_window_seconds": 15,
                "image_spam_group_min_members": 2,
                "image_spam_recall_count": 5,
                "image_spam_reply": "检测到连续发送图片或表情，相关消息已撤回。",
                "image_spam_at_member": True,
                "repeat_review_enabled": False,
                "repeat_count": 4,
                "repeat_window_seconds": 30,
                "repeat_mute_min_seconds": 60,
                "repeat_mute_max_seconds": 600,
                "repeat_reply": "检测到集中复读，已随机禁言一名参与者。",
                "repeat_at_member": True,
                "bilibili_uids": "",
                "bilibili_dynamic_enabled": False,
                "bilibili_live_enabled": False,
                "keyword_replies": [],
                "platform_id": platform_id,
                "managed_strategy_id": "",
                "applied_whitelist": "",
            }
            entries.append(entry)
            self.config["auto_review_groups"] = entries
        else:
            entry["group_name"] = group_name
            entry["platform_id"] = platform_id
        self.config.save_config()
        return entry

    def _condition_settings(self, entry: dict[str, Any] | None) -> dict[str, Any]:
        if entry is None:
            return {"enabled": False}
        logic = str(entry.get("condition_logic") or "all")
        fallback = str(entry.get("fallback_action") or "pending")
        if logic not in CONDITION_LOGICS:
            raise ValueError("条件组合只能是 all 或 any")
        if fallback not in FALLBACK_ACTIONS:
            raise ValueError("兜底动作只能是 pending、decline 或 approve")
        uid_check_enabled = bool(entry.get("uid_check_enabled", True))
        return {
            "enabled": bool(entry.get("uid_review_enabled", False)),
            "uid_check_enabled": uid_check_enabled,
            "uid_exists_auto_approve": uid_check_enabled
            and bool(entry.get("uid_exists_auto_approve", False)),
            "global_reject_keywords": parse_keywords(
                str(self.config.get("global_reject_keywords") or "")
            ),
            "approve_keywords": parse_keywords(
                str(entry.get("approve_keywords") or "")
            ),
            "reject_keywords": parse_keywords(
                str(
                    entry.get("reject_keywords")
                    or entry.get("uid_reject_keywords")
                    or ""
                )
            ),
            "condition_logic": logic,
            "fallback_action": fallback,
            "fallback_human_verify_enabled": bool(
                entry.get("fallback_human_verify_enabled", False)
            ),
        }

    @staticmethod
    def _bounded_int(
        value: Any,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return min(maximum, max(minimum, value))

    def _moderation_settings(self, entry: dict[str, Any] | None) -> dict[str, Any]:
        entry = entry or {}
        def configured_text(source: dict[str, Any], key: str, default: str) -> str:
            value = source.get(key, default)
            return default if value is None else str(value)

        minimum = self._bounded_int(
            entry.get("repeat_mute_min_seconds"), 60, 1, 2_592_000
        )
        maximum = self._bounded_int(
            entry.get("repeat_mute_max_seconds"), 600, minimum, 2_592_000
        )
        return {
            "enabled": bool(entry.get("moderation_enabled", False)),
            "exempt_admins": bool(entry.get("moderation_exempt_admins", True)),
            "global_member_blacklist": parse_member_list(
                self.config.get("global_member_blacklist", "")
            ),
            "global_member_whitelist": parse_member_list(
                self.config.get("global_member_whitelist", "")
            ),
            "global_blacklist_reply": str(
                configured_text(
                    self.config,
                    "global_blacklist_reply",
                    "成员命中群聊黑名单，消息已撤回。",
                )
            ),
            "global_blacklist_at": bool(
                self.config.get("global_blacklist_at_member", True)
            ),
            "member_blacklist": parse_member_list(entry.get("member_blacklist", "")),
            "member_whitelist": parse_member_list(entry.get("member_whitelist", "")),
            "blacklist_reply": str(
                configured_text(
                    entry,
                    "blacklist_reply",
                    "成员命中本群黑名单，消息已撤回。",
                )
            ),
            "blacklist_at": bool(entry.get("blacklist_at_member", True)),
            "global_keywords": parse_keywords(
                str(self.config.get("global_message_reject_keywords") or "")
            ),
            "global_keyword_reply": str(
                configured_text(
                    self.config,
                    "global_message_reject_reply",
                    "消息命中全局禁止关键词，已撤回。",
                )
            ),
            "global_keyword_at": bool(
                self.config.get("global_message_reject_at_member", True)
            ),
            "keywords": parse_keywords(str(entry.get("message_reject_keywords") or "")),
            "keyword_reply": str(
                configured_text(
                    entry,
                    "message_reject_reply",
                    "消息命中本群禁止关键词，已撤回。",
                )
            ),
            "keyword_at": bool(entry.get("message_reject_at_member", True)),
            "ai_enabled": bool(self.config.get(GLOBAL_AI_ENABLED_KEY, False)),
            "ai_provider_id": str(
                self.config.get(GLOBAL_AI_PROVIDER_KEY) or ""
            ).strip(),
            "ai_fallback_provider_ids": normalize_provider_ids(
                self.config.get(GLOBAL_AI_FALLBACKS_KEY)
            ),
            "ai_confirm_provider_id": str(
                self.config.get(GLOBAL_AI_CONFIRM_PROVIDER_KEY) or ""
            ).strip(),
            "ai_timeout": self._bounded_int(
                self.config.get(GLOBAL_AI_TIMEOUT_KEY),
                AI_REVIEW_TOTAL_TIMEOUT_SECONDS,
                5,
                120,
            ),
            "ai_images_enabled": bool(self.config.get(GLOBAL_AI_IMAGES_KEY, False)),
            "ai_block_threshold": self._bounded_int(
                self.config.get(GLOBAL_AI_BLOCK_THRESHOLD_KEY),
                AI_REVIEW_DEFAULT_BLOCK_THRESHOLD,
                50,
                100,
            ),
            "ai_action": (
                str(self.config.get(GLOBAL_AI_ACTION_KEY) or "record_only")
                if str(self.config.get(GLOBAL_AI_ACTION_KEY) or "record_only")
                in AI_REVIEW_ACTIONS
                else "record_only"
            ),
            "ai_reply": str(
                configured_text(
                    self.config,
                    "global_ai_reject_reply",
                    "消息未通过 AI 内容审核，已撤回。",
                )
            ),
            "ai_at": bool(self.config.get("global_ai_reject_at_member", True)),
            "global_image_keywords": parse_keywords(
                str(self.config.get(GLOBAL_IMAGE_KEYWORDS_KEY) or "")
            ),
            "global_image_reply": str(
                configured_text(
                    self.config,
                    "global_image_reject_reply",
                    "图片文字命中全局禁止关键词，已撤回。",
                )
            ),
            "global_image_at": bool(
                self.config.get("global_image_reject_at_member", True)
            ),
            "image_keyword_enabled": bool(
                entry.get("image_keyword_review_enabled", False)
            ),
            "image_keywords": parse_keywords(
                str(entry.get("image_reject_keywords") or "")
            ),
            "image_keyword_reply": str(
                configured_text(
                    entry,
                    "image_reject_reply",
                    "图片文字命中本群禁止关键词，已撤回。",
                )
            ),
            "image_keyword_at": bool(entry.get("image_reject_at_member", True)),
            "image_ocr_enabled": bool(
                self.config.get(GLOBAL_IMAGE_OCR_ENABLED_KEY, False)
            ),
            "image_ocr_provider_id": str(
                self.config.get(GLOBAL_IMAGE_OCR_PROVIDER_KEY) or ""
            ).strip(),
            "image_ocr_timeout": self._bounded_int(
                self.config.get(GLOBAL_IMAGE_OCR_TIMEOUT_KEY),
                IMAGE_OCR_DEFAULT_TIMEOUT_SECONDS,
                2,
                30,
            ),
            "image_ocr_max_images": self._bounded_int(
                self.config.get(GLOBAL_IMAGE_OCR_MAX_IMAGES_KEY),
                IMAGE_OCR_DEFAULT_MAX_IMAGES,
                1,
                3,
            ),
            "image_enabled": bool(entry.get("image_spam_enabled", False)),
            "image_count": self._bounded_int(entry.get("image_spam_count"), 5, 2, 20),
            "image_window": self._bounded_int(
                entry.get("image_spam_window_seconds"), 15, 3, 120
            ),
            "image_group_min_members": self._bounded_int(
                entry.get("image_spam_group_min_members"), 2, 2, 10
            ),
            "image_recall_count": self._bounded_int(
                entry.get("image_spam_recall_count"), 5, 1, 50
            ),
            "image_spam_reply": str(
                configured_text(
                    entry,
                    "image_spam_reply",
                    "检测到连续发送图片或表情，相关消息已撤回。",
                )
            ),
            "image_spam_at": bool(entry.get("image_spam_at_member", True)),
            "repeat_enabled": bool(entry.get("repeat_review_enabled", False)),
            "repeat_count": self._bounded_int(entry.get("repeat_count"), 4, 3, 20),
            "repeat_window": self._bounded_int(
                entry.get("repeat_window_seconds"), 30, 5, 120
            ),
            "repeat_mute_min": minimum,
            "repeat_mute_max": maximum,
            "repeat_reply": str(
                configured_text(
                    entry,
                    "repeat_reply",
                    "检测到集中复读，已随机禁言一名参与者。",
                )
            ),
            "repeat_at": bool(entry.get("repeat_at_member", True)),
        }

    @staticmethod
    def _bilibili_uids(entry: dict[str, Any]) -> list[str]:
        return parse_bilibili_uids(str(entry.get("bilibili_uids") or ""))

    @staticmethod
    def _bilibili_dynamic_type(value: Any) -> str:
        return {
            "DYNAMIC_TYPE_AV": "视频",
            "DYNAMIC_TYPE_DRAW": "图文",
            "DYNAMIC_TYPE_WORD": "文字",
            "DYNAMIC_TYPE_FORWARD": "转发",
            "DYNAMIC_TYPE_ARTICLE": "专栏",
            "DYNAMIC_TYPE_LIVE_RCMD": "直播",
            "DYNAMIC_TYPE_UGC_SEASON": "合集",
            "DYNAMIC_TYPE_PGC": "番剧",
        }.get(str(value or "").upper(), "动态")

    @staticmethod
    def _bilibili_markdown_image(value: Any) -> str:
        url = str(value or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("https://", "http://")) or any(
            char.isspace() or char in "()" for char in url
        ):
            return ""
        return f"![封面 #300px #169px]({url})"

    @staticmethod
    def _markdown_fallback_text(value: str) -> str:
        text = re.sub(
            r"!\[[^\]]*\]\(https?://[^)]+\)\s*",
            "",
            str(value or ""),
        )
        text = re.sub(
            r"\[([^\]]+)\]\((https?://[^)]+)\)",
            r"\1：\2",
            text,
        )
        text = re.sub(r"(?m)^(?:#{1,6}\s+|>\s*)", "", text)
        return text.replace("**", "").replace("\\", "").strip()

    def _ensure_native_mode(self, group_openid: str) -> None:
        entry = self._group_config(group_openid)
        if self._condition_settings(entry)["enabled"]:
            raise ValueError("当前群已启用条件审核，不能同时使用 QQ 号码白名单策略")

    def _platform_clients(self) -> dict[str, Any]:
        clients = {}
        for platform in self._qq_platforms():
            platform_id = str(getattr(platform.meta(), "id", "") or "")
            if platform_id:
                clients[platform_id] = platform.get_client()
        return clients

    def _uid_review_entries(self) -> list[tuple[str, str, dict[str, Any]]]:
        entries = self.config.get("auto_review_groups") or []
        if not isinstance(entries, list):
            self.logger.warning("WebUI 自动审核配置格式错误")
            return []

        result = []
        seen = set()
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or bool(entry.get("enabled", False))
                or bool(entry.get("managed_strategy_id"))
            ):
                continue
            try:
                settings = self._condition_settings(entry)
            except ValueError as exc:
                self.logger.warning("跳过无效的条件审核配置：%s", exc)
                continue
            group_openid = str(entry.get("group_openid") or "").strip()
            platform_id = str(entry.get("platform_id") or "").strip()
            key = (platform_id, group_openid)
            if not settings["enabled"] or not all(key) or key in seen:
                continue
            seen.add(key)
            result.append((platform_id, group_openid, settings))
        return result

    def _review_interval(self) -> int:
        try:
            value = int(self.config.get("uid_review_interval_seconds", 60))
        except (TypeError, ValueError):
            return 60
        return min(600, max(15, value))

    async def _poll_uid_group(
        self,
        client: Any,
        platform_id: str,
        group_openid: str,
        settings: dict[str, Any],
    ) -> None:
        api = QQGroupAPI(client)
        key = (platform_id, group_openid)
        cursor = self._poll_cursors.get(key, "")
        try:
            data = await api.list_join_requests(
                group_openid,
                limit=100,
                cursor=cursor,
            )
        except QQAPIError:
            if cursor:
                self._poll_cursors.pop(key, None)
            raise
        self._poll_cursors[key] = str(data.get("next_cursor") or "")

        for request in data.get("list") or []:
            if (
                not isinstance(request, dict)
                or request.get("apply_source") != "self_apply"
            ):
                continue
            member_openid = str(request.get("member_openid") or "")
            join_request_id = str(request.get("join_request_id") or "")
            if not member_openid or not join_request_id:
                continue
            verified_uid: str | None = None
            approved_by_conditions = False
            fallback_approved = False

            text = verification_text(request)
            global_keyword = matched_keyword(
                text, settings.get("global_reject_keywords", [])
            )
            keyword = matched_keyword(text, settings["reject_keywords"])
            if global_keyword:
                op, reason = "decline", "验证消息包含全局拒绝关键词"
            elif keyword:
                op, reason = "decline", "验证消息包含拒绝关键词"
            else:
                checks = []
                uid_direct = bool(settings.get("uid_exists_auto_approve", False))
                uid_direct_passed = False
                binding_conflict = False
                failure_reason = "未满足自动审核条件"
                approve_keywords = settings["approve_keywords"]
                if approve_keywords:
                    keyword_ok = bool(matched_keyword(text, approve_keywords))
                    checks.append(keyword_ok)
                    if not keyword_ok:
                        failure_reason = "验证消息未包含通过关键词"

                logic = settings["condition_logic"]
                uid_needed = bool(settings["uid_check_enabled"])
                if (logic == "all" and False in checks and not uid_direct) or (
                    logic == "any" and True in checks
                ):
                    uid_needed = False

                if uid_needed:
                    uid = parse_request_bilibili_uid(request)
                    if uid is None:
                        checks.append(False)
                        failure_reason = "未提供有效的 B 站 UID"
                    else:
                        identity = self._request_identity(
                            request,
                            group_openid,
                            member_openid,
                        )
                        conflict = self._uid_binding_conflict(uid, identity)
                        if conflict is not None:
                            checks.append(False)
                            binding_conflict = True
                            failure_reason = "该 B 站 UID 已绑定其他 QQ 用户"
                            exists = False
                        else:
                            exists = None
                        if exists is None:
                            if time.monotonic() < self._bilibili_retry_at:
                                continue
                            try:
                                exists = await bilibili_uid_exists(uid)
                            except BilibiliLookupError as exc:
                                self._bilibili_retry_at = time.monotonic() + max(
                                    60,
                                    self._review_interval(),
                                )
                                self.logger.warning(
                                    "B 站 UID 查询暂不可用，本轮保留待审申请：%s",
                                    exc,
                                )
                                continue
                            checks.append(exists)
                        if exists and uid_direct:
                            uid_direct_passed = True
                            verified_uid = uid
                        elif exists:
                            verified_uid = uid
                        elif not exists:
                            if not binding_conflict:
                                failure_reason = "B 站 UID 不存在"

                passed = uid_direct_passed or (
                    bool(checks) and (all(checks) if logic == "all" else any(checks))
                )
                if binding_conflict:
                    op, reason = "decline", failure_reason
                elif passed:
                    op, reason = "approve", ""
                    approved_by_conditions = True
                else:
                    op = settings["fallback_action"]
                    if op == "pending":
                        continue
                    reason = failure_reason if op == "decline" else ""
                    fallback_approved = op == "approve"

            try:
                await self._approve_request(
                    api,
                    group_openid,
                    member_openid,
                    join_request_id,
                    op=op,
                    reject_reason=reason,
                )
            except QQAPIError as exc:
                self.logger.warning(
                    "自动处理 QQ 入群申请失败：group=%s request=%s error=%s",
                    group_openid,
                    join_request_id,
                    exc,
                )
                if exc.status == 429:
                    return
                continue
            self.logger.info(
                "已自动%s QQ 入群申请：group=%s request=%s",
                "同意" if op == "approve" else "拒绝",
                group_openid,
                join_request_id,
            )
            if op == "approve" and verified_uid and approved_by_conditions:
                try:
                    await self._bind_uid_identity(
                        verified_uid,
                        request,
                        group_openid,
                        member_openid,
                    )
                except Exception:
                    self.logger.exception("保存 UID 身份绑定失败")
            if (
                op == "approve"
                and fallback_approved
                and settings.get("fallback_human_verify_enabled")
            ):
                try:
                    await self._mark_suspicious(
                        request,
                        group_openid,
                        member_openid,
                        "入群条件未通过，由兜底规则同意",
                    )
                    await self._send_verification_challenge(
                        client,
                        group_openid,
                        member_openid,
                    )
                except Exception as exc:  # noqa: BLE001 - retry on first message
                    self.logger.warning(
                        "发送入群真人验证失败，将在用户发言时重试：%s", exc
                    )

    async def _log_poll_failure(
        self,
        client: Any,
        platform_id: str,
        group_openid: str,
        exc: Exception,
    ) -> None:
        entry = self._group_config(group_openid)
        group_name = str((entry or {}).get("group_name") or "-")
        key = (platform_id, group_openid)
        role = self._permission_diagnostics.get(key, "-")
        if (
            isinstance(exc, QQAPIError)
            and exc.err_code in GROUP_PERMISSION_ERROR_CODES
            and role == "-"
        ):
            try:
                state = await QQGroupAPI(client).get_bot_state(group_openid)
                role = str(state.get("member_role") or "unknown")
            except (QQAPIError, AttributeError, TypeError, ValueError) as state_exc:
                role = f"查询失败：{state_exc}"
            self._permission_diagnostics[key] = role
        self.logger.warning(
            "轮询 QQ 入群申请失败：group_name=%s group=%s platform=%s "
            "bot_role=%s error=%s",
            group_name,
            group_openid,
            platform_id,
            role,
            exc,
        )

    async def _uid_review_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                clients = self._platform_clients()
                for platform_id, group_openid, settings in self._uid_review_entries():
                    client = clients.get(platform_id)
                    if client is None:
                        continue
                    try:
                        await self._poll_uid_group(
                            client,
                            platform_id,
                            group_openid,
                            settings,
                        )
                    except (
                        QQAPIError,
                        AttributeError,
                        TypeError,
                        ValueError,
                        RuntimeError,
                    ) as exc:
                        await self._log_poll_failure(
                            client,
                            platform_id,
                            group_openid,
                            exc,
                        )
                    await asyncio.sleep(2.1)
            except Exception as exc:  # noqa: BLE001 - keep the background task alive
                self.logger.warning("QQ 入群审核后台任务本轮失败：%s", exc)
            await asyncio.sleep(self._review_interval())

    def _bilibili_subscriptions(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        entries = self.config.get("auto_review_groups") or []
        if not isinstance(entries, list):
            return result
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            group_openid = str(entry.get("group_openid") or "").strip()
            platform_id = str(entry.get("platform_id") or "").strip()
            if not group_openid or not platform_id:
                continue
            dynamic = bool(entry.get("bilibili_dynamic_enabled", False))
            live = bool(entry.get("bilibili_live_enabled", False))
            if not dynamic and not live:
                continue
            try:
                uids = self._bilibili_uids(entry)
            except ValueError as exc:
                self.logger.warning(
                    "跳过无效 B 站订阅配置：group=%s error=%s", group_openid, exc
                )
                continue
            for uid in uids:
                result.setdefault(uid, []).append(
                    {
                        "group_openid": group_openid,
                        "platform_id": platform_id,
                        "dynamic": dynamic,
                        "live": live,
                    }
                )
        return result

    async def _push_bilibili_message(
        self,
        targets: list[dict[str, Any]],
        text: str,
        kind: str,
    ) -> bool:
        clients = self._platform_clients()
        success = True
        for target in targets:
            if not target.get(kind):
                continue
            client = clients.get(str(target.get("platform_id") or ""))
            if client is None:
                success = False
                continue
            try:
                await self._send_group_markdown(
                    client,
                    str(target["group_openid"]),
                    text,
                )
            except Exception as exc:  # noqa: BLE001 - proactive QQ boundary
                try:
                    await self._send_group_text(
                        client,
                        str(target["group_openid"]),
                        self._markdown_fallback_text(text),
                    )
                except Exception as fallback_exc:  # noqa: BLE001 - QQ boundary
                    success = False
                    self.logger.warning(
                        "发送 B 站推送失败：group=%s markdown=%s text=%s",
                        target.get("group_openid"),
                        exc,
                        fallback_exc,
                    )
        return success

    async def _poll_bilibili_live(
        self,
        subscriptions: dict[str, list[dict[str, Any]]],
    ) -> bool:
        changed = False
        uids = [
            uid
            for uid, targets in subscriptions.items()
            if any(target.get("live") for target in targets)
        ]
        for start in range(0, len(uids), 100):
            statuses = await asyncio.to_thread(
                fetch_live_statuses, uids[start : start + 100]
            )
            for uid, current in statuses.items():
                previous = self._bilibili_state["live"].get(uid)
                transition = live_transition(previous, current)
                current_state = {
                    key: current.get(key)
                    for key in ("live_status", "live_time", "room_id", "uname", "title")
                }
                if transition is None:
                    delivered = True
                else:
                    name = self._markdown_text(current.get("uname") or f"UID {uid}")
                    title = self._markdown_text(
                        current.get("title") or "未设置标题", 300
                    )
                    room_id = str(current.get("room_id") or "").strip()
                    cover = self._bilibili_markdown_image(
                        current.get("user_cover")
                        or current.get("keyframe")
                        or current.get("cover")
                    )
                    if transition == "start":
                        sections = ["## 🔴 正在直播", f"**{name}** · 直播中"]
                        if cover:
                            sections.append(cover)
                        sections.extend(
                            [
                                f"**{title}**",
                                f"[进入直播间 ↗](https://live.bilibili.com/{room_id})",
                            ]
                        )
                        text = "\n\n".join(sections)
                    else:
                        text = "\n\n".join(
                            [
                                "## ⚪ 直播结束",
                                f"**{name}**",
                                f"**{title}**",
                                "本场直播已结束。",
                            ]
                        )
                    delivered = await self._push_bilibili_message(
                        subscriptions.get(uid, []), text, "live"
                    )
                if delivered and previous != current_state:
                    self._bilibili_state["live"][uid] = current_state
                    changed = True
        return changed

    async def _poll_bilibili_dynamics(
        self,
        subscriptions: dict[str, list[dict[str, Any]]],
    ) -> bool:
        cookie = str(self.config.get("bilibili_cookie") or "").strip()
        if not cookie:
            if time.monotonic() >= self._bilibili_push_warning_at:
                self.logger.warning("已启用 B 站动态推送，但尚未配置 B 站 Cookie")
                self._bilibili_push_warning_at = time.monotonic() + 600
            return False
        keys = await asyncio.to_thread(fetch_wbi_keys, cookie)
        changed = False
        now = time.monotonic()
        for uid, targets in subscriptions.items():
            if not any(target.get("dynamic") for target in targets):
                continue
            if now < self._bilibili_dynamic_retry_at.get(uid, 0):
                continue
            try:
                payload = await asyncio.to_thread(
                    fetch_space_dynamics,
                    uid,
                    cookie,
                    wbi_keys=keys,
                )
                items = parse_dynamic_items(payload)
            except (BilibiliAPIError, BilibiliConfigError, ValueError) as exc:
                self._bilibili_dynamic_retry_at[uid] = now + 600
                self.logger.warning("B 站动态轮询失败：uid=%s error=%s", uid, exc)
                continue
            state = self._bilibili_state["dynamic"].get(uid)
            if state is None:
                self._bilibili_state["dynamic"][uid] = {
                    "seen": [item["id"] for item in items[:100]],
                    "max_pub_ts": max(
                        (int(item.get("pub_ts") or 0) for item in items),
                        default=0,
                    ),
                }
                changed = True
                continue
            seen = {str(item) for item in state.get("seen") or []}
            baseline = self._bounded_int(state.get("max_pub_ts"), 0, 0, 4_000_000_000)
            new_items = sorted(
                (
                    item
                    for item in items
                    if item["id"] not in seen
                    and int(item.get("pub_ts") or 0) >= baseline
                ),
                key=lambda item: (int(item.get("pub_ts") or 0), item["id"]),
            )[:10]
            delivered_items = []
            for item in new_items:
                name = self._markdown_text(item.get("author") or f"UID {uid}")
                raw_title = str(item.get("title") or "").strip()
                raw_summary = str(item.get("text") or "").strip()
                title = (
                    self._markdown_text(raw_title, 300)
                    if raw_title not in {"-", "新动态", "发布了新动态"}
                    else ""
                )
                summary = (
                    self._markdown_text(raw_summary, 500)
                    if raw_summary not in {"", "-", raw_title}
                    else ""
                )
                kind = self._bilibili_dynamic_type(item.get("type"))
                pub_ts = self._bounded_int(
                    item.get("pub_ts"), 0, 0, 4_000_000_000
                )
                meta = f"**{name}** · {kind}"
                if pub_ts:
                    meta += time.strftime(" · %m-%d %H:%M", time.localtime(pub_ts))
                if not title and not summary:
                    title = "发布了新动态"
                    summary = "暂无文字说明，点击下方查看完整动态。"
                cover = self._bilibili_markdown_image(item.get("cover"))
                sections = ["## 🔔 B站动态", meta]
                if cover:
                    sections.append(cover)
                sections.append(f"**{title or '发布了新动态'}**")
                sections.append(f"> {summary or '暂无文字说明，点击下方查看完整动态。'}")
                sections.append(f"[查看动态 ↗]({item['url']})")
                text = "\n\n".join(sections)
                if not await self._push_bilibili_message(targets, text, "dynamic"):
                    break
                delivered_items.append(item)
            new_seen = list(
                dict.fromkeys([item["id"] for item in delivered_items] + list(seen))
            )[:100]
            new_max = max(
                [baseline, *(int(item.get("pub_ts") or 0) for item in delivered_items)]
            )
            if state.get("seen") != new_seen or state.get("max_pub_ts") != new_max:
                state["seen"] = new_seen
                state["max_pub_ts"] = new_max
                changed = True
            await asyncio.sleep(1)
        return changed

    async def _bilibili_loop(self) -> None:
        await asyncio.sleep(10)
        next_dynamic_at = 0.0
        while True:
            subscriptions = self._bilibili_subscriptions()
            changed = False
            now = time.monotonic()
            live_uids = {
                uid
                for uid, targets in subscriptions.items()
                if any(target.get("live") for target in targets)
            }
            dynamic_uids = {
                uid
                for uid, targets in subscriptions.items()
                if any(target.get("dynamic") for target in targets)
            }
            for uid in set(self._bilibili_state["live"]) - live_uids:
                self._bilibili_state["live"].pop(uid, None)
                changed = True
            for uid in set(self._bilibili_state["dynamic"]) - dynamic_uids:
                self._bilibili_state["dynamic"].pop(uid, None)
                self._bilibili_dynamic_retry_at.pop(uid, None)
                changed = True
            if live_uids and now >= self._bilibili_live_retry_at:
                try:
                    changed |= await self._poll_bilibili_live(subscriptions)
                except (BilibiliAPIError, BilibiliConfigError, ValueError) as exc:
                    self._bilibili_live_retry_at = now + 600
                    if now >= self._bilibili_push_warning_at:
                        self.logger.warning("B 站直播轮询失败，10 分钟后重试：%s", exc)
                        self._bilibili_push_warning_at = now + 600
                except Exception as exc:  # noqa: BLE001 - keep push loop alive
                    self._bilibili_live_retry_at = now + 60
                    self.logger.warning("B 站直播后台任务本轮失败：%s", exc)
            if dynamic_uids and now >= next_dynamic_at:
                try:
                    changed |= await self._poll_bilibili_dynamics(subscriptions)
                    next_dynamic_at = now + self._bounded_int(
                        self.config.get("bilibili_dynamic_interval_seconds"),
                        180,
                        60,
                        3600,
                    )
                except (BilibiliAPIError, BilibiliConfigError, ValueError) as exc:
                    next_dynamic_at = now + 600
                    if now >= self._bilibili_push_warning_at:
                        self.logger.warning("B 站动态轮询失败，10 分钟后重试：%s", exc)
                        self._bilibili_push_warning_at = now + 600
                except Exception as exc:  # noqa: BLE001 - keep push loop alive
                    next_dynamic_at = now + 60
                    self.logger.warning("B 站动态后台任务本轮失败：%s", exc)
            if changed:
                try:
                    await self._save_state()
                except Exception as exc:  # noqa: BLE001 - keep push loop alive
                    self.logger.warning("保存 B 站推送状态失败，下轮继续：%s", exc)
            await asyncio.sleep(
                self._bounded_int(
                    self.config.get("bilibili_live_interval_seconds"),
                    60,
                    30,
                    600,
                )
            )

    @staticmethod
    def _raw_data(event: AstrMessageEvent) -> dict[str, Any]:
        raw = event.message_obj.raw_message
        value = getattr(raw, "raw_data", None)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _message_role(event: AstrMessageEvent) -> str:
        author = QQGroupAdmin._raw_data(event).get("author")
        return (
            str(author.get("member_role") or "member")
            if isinstance(author, dict)
            else "member"
        )

    @staticmethod
    def _image_urls(event: AstrMessageEvent) -> list[str]:
        urls = []
        for component in getattr(event.message_obj, "message", None) or []:
            if type(component).__name__ == "Image":
                url = str(
                    getattr(component, "url", "")
                    or getattr(component, "file", "")
                    or ""
                ).strip()
                if url:
                    urls.append(url)
        if urls:
            return urls
        for attachment in QQGroupAdmin._raw_data(event).get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            content_type = str(attachment.get("content_type") or "").lower()
            filename = str(attachment.get("filename") or "").lower()
            if content_type.startswith("image/") or filename.endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".webp")
            ):
                url = str(attachment.get("url") or "").strip()
                if url:
                    if url.startswith("//"):
                        url = "https:" + url
                    urls.append(url)
        return urls

    @staticmethod
    def _image_like_counts(
        event: AstrMessageEvent,
        text: str,
        image_urls: list[str],
    ) -> tuple[int, int]:
        components = list(getattr(event.message_obj, "message", None) or [])
        component_count = sum(
            type(component).__name__ in {"Image", "Face"}
            for component in components
        )
        total = max(component_count, len(image_urls))
        if not total:
            return 0, 0
        if components:
            has_text = any(
                type(component).__name__ == "Plain"
                and str(getattr(component, "text", "") or "").strip()
                for component in components
            )
        else:
            has_text = bool(text.strip())
        return total, 0 if has_text else total

    async def _image_ocr_text(
        self,
        event: AstrMessageEvent,
        image_urls: list[str],
        provider_id: str,
        timeout_seconds: int,
        max_images: int,
    ) -> str:
        """Best-effort OCR used only when image keyword review is enabled."""

        values = [embedded_image_text(event)]
        urls = list(dict.fromkeys(image_urls))[: max(1, min(3, max_images))]
        for url in urls:
            try:
                value = await asyncio.wait_for(
                    asyncio.to_thread(ocr_image_url, url, float(timeout_seconds)),
                    timeout=float(timeout_seconds) + 1,
                )
            except Exception as exc:  # noqa: BLE001 - OCR is fail-open
                self.logger.debug("本地图片 OCR 失败：%s", exc)
                value = ""
            if value:
                values.append(value)

        # A configured vision provider is an explicit opt-in fallback.  Keep it
        # behind the existing semaphore so OCR cannot create an unbounded queue.
        vision_urls = []
        if provider_id and urls:
            for url in urls:
                normalized = await asyncio.to_thread(normalize_vision_image_ref, url)
                if normalized:
                    vision_urls.append(normalized)
                else:
                    self.logger.debug("跳过无法安全转换的 GIF 图片")
        if provider_id and vision_urls:
            try:
                async with asyncio.timeout(max(2, timeout_seconds)):
                    async with self._ai_semaphore:
                        response = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=(
                                "只做图片文字转录，不进行内容审核。尽量原样输出可见文字；"
                                "看不清时输出空行，不要猜测，不要添加解释。"
                            ),
                            image_urls=vision_urls,
                            system_prompt="你是保守的 OCR 引擎，只转录图片中确实可见的文字。",
                        )
                if str(getattr(response, "role", "")) != "err":
                    value = str(getattr(response, "completion_text", "") or "").strip()
                    if value:
                        values.append(value)
            except Exception as exc:  # noqa: BLE001 - OCR is fail-open
                self.logger.debug("视觉模型图片 OCR 失败：%s", exc)
        return "\n".join(dict.fromkeys(value for value in values if value))[:8000]

    @staticmethod
    def _ai_decision(value: str, threshold: int) -> bool | None:
        """Return block/allow; ambiguous model output fails open."""

        text = str(value or "").strip().upper()
        text = re.sub(r"^```(?:JSON)?\s*|\s*```$", "", text).strip()
        first = re.search(r"\b(ALLOW|BLOCK)\b", text)
        if first and first.group(1) == "ALLOW":
            return False
        if first and first.group(1) == "BLOCK":
            pass
        elif re.search(r"^(允许|通过)\b", text):
            return False
        elif not re.search(r"^(拦截|拒绝)\b", text):
            return None
        match = re.search(
            r"(?:CONFIDENCE|SCORE|置信度|分数)\s*[:=：]?\s*(\d{1,3})", text
        )
        if not match:
            return None
        try:
            confidence = int(match.group(1))
        except ValueError:
            return None
        return confidence >= max(50, min(100, int(threshold)))

    @staticmethod
    def _ai_decision_details(value: str) -> tuple[str, int | None, str]:
        text = str(value or "").strip()
        decision = re.search(r"\b(ALLOW|BLOCK)\b", text, re.IGNORECASE)
        confidence = re.search(
            r"(?:CONFIDENCE|SCORE|置信度|分数)\s*[:=：]?\s*(\d{1,3})",
            text,
            re.IGNORECASE,
        )
        reason = re.search(r"(?:REASON|理由|原因)\s*[:=：]\s*(.+)", text, re.IGNORECASE)
        return (
            decision.group(1).upper() if decision else "",
            min(100, int(confidence.group(1))) if confidence else None,
            (reason.group(1).strip() if reason else "")[:200],
        )

    async def _ai_blocks_message(
        self,
        event: AstrMessageEvent,
        text: str,
        image_urls: list[str],
        provider_id: str = "",
        fallback_provider_ids: Any = "",
        timeout_seconds: int = AI_REVIEW_TOTAL_TIMEOUT_SECONDS,
        image_review_enabled: bool = False,
        block_threshold: int = AI_REVIEW_DEFAULT_BLOCK_THRESHOLD,
        confirm_provider_id: str = "",
        result: dict[str, Any] | None = None,
    ) -> bool:
        vision_urls = []
        if image_review_enabled:
            for url in dict.fromkeys(image_urls):
                normalized = await asyncio.to_thread(normalize_vision_image_ref, url)
                if normalized:
                    vision_urls.append(normalized)
                else:
                    self.logger.debug("跳过无法安全转换的 GIF 图片")
        if not text and not vision_urls:
            return False
        prompt = (
            "审核以下 QQ 群消息。只有在明确的色情、暴力威胁、违法交易、诈骗引流、"
            "严重人身攻击/隐私泄露，或明确煽动自伤他伤时才拦截。普通吐槽、轻度脏话、"
            "玩笑、游戏术语、角色名、单个词和游戏/动漫截图中的文字必须放行。"
            "请只输出一行：ALLOW confidence=0-100 reason=... 或 "
            "BLOCK confidence=0-100 reason=...。\n消息："
            + (text[:4000] if text else "[仅图片]")
        )
        providers = []
        if provider_id:
            providers.append(provider_id)
        else:
            try:
                providers.append(
                    await self.context.get_current_chat_provider_id(
                        event.unified_msg_origin
                    )
                )
            except Exception as exc:  # noqa: BLE001 - fallback may still work
                self.logger.debug("读取当前 AI 审核模型失败：%s", exc)
        providers.extend(normalize_provider_ids(fallback_provider_ids))
        errors = []
        total_timeout = self._bounded_int(
            timeout_seconds, AI_REVIEW_TOTAL_TIMEOUT_SECONDS, 5, 120
        )
        deadline = time.monotonic() + total_timeout
        candidates = list(
            dict.fromkeys(str(value or "").strip() for value in providers if value)
        )
        for index, current_provider_id in enumerate(candidates):
            if not current_provider_id:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("达到 AI 审核总超时")
                break
            # Reserve a slice for every remaining candidate so a hung model
            # cannot consume the budget needed by fallbacks or confirmation.
            remaining_candidates = len(candidates) - index + bool(
                str(confirm_provider_id or "").strip()
            )
            provider_timeout = max(
                1.0,
                min(30.0, remaining / max(1, remaining_candidates)),
            )
            try:
                async with asyncio.timeout(min(provider_timeout, remaining)):
                    async with self._ai_semaphore:
                        response = await self.context.llm_generate(
                            chat_provider_id=current_provider_id,
                            prompt=prompt,
                            image_urls=vision_urls or None,
                            system_prompt=(
                                "你是保守的群消息审核器，宁可放行不确定内容，"
                                "不得把普通聊天或游戏截图判为违规。"
                            ),
                        )
                if str(getattr(response, "role", "")) == "err":
                    raise RuntimeError("模型返回错误响应")
                raw_decision = str(getattr(response, "completion_text", "") or "")
                decision = self._ai_decision(raw_decision, block_threshold)
                if decision is None:
                    raise RuntimeError("模型未返回带置信度的 ALLOW/BLOCK")
                _, confidence, ai_reason = self._ai_decision_details(
                    raw_decision
                )
                if result is not None:
                    result.update(
                        {
                            "provider": current_provider_id,
                            "decision": "BLOCK" if decision else "ALLOW",
                            "confidence": confidence,
                            "reason": ai_reason,
                        }
                    )
                self.logger.debug(
                    "AI 群消息审核完成：provider=%s decision=%s",
                    current_provider_id,
                    "BLOCK" if decision else "ALLOW",
                )
                if not decision or not str(confirm_provider_id or "").strip():
                    return decision

                confirm_provider = str(confirm_provider_id).strip()

                def confirmation_failed(
                    detail: str,
                    provider: str = confirm_provider,
                ) -> bool:
                    safe_detail = re.sub(r"https?://\S+", "<url>", detail)[:120]
                    if result is not None:
                        result.update(
                            {
                                "confirm_provider": provider,
                                "confirm_decision": "ERROR",
                                "confirm_reason": safe_detail,
                                "confirmation_failed": True,
                            }
                        )
                    if time.monotonic() >= self._ai_warning_at:
                        self.logger.warning(
                            "AI 二次确认失败，本条仅记录未撤回：provider=%s error=%s",
                            provider,
                            safe_detail,
                        )
                        self._ai_warning_at = time.monotonic() + 300
                    return True

                if confirm_provider in candidates:
                    return confirmation_failed("确认模型与初判候选模型重复")
                confirm_remaining = deadline - time.monotonic()
                if confirm_remaining <= 0:
                    return confirmation_failed("达到 AI 审核总超时")
                try:
                    async with asyncio.timeout(min(30.0, confirm_remaining)):
                        async with self._ai_semaphore:
                            confirm_response = await self.context.llm_generate(
                                chat_provider_id=confirm_provider,
                                prompt=prompt,
                                image_urls=vision_urls or None,
                                system_prompt=(
                                    "你是独立的群消息复核器，宁可放行不确定内容。"
                                    "只按消息本身判断，不得扩大违规范围。"
                                ),
                            )
                    if str(getattr(confirm_response, "role", "")) == "err":
                        raise RuntimeError("模型返回错误响应")
                    raw_confirm = str(
                        getattr(confirm_response, "completion_text", "") or ""
                    )
                    confirmed = self._ai_decision(raw_confirm, block_threshold)
                    if confirmed is None:
                        raise RuntimeError("模型未返回带置信度的 ALLOW/BLOCK")
                    _, confirm_confidence, confirm_reason = (
                        self._ai_decision_details(raw_confirm)
                    )
                    if result is not None:
                        result.update(
                            {
                                "confirm_provider": confirm_provider,
                                "confirm_decision": (
                                    "BLOCK" if confirmed else "ALLOW"
                                ),
                                "confirm_confidence": confirm_confidence,
                                "confirm_reason": confirm_reason,
                                "confirmation_failed": False,
                            }
                        )
                    self.logger.debug(
                        "AI 群消息二次确认完成：provider=%s decision=%s",
                        confirm_provider,
                        "BLOCK" if confirmed else "ALLOW",
                    )
                    return confirmed
                except Exception as exc:  # noqa: BLE001 - downgrade to record-only
                    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                        detail = "模型超时"
                    elif str(exc) in {
                        "模型返回错误响应",
                        "模型未返回带置信度的 ALLOW/BLOCK",
                    }:
                        detail = str(exc)
                    else:
                        detail = "模型调用失败"
                    self.logger.debug(
                        "AI 二次确认模型不可用：provider=%s error_type=%s",
                        confirm_provider,
                        type(exc).__name__,
                    )
                    return confirmation_failed(detail)
            except Exception as exc:  # noqa: BLE001 - try configured fallback
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    detail = "模型超时"
                else:
                    detail = str(exc).strip() or type(exc).__name__
                # Provider adapters may include request URLs or credentials in
                # exception text. Keep the warning useful without echoing raw
                # transport details; the full exception remains debug-only.
                detail = re.sub(r"https?://\S+", "<url>", detail)[:120]
                errors.append(f"{current_provider_id}: {detail}")
                self.logger.debug(
                    "AI 群消息审核模型不可用：provider=%s error=%s",
                    current_provider_id,
                    exc,
                )
        if time.monotonic() >= self._ai_warning_at:
            detail = "; ".join(errors) or "没有可用模型"
            self.logger.warning("AI 群消息审核失败，本条已放行：%s", detail)
            self._ai_warning_at = time.monotonic() + 300
        if result is not None:
            result.update({"decision": "ERROR", "reason": "; ".join(errors)[:200]})
        return False

    async def _recall_messages(
        self,
        api: QQGroupAPI,
        group_openid: str,
        message_ids: list[str],
    ) -> list[str]:
        failed = []
        requested = list(dict.fromkeys(message_ids))
        for message_id in requested:
            try:
                async with self._recall_lock:
                    wait = 0.11 - (time.monotonic() - self._last_recall_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    try:
                        await api.recall_group_message(group_openid, message_id)
                    finally:
                        self._last_recall_at = time.monotonic()
            except QQAPIError as exc:
                failed.append(message_id)
                self.logger.warning(
                    "撤回群消息失败：group=%s message=%s error=%s",
                    group_openid,
                    message_id,
                    exc,
                )
        self._moderation.forget_messages(
            group_openid,
            [message_id for message_id in requested if message_id not in failed],
        )
        return failed

    async def _warn_member(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        reason: str,
        *,
        message_id: str = "",
    ) -> None:
        _, group_openid, _ = self._context(event)
        await self._send_group_notice(
            self._client(event),
            group_openid,
            reason,
            member_openid=member_openid,
            message_id=message_id,
        )

    def _member_list_matches(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        member_openid: str,
        values: list[str],
    ) -> bool:
        if not values:
            return False
        candidates = {member_openid}
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        union_openid = (
            str(author.get("union_openid") or "").strip()
            if isinstance(author, dict)
            else str(getattr(author, "union_openid", "") or "").strip()
        )
        raw_data = self._raw_data(event)
        if not union_openid and isinstance(raw_data.get("author"), dict):
            union_openid = str(
                raw_data["author"].get("union_openid") or ""
            ).strip()
        if union_openid:
            candidates.add(union_openid)
        uid = self._uid_for_member(group_openid, member_openid)
        if uid:
            candidates.add(uid)
        return bool(
            {candidate.casefold() for candidate in candidates}
            .intersection(value.casefold() for value in values)
        )

    async def _handle_member_blacklist(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        member_openid: str,
        message_id: str,
        delivery_key: tuple[str, str, str, str],
        settings: dict[str, Any],
        *,
        global_match: bool,
    ) -> None:
        reason = (
            "成员命中全局群聊黑名单，消息已撤回。"
            if global_match
            else "成员命中本群黑名单，消息已撤回。"
        )
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        union_openid = (
            str(author.get("union_openid") or "")
            if isinstance(author, dict)
            else str(getattr(author, "union_openid", "") or "")
        )
        username = (
            str(author.get("username") or "")
            if isinstance(author, dict)
            else str(getattr(author, "username", "") or "")
        )
        text = str(event.get_message_str() or "").strip()
        images = self._image_urls(event)
        uid = self._uid_for_member(group_openid, member_openid)
        if hasattr(event, "stop_event"):
            event.stop_event()
        await self._record_uid_violation(
            uid,
            group_openid,
            member_openid,
            reason,
            content=text or ("[图片]" * max(1, len(images))),
            message_id=message_id,
            action_member_openid=member_openid,
            request={
                "username": username,
                "union_openid": union_openid,
            },
        )
        try:
            reply = settings[
                "global_blacklist_reply" if global_match else "blacklist_reply"
            ]
            at_member = settings[
                "global_blacklist_at" if global_match else "blacklist_at"
            ]
            if reply or at_member:
                await self._send_group_notice(
                    self._client(event),
                    group_openid,
                    reply,
                    member_openid=member_openid if at_member or "{at_user}" in reply else "",
                    message_id=message_id,
                )
        except Exception as exc:  # noqa: BLE001 - warning must not reopen message
            self.logger.warning("发送黑名单提示失败：%s", exc)
        failed = await self._recall_messages(
            self._api(event), group_openid, [message_id]
        )
        if not failed:
            self._moderation.remember(delivery_key, True)

    async def _reply_to_keyword(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        message_id: str,
        text: str,
        entry: dict[str, Any] | None,
    ) -> bool:
        if not entry or str(entry.get("platform_id") or "") != str(
            event.get_platform_id()
        ):
            return False
        now = time.monotonic()
        previous_ready_at = self._keyword_reply_ready_at.get(group_openid, 0)
        if now < previous_ready_at:
            return False
        reply = keyword_reply_for_message(
            text,
            group_openid,
            (entry or {}).get("keyword_replies"),
            self.config.get("global_keyword_replies"),
        )
        if reply is None:
            return False
        cooldown = self._bounded_int(
            self.config.get("keyword_reply_cooldown_seconds"), 0, 0, 3_600
        )
        reservation = time.monotonic() + cooldown
        if cooldown:
            self._keyword_reply_ready_at[group_openid] = reservation
        try:
            client = self._client(event)
            sent = await self._send_group_text(
                client,
                group_openid,
                reply,
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001 - reply failures must not block chat
            self.logger.warning(
                "发送关键词回复失败：group=%s error=%s", group_openid, exc
            )
            if (
                cooldown
                and self._keyword_reply_ready_at.get(group_openid) == reservation
            ):
                if previous_ready_at:
                    self._keyword_reply_ready_at[group_openid] = previous_ready_at
                else:
                    self._keyword_reply_ready_at.pop(group_openid, None)
            return False
        if cooldown:
            self._keyword_reply_ready_at[group_openid] = time.monotonic() + cooldown
        else:
            self._keyword_reply_ready_at.pop(group_openid, None)
        recall = self._bounded_int(
            self.config.get("keyword_reply_recall_seconds"), 0, 0, 120
        )
        sent_id = str(
            sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", "") or ""
        )
        if recall and sent_id:
            self._schedule_recall(
                client,
                group_openid,
                sent_id,
                recall,
                "keyword-reply",
            )
        elif recall:
            self.logger.warning(
                "QQ 未返回关键词回复消息 ID，无法自动撤回：group=%s",
                group_openid,
            )
        if hasattr(event, "stop_event"):
            event.stop_event()
        return True

    @filter.platform_adapter_type(QQ_PLATFORM_TYPES)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def audit_group_message(self, event: AstrMessageEvent) -> None:
        try:
            await self._audit_group_message_impl(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - moderation must fail open
            self.logger.warning("群消息审核失败，本条已放行：%s", exc)

    async def _audit_group_message_impl(self, event: AstrMessageEvent) -> None:
        try:
            raw, group_openid, member_openid = self._context(event)
        except ValueError:
            return
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not message_id:
            return
        raw_data = self._raw_data(event)
        author = raw_data.get("author")
        if isinstance(author, dict) and author.get("bot") is True:
            return
        msg_seq = str(getattr(raw, "msg_seq", "") or raw_data.get("msg_seq") or "")
        delivery_key = (
            str(event.get_platform_id()),
            group_openid,
            message_id,
            msg_seq,
        )
        duplicate = self._moderation.duplicate(delivery_key)
        if duplicate is not None:
            if hasattr(event, "stop_event"):
                event.stop_event()
            return
        role = self._message_role(event)
        if not bool(getattr(event, "is_at_or_wake_command", False)):
            self._moderation.record_message(
                group_openid,
                member_openid,
                message_id,
                role,
            )

        entry = self._group_config(group_openid)
        settings = self._moderation_settings(entry)
        admin_exempt = settings["exempt_admins"] and role in GROUP_ADMIN_ROLES
        global_blacklist_match = self._member_list_matches(
            event,
            group_openid,
            member_openid,
            settings["global_member_blacklist"],
        )
        group_blacklist_match = self._member_list_matches(
            event,
            group_openid,
            member_openid,
            settings["member_blacklist"],
        )
        if not admin_exempt and (global_blacklist_match or group_blacklist_match):
            self._moderation.break_repeat(group_openid)
            await self._handle_member_blacklist(
                event,
                group_openid,
                member_openid,
                message_id,
                delivery_key,
                settings,
                global_match=global_blacklist_match,
            )
            return
        member_whitelisted = (
            self._member_list_matches(
                event,
                group_openid,
                member_openid,
                settings["global_member_whitelist"],
            )
            or self._member_list_matches(
                event,
                group_openid,
                member_openid,
                settings["member_whitelist"],
            )
        )
        # Trusted lists are evaluated before the suspicious-member challenge.
        # A blacklist still wins over a whitelist above; an explicitly trusted
        # member can therefore recover from a stale challenge without needing
        # to solve it first.
        suspicious_key = self._member_state_key(group_openid, member_openid)
        if (
            suspicious_key in self._suspicious_members
            and not admin_exempt
            and not member_whitelisted
        ):
            self._moderation.break_repeat(group_openid)
            if hasattr(event, "stop_event"):
                event.stop_event()
            try:
                self._cleanup_tokens()
                if not any(
                    data[1:3] == (group_openid, member_openid)
                    for data in self._verification_tokens.values()
                ):
                    await self._send_verification_challenge(
                        self._client(event),
                        group_openid,
                        member_openid,
                        message_id=message_id,
                    )
            except Exception as exc:  # noqa: BLE001 - QQ keyboard boundary
                self.logger.warning("发送真人验证按钮失败：%s", exc)
            failed = await self._recall_messages(
                self._api(event), group_openid, [message_id]
            )
            if not failed:
                self._moderation.remember(delivery_key, True)
            return
        text = str(event.get_message_str() or "").strip()
        if not settings["enabled"] or admin_exempt or member_whitelisted:
            self._moderation.break_repeat(group_openid)
            if settings["image_enabled"]:
                self._moderation.break_image_chain(group_openid, member_openid)
            await self._reply_to_keyword(event, group_openid, message_id, text, entry)
            self._moderation.remember(delivery_key, False)
            return

        images = self._image_urls(event)
        image_count, pure_image_count = self._image_like_counts(event, text, images)
        reason = ""
        warn_text = ""
        warn_at_member = True
        ai_review: dict[str, Any] = {}
        ai_record_only = False
        recall_ids: list[str] = []
        ocr_text = ""
        if settings["image_enabled"] and image_count == 0:
            self._moderation.break_image_chain(group_openid, member_openid)
        if matched_keyword(text, settings["global_keywords"]):
            reason = "消息命中全局禁止关键词，已撤回。"
            warn_text = settings["global_keyword_reply"]
            warn_at_member = settings["global_keyword_at"]
        elif matched_keyword(text, settings["keywords"]):
            reason = "消息命中本群禁止关键词，已撤回。"
            warn_text = settings["keyword_reply"]
            warn_at_member = settings["keyword_at"]
        elif settings["image_ocr_enabled"] and image_count and (
            settings["global_image_keywords"]
            or (settings["image_keyword_enabled"] and settings["image_keywords"])
        ):
            ocr_text = embedded_image_text(event)
            embedded_match = matched_keyword(
                ocr_text, settings["global_image_keywords"]
            ) or (
                settings["image_keyword_enabled"]
                and matched_keyword(ocr_text, settings["image_keywords"])
            )
            if not embedded_match:
                ocr_text = await self._image_ocr_text(
                    event,
                    images,
                    settings["image_ocr_provider_id"],
                    settings["image_ocr_timeout"],
                    settings["image_ocr_max_images"],
                )
            if matched_keyword(ocr_text, settings["global_image_keywords"]):
                reason = "图片文字命中全局禁止关键词，已撤回。"
                warn_text = settings["global_image_reply"]
                warn_at_member = settings["global_image_at"]
            elif settings["image_keyword_enabled"] and matched_keyword(
                ocr_text, settings["image_keywords"]
            ):
                reason = "图片文字命中本群禁止关键词，已撤回。"
                warn_text = settings["image_keyword_reply"]
                warn_at_member = settings["image_keyword_at"]
        if not reason and settings["image_enabled"]:
            image_recall_ids = self._moderation.add_images(
                group_openid,
                member_openid,
                message_id,
                image_count,
                threshold=settings["image_count"],
                window=settings["image_window"],
                recall_limit=settings["image_recall_count"],
            )
            group_recall_ids = self._moderation.add_group_images(
                group_openid,
                member_openid,
                message_id,
                pure_image_count,
                threshold=settings["image_count"],
                min_members=settings["image_group_min_members"],
                window=settings["image_window"],
                recall_limit=settings["image_recall_count"],
            )
            recall_ids = list(dict.fromkeys(image_recall_ids + group_recall_ids))
            if group_recall_ids:
                reason = "检测到多人连续发送图片或表情，相关消息已撤回。"
            elif image_recall_ids:
                reason = "短时间连续发送图片或表情，相关消息已撤回。"
            if group_recall_ids or image_recall_ids:
                warn_text = settings["image_spam_reply"]
                warn_at_member = settings["image_spam_at"]
        if reason and not recall_ids and settings["image_enabled"]:
            self._moderation.break_image_chain(group_openid, member_openid)

        repeat_members: list[str] = []
        if reason:
            self._moderation.break_repeat(group_openid)
        elif settings["repeat_enabled"]:
            signature = "" if pure_image_count else normalize_message(text)
            repeat_members = self._moderation.add_repeat(
                group_openid,
                signature,
                member_openid,
                role,
                message_id,
                threshold=settings["repeat_count"],
                window=settings["repeat_window"],
            )
            if repeat_members:
                reason = "检测到集中复读，已随机禁言一名参与者。"
                warn_text = settings["repeat_reply"]
                warn_at_member = settings["repeat_at"]
                recall_ids = [message_id]
        else:
            self._moderation.break_repeat(group_openid)
        if (
            not reason
            and settings["ai_enabled"]
            and await self._ai_blocks_message(
                event,
                text,
                images,
                settings["ai_provider_id"],
                settings["ai_fallback_provider_ids"],
                settings["ai_timeout"],
                settings["ai_images_enabled"],
                settings["ai_block_threshold"],
                settings["ai_confirm_provider_id"],
                result=ai_review,
            )
        ):
            ai_record_only = settings["ai_action"] == "record_only" or bool(
                ai_review.get("confirmation_failed")
            )
            reason = (
                "消息命中 AI 内容审核，二次确认失败，仅记录未撤回。"
                if ai_review.get("confirmation_failed")
                else "消息命中 AI 内容审核，仅记录未撤回。"
                if ai_record_only
                else "消息未通过 AI 内容审核，已撤回。"
            )
            warn_text = settings["ai_reply"]
            warn_at_member = settings["ai_at"]

        if not reason:
            await self._reply_to_keyword(event, group_openid, message_id, text, entry)
            self._moderation.remember(delivery_key, False)
            return
        if not ai_record_only and hasattr(event, "stop_event"):
            event.stop_event()
        target_member = member_openid
        if repeat_members:
            target_member = secrets.choice(repeat_members)
            duration = (
                secrets.randbelow(
                    settings["repeat_mute_max"] - settings["repeat_mute_min"] + 1
                )
                + settings["repeat_mute_min"]
            )
            try:
                await self._api(event).set_member_mutes(
                    group_openid,
                    [
                        {
                            "op": "add",
                            "member_openid": target_member,
                            "mute_expire_at": future_rfc3339(
                                parse_duration(str(duration))
                            ),
                        }
                    ],
                )
            except (QQAPIError, ValueError) as exc:
                self.logger.warning("复读随机禁言失败：%s", exc)
                reason = "检测到集中复读，相关消息已撤回；随机禁言失败。"
                warn_text = ""
                warn_at_member = False
            else:
                reason = f"参与集中复读，已随机禁言 {duration} 秒。"
                if "{duration}" in warn_text:
                    warn_text = warn_text.replace("{duration}", str(duration))
        uid = self._uid_for_member(group_openid, member_openid)
        self.logger.info(
            "已处理违规群消息：group=%s member=%s uid=%s reason=%s",
            group_openid,
            target_member,
            uid or "-",
            reason,
        )
        author = getattr(event.message_obj.raw_message, "author", None)
        await self._record_uid_violation(
            uid,
            group_openid,
            member_openid,
            reason,
            content=(text or ("[图片]" * max(1, len(images))))
            + (f"\n[图片文字]\n{ocr_text[:2000]}" if ocr_text else ""),
            message_id=message_id,
            action_member_openid=target_member,
            action="record_only" if ai_record_only else "recall",
            ai_review=ai_review,
            request={
                "username": str(getattr(author, "username", "") or ""),
                "union_openid": str(getattr(author, "union_openid", "") or ""),
            },
        )
        if ai_record_only:
            self._moderation.remember(delivery_key, False)
            return
        try:
            if warn_text or warn_at_member:
                await self._send_group_notice(
                    self._client(event),
                    group_openid,
                    warn_text,
                    member_openid=(
                        target_member
                        if warn_at_member or "{at_user}" in (warn_text or "")
                        else ""
                    ),
                    message_id=message_id,
                )
        except Exception as exc:  # noqa: BLE001 - warning should not reopen event
            self.logger.warning("发送群消息审核警告失败：%s", exc)
        failed = await self._recall_messages(
            self._api(event), group_openid, recall_ids or [message_id]
        )
        if not failed:
            self._moderation.remember(delivery_key, True)

    def _save_group_config(
        self,
        entry: dict[str, Any],
        strategy_id: str,
        users: list[str],
    ) -> None:
        value = ",".join(users)
        entry["managed_strategy_id"] = strategy_id
        entry["applied_whitelist"] = value
        self.config.save_config()

    def _record_whitelist_change(
        self,
        group_openid: str,
        strategy_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        entry = self._group_config(group_openid)
        if entry is None:
            return
        removed = set(remove or [])

        def changed(users: list[str]) -> list[str]:
            users = [user for user in users if user not in removed]
            known = set(users)
            for user in add or []:
                if user not in known:
                    users.append(user)
                    known.add(user)
            return users

        desired = changed(
            parse_qq_number_text(str(entry.get("whitelist_qq_numbers") or ""))
        )
        applied = changed(
            parse_qq_number_text(str(entry.get("applied_whitelist") or ""))
        )
        entry["whitelist_qq_numbers"] = ",".join(desired)
        entry["enabled"] = True
        entry["managed_strategy_id"] = strategy_id
        entry["applied_whitelist"] = ",".join(applied)
        self.config.save_config()

    def _clear_group_config(self, group_openid: str) -> None:
        entry = self._group_config(group_openid)
        if entry is None:
            return
        entry["enabled"] = False
        entry["managed_strategy_id"] = ""
        entry["applied_whitelist"] = ""
        self.config.save_config()

    def _results(self, event: AstrMessageEvent, text: str):
        for chunk in split_message(text):
            yield event.plain_result(chunk)

    @staticmethod
    def _plain_text(value: Any, limit: int = 160) -> str:
        return " ".join(str(value or "-").split())[:limit]

    @staticmethod
    def _markdown_text(value: Any, limit: int = 160) -> str:
        text = " ".join(str(value or "-").split())[:limit]
        for char in "\\`*_{}[]()#+-.!|<>":
            text = text.replace(char, f"\\{char}")
        return text

    @qq_admin_command("群帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示完整命令帮助。"""
        self._context(event)
        yield event.plain_result(self.HELP)

    @qq_admin_command("群信息")
    async def group_info(self, event: AstrMessageEvent):
        """查询当前群基本信息。"""
        _, group_openid, member_openid = self._context(event)
        try:
            data = await self._api(event).get_group_info(group_openid)
        except (QQAPIError, RuntimeError) as exc:
            yield event.plain_result(
                "\n".join(
                    [
                        f"群 OpenID：{group_openid}",
                        f"你的成员 OpenID：{member_openid}",
                        f"群资料查询失败：{exc}",
                    ]
                )
            )
            return
        yield event.plain_result(
            "\n".join(
                [
                    f"群名称：{self._value(data.get('group_name'))}",
                    f"群 OpenID：{group_openid}",
                    f"你的成员 OpenID：{member_openid}",
                    f"简介：{self._value(data.get('group_finger_memo'))}",
                    f"分类：{self._value(data.get('group_class_text'))}",
                    f"标签：{self._list(data.get('group_tags'))}",
                    f"成员数：{self._value(data.get('group_member_num'))}",
                ]
            )
        )

    @qq_admin_command("同步指令面板")
    async def sync_command_panel(self, event: AstrMessageEvent):
        """创建或更新由本插件管理的 QQ 原生群指令面板。"""
        self._context(event)
        api = self._api(event)
        async with self._command_panel_lock:
            records = []
            cursor = ""
            seen_cursors = {""}
            while True:
                data = (
                    await api.list_group_panels(cursor=cursor)
                    if cursor
                    else await api.list_group_panels()
                )
                page_records = data.get("records") if isinstance(data, dict) else None
                if not isinstance(page_records, list):
                    raise TypeError("QQ API 未返回有效的指令面板列表")
                records.extend(page_records)
                raw_next_cursor = data.get("next_cursor")
                if raw_next_cursor is None:
                    next_cursor = ""
                elif isinstance(raw_next_cursor, str):
                    next_cursor = raw_next_cursor.strip()
                else:
                    raise TypeError("QQ API 返回了无效的指令面板分页游标")
                is_end = data.get("is_end")
                if is_end is False and not next_cursor:
                    raise RuntimeError("QQ API 指令面板分页游标缺失")
                if is_end is True or not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError("QQ API 指令面板分页游标重复")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            managed = [
                record
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("panel"), dict)
                and record["panel"].get("remark") == COMMAND_PANEL_REMARK
            ]
            if len(managed) > 1:
                raise RuntimeError(
                    "检测到多个由本插件管理的指令面板，请先在 QQ 开放平台清理"
                )
            if managed:
                if managed[0].get("target_type") != "all":
                    raise RuntimeError(
                        "插件指令面板已改为指定群范围，请先在 QQ 开放平台清理"
                    )
                panel_id = str(managed[0].get("panel_id") or "").strip()
                if not panel_id:
                    raise RuntimeError("QQ API 未返回指令面板 ID")
                await api.update_panel(panel_id, COMMAND_PANEL)
                action = "更新"
            else:
                await api.create_group_panel(COMMAND_PANEL)
                action = "创建"
        yield event.plain_result(
            f"已{action} QQ 原生群指令面板，共 {len(COMMAND_PANEL['items'])} 条命令。"
        )

    @qq_group_command("审核设置")
    async def review_settings(self, event: AstrMessageEvent):
        """发送仅 QQ 群主或管理员可操作的审核设置按钮。"""
        if not bool(self.config.get("settings_command_enabled", True)):
            if hasattr(event, "stop_event"):
                event.stop_event()
            return
        _, group_openid, _ = self._context(event)
        client = self._client(event)
        info = await QQGroupAPI(client).get_group_info(group_openid)
        group_name = str(info.get("group_name") or "").strip()
        if not group_name:
            yield event.plain_result("操作失败：QQ API 未返回群名称")
            return
        token = self._settings_token(
            group_openid,
            str(event.get_platform_id()),
            group_name,
        )

        text, rows = self._settings_home_payload(group_openid, token, group_name)
        auto_recall = bool(self.config.get("settings_panel_auto_recall", True))
        recall_hint = (
            f"{SETTINGS_MESSAGE_TTL} 秒后自动撤回。"
            if auto_recall
            else "面板不会自动撤回。"
        )
        kwargs = {
            "group_openid": group_openid,
            "msg_type": 2,
            "markdown": {"content": f"{text}\n{recall_hint}"},
            "keyboard": {"content": {"rows": rows}},
        }
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if message_id:
            kwargs["msg_id"] = message_id
        try:
            sent = await client.api.post_group_message(**kwargs)
        except Exception as exc:
            detail = self._plain_text(exc, 240)
            self.logger.warning("发送审核设置按钮失败：%s", exc)
            raise RuntimeError(
                "发送审核设置按钮失败；请确认 Markdown 和自定义按钮权限"
                f"（QQ 返回：{detail}）"
            ) from exc
        raw_sent_id = (
            sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", "")
        )
        sent_id = str(raw_sent_id or "")
        if sent_id and auto_recall:
            self._schedule_settings_recall(client, group_openid, sent_id)
        elif not sent_id and auto_recall:
            self.logger.warning("QQ 未返回审核设置消息 ID，无法自动撤回")
        if hasattr(event, "stop_event"):
            event.stop_event()

    @staticmethod
    def _settings_button(
        token: str,
        button_id: str,
        label: str,
        action: str,
        style: int,
    ) -> dict[str, Any]:
        return {
            "id": button_id,
            "render_data": {
                "label": label,
                "visited_label": label,
                "style": style,
            },
            "action": {
                "type": 1,
                "permission": {"type": 1},
                "data": f"qqgs:{token}:{action}",
                "unsupport_tips": "当前 QQ 版本不支持设置按钮",
            },
        }

    def _settings_home_payload(
        self,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        def button(
            button_id: str, label: str, action: str, style: int
        ) -> dict[str, Any]:
            return self._settings_button(token, button_id, label, action, style)

        rows = [
            {
                "buttons": [
                    button("bind", "绑定此群", "bind", 1),
                    button("sync", "应用配置", "sync", 1),
                ]
            },
            {
                "buttons": [
                    button("conditions", "审核条件", "conditions", 1),
                    button("moderation", "消息审查", "moderation", 1),
                ]
            },
            {
                "buttons": [
                    button("keywords", "关键词回复", "keywords", 1),
                    button("bilibili", "B站推送", "bilibili", 1),
                ]
            },
            {"buttons": [button("off", "关闭自动审核", "off", 0)]},
        ]
        entry = self._group_config(group_openid)
        conditions = self._condition_settings(entry) if entry else {}
        moderation = self._moderation_settings(entry)
        mode = (
            "QQ 白名单"
            if entry and entry.get("enabled")
            else "条件审核"
            if conditions.get("enabled")
            else "已关闭"
            if entry
            else "未绑定"
        )
        keyword_rules = (entry or {}).get("keyword_replies")
        keyword_count = (
            sum(
                isinstance(rule, dict) and bool(rule.get("enabled", True))
                for rule in keyword_rules
            )
            if isinstance(keyword_rules, list)
            else 0
        )
        return (
            (
                f"# {self._markdown_text(group_name)}\n"
                f"入群审核：{mode}；消息审查：{'开' if moderation['enabled'] else '关'}；"
                f"本群关键词回复：{keyword_count} 条\n"
                "设置按钮仅群主或群管理员可用。"
            ),
            rows,
        )

    async def _send_settings_panel(
        self,
        client: Any,
        group_openid: str,
        text: str,
        rows: list[dict[str, Any]],
    ) -> None:
        sent = await self._send_group_markdown(
            client,
            group_openid,
            text,
            keyboard={"content": {"rows": rows}},
        )
        sent_id = str(
            sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", "") or ""
        )
        if sent_id and bool(self.config.get("settings_panel_auto_recall", True)):
            self._schedule_settings_recall(client, group_openid, sent_id)

    async def _send_settings_home(
        self,
        client: Any,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> None:
        text, rows = self._settings_home_payload(group_openid, token, group_name)
        await self._send_settings_panel(client, group_openid, text, rows)

    async def _send_condition_settings(
        self,
        client: Any,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> None:
        def button(
            button_id: str, label: str, action: str, style: int
        ) -> dict[str, Any]:
            return self._settings_button(token, button_id, label, action, style)

        rows = [
            {
                "buttons": [
                    button("conditional", "条件审核", "conditional", 1),
                    button("native", "QQ白名单", "native", 1),
                    button("off", "关闭审核", "off", 0),
                ]
            },
            {
                "buttons": [
                    button("uid-on", "UID检查开", "uid_on", 1),
                    button("uid-off", "UID检查关", "uid_off", 0),
                ]
            },
            {
                "buttons": [
                    button("direct-on", "UID直通开", "direct_on", 1),
                    button("direct-off", "UID直通关", "direct_off", 0),
                ]
            },
            {
                "buttons": [
                    button("all", "全部满足", "all", 1),
                    button("any", "任一满足", "any", 1),
                    button("home", "返回主页", "home", 0),
                ]
            },
            {
                "buttons": [
                    button("pending", "未过待审", "pending", 0),
                    button("decline", "未过拒绝", "decline", 0),
                    button("approve", "未过同意", "approve", 0),
                ]
            },
        ]
        entry = self._group_config(group_openid)
        settings = self._condition_settings(entry) if entry else {}
        mode = (
            "QQ 白名单"
            if entry and entry.get("enabled")
            else "条件审核"
            if settings.get("enabled")
            else "已关闭"
            if entry
            else "未绑定"
        )
        logic = (
            "全部满足"
            if settings.get("condition_logic", "all") == "all"
            else "任一满足"
        )
        fallback = {
            "pending": "保留待审",
            "decline": "拒绝",
            "approve": "同意",
        }.get(settings.get("fallback_action", "pending"), "保留待审")
        await self._send_settings_panel(
            client,
            group_openid,
            (
                f"# {self._markdown_text(group_name)} 审核条件\n"
                f"模式：{mode}；UID 检查：{'开' if settings.get('uid_check_enabled', True) else '关'}；"
                f"UID 直通：{'开' if settings.get('uid_exists_auto_approve') else '关'}\n"
                f"组合：{logic}；未通过：{fallback}\n"
                "通过与拒绝关键词请在插件 WebUI 编辑。"
            ),
            rows,
        )

    async def _send_keyword_settings(
        self,
        client: Any,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> None:
        def active_rules(value: Any) -> list[dict[str, Any]]:
            return (
                [
                    rule
                    for rule in value
                    if isinstance(rule, dict) and bool(rule.get("enabled", True))
                ]
                if isinstance(value, list)
                else []
            )

        def rule_label(rule: dict[str, Any]) -> str:
            name = str(rule.get("name") or rule.get("rule_name") or "").strip()
            if name:
                return name
            keywords = rule.get("keywords", rule.get("keyword", ""))
            if isinstance(keywords, list):
                keywords = "/".join(str(item) for item in keywords[:2])
            return str(keywords or "未命名规则").strip()[:24]

        entry = self._group_config(group_openid)
        group_rules = active_rules((entry or {}).get("keyword_replies"))
        global_rules = active_rules(self.config.get("global_keyword_replies"))
        names = "、".join(rule_label(rule) for rule in group_rules[:3]) or "无"
        cooldown = self._bounded_int(
            self.config.get("keyword_reply_cooldown_seconds"), 0, 0, 86_400
        )
        recall = self._bounded_int(
            self.config.get(
                "keyword_reply_recall_seconds",
                self.config.get("keyword_reply_auto_recall_seconds"),
            ),
            0,
            0,
            3_600,
        )
        rows = [
            {
                "buttons": [
                    self._settings_button(token, "home", "返回主页", "home", 0),
                    self._settings_button(
                        token, "moderation", "消息审查", "moderation", 1
                    ),
                    self._settings_button(token, "bilibili", "B站推送", "bilibili", 1),
                ]
            }
        ]
        await self._send_settings_panel(
            client,
            group_openid,
            (
                f"# {self._markdown_text(group_name)} 关键词回复\n"
                f"本群：{len(group_rules)} 条（{self._markdown_text(names, 96)}）；"
                f"全局：{len(global_rules)} 条\n"
                f"每群冷却：{cooldown} 秒；回复撤回：{recall if recall else '关闭'}"
                f"{' 秒' if recall else ''}\n"
                "规则名称、关键词 AND/OR、回复内容和覆盖群请在插件 WebUI 编辑。"
            ),
            rows,
        )

    async def _send_bilibili_settings(
        self,
        client: Any,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> None:
        entry = self._group_config(group_openid)
        rows = [
            {
                "buttons": [
                    self._settings_button(
                        token, "dynamic-on", "动态推送开", "bili_dynamic_on", 1
                    ),
                    self._settings_button(
                        token, "dynamic-off", "动态推送关", "bili_dynamic_off", 0
                    ),
                ]
            },
            {
                "buttons": [
                    self._settings_button(
                        token, "live-on", "直播推送开", "bili_live_on", 1
                    ),
                    self._settings_button(
                        token, "live-off", "直播推送关", "bili_live_off", 0
                    ),
                ]
            },
            {"buttons": [self._settings_button(token, "home", "返回主页", "home", 0)]},
        ]
        uids = self._bilibili_uids(entry) if entry else []
        await self._send_settings_panel(
            client,
            group_openid,
            (
                f"# {self._markdown_text(group_name)} B站推送\n"
                f"UP 主：{len(uids)} 个；"
                f"动态：{'开' if entry and entry.get('bilibili_dynamic_enabled') else '关'}；"
                f"直播：{'开' if entry and entry.get('bilibili_live_enabled') else '关'}\n"
                "UP 主 UID 请在插件 WebUI 编辑，保存后立即生效。"
            ),
            rows,
        )

    async def _send_moderation_settings(
        self,
        client: Any,
        group_openid: str,
        token: str,
        group_name: str,
    ) -> None:
        def button(
            button_id: str, label: str, action: str, style: int
        ) -> dict[str, Any]:
            return {
                "id": button_id,
                "render_data": {
                    "label": label,
                    "visited_label": label,
                    "style": style,
                },
                "action": {
                    "type": 1,
                    "permission": {"type": 1},
                    "data": f"qqgs:{token}:{action}",
                    "unsupport_tips": "当前 QQ 版本不支持设置按钮",
                },
            }

        entry = self._group_config(group_openid, required=True)
        settings = self._moderation_settings(entry)
        rows = [
            {
                "buttons": [
                    button("mod-on", "审查开启", "mod_on", 1),
                    button("mod-off", "审查关闭", "mod_off", 0),
                ]
            },
            {
                "buttons": [
                    button("ai-on", "AI开启", "ai_on", 1),
                    button("ai-off", "AI关闭", "ai_off", 0),
                ]
            },
            {
                "buttons": [
                    button("image-on", "连图开启", "image_on", 1),
                    button("image-off", "连图关闭", "image_off", 0),
                ]
            },
            {
                "buttons": [
                    button("repeat-on", "复读开启", "repeat_on", 1),
                    button("repeat-off", "复读关闭", "repeat_off", 0),
                ]
            },
            {
                "buttons": [
                    button("verify-on", "兜底验证开", "verify_on", 1),
                    button("verify-off", "兜底验证关", "verify_off", 0),
                    button("home", "返回主页", "home", 0),
                ]
            },
        ]
        text = (
            f"# {self._markdown_text(group_name)} 消息审查\n"
            f"总开关：{'开' if settings['enabled'] else '关'}；"
            f"AI（全局）：{'开' if settings['ai_enabled'] else '关'}；"
            f"连图：{'开' if settings['image_enabled'] else '关'}；"
            f"复读：{'开' if settings['repeat_enabled'] else '关'}\n"
            f"图片阈值：{settings['image_count']} 条/{settings['image_window']} 秒；"
            f"跨成员至少 {settings['image_group_min_members']} 人\n"
            f"兜底真人验证：{'开' if entry.get('fallback_human_verify_enabled') else '关'}；"
            "关键词和阈值请在插件页面配置。"
        )
        sent = await self._send_group_markdown(
            client,
            group_openid,
            text,
            keyboard={"content": {"rows": rows}},
        )
        sent_id = str(
            sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", "") or ""
        )
        if sent_id and bool(self.config.get("settings_panel_auto_recall", True)):
            self._schedule_settings_recall(client, group_openid, sent_id)

    def _schedule_settings_recall(
        self,
        client: Any,
        group_openid: str,
        message_id: str,
    ) -> None:
        self._schedule_recall(
            client,
            group_openid,
            message_id,
            SETTINGS_MESSAGE_TTL,
            "settings",
        )

    def _schedule_recall(
        self,
        client: Any,
        group_openid: str,
        message_id: str,
        delay: int,
        kind: str,
    ) -> None:
        task = asyncio.create_task(
            self._recall_message(client, group_openid, message_id, delay, kind),
            name=f"qqgroup-admin-{kind}-recall",
        )
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def _recall_settings_message(
        self,
        client: Any,
        group_openid: str,
        message_id: str,
    ) -> None:
        await self._recall_message(
            client,
            group_openid,
            message_id,
            SETTINGS_MESSAGE_TTL,
            "审核设置",
        )

    async def _recall_message(
        self,
        client: Any,
        group_openid: str,
        message_id: str,
        delay: int,
        kind: str,
    ) -> None:
        await asyncio.sleep(delay)
        try:
            await QQGroupAPI(client).recall_group_message(group_openid, message_id)
        except QQAPIError as exc:
            self.logger.warning("自动撤回%s消息失败：%s", kind, exc)

    @qq_admin_command("机器人状态")
    async def bot_state(self, event: AstrMessageEvent):
        """查询机器人在当前群的状态。"""
        _, group_openid, _ = self._context(event)
        data = await self._api(event).get_bot_state(group_openid)
        roles = {"member": "普通成员", "owner": "群主", "admin": "管理员"}
        settings = {
            "all": "全部消息",
            "only_mention": "仅提及消息",
            "mention_and_context": "提及消息及上下文",
        }
        yield event.plain_result(
            "\n".join(
                [
                    f"机器人 OpenID：{self._value(data.get('member_openid'))}",
                    f"入群时间：{self._value(data.get('joined_at'))}",
                    f"群角色：{roles.get(data.get('member_role'), self._value(data.get('member_role')))}",
                    f"接收消息：{settings.get(data.get('recv_msg_setting'), self._value(data.get('recv_msg_setting')))}",
                    f"允许主动消息：{'是' if data.get('allow_proactive_msg') else '否'}",
                ]
            )
        )

    @qq_admin_command("申请列表")
    async def join_list(
        self,
        event: AstrMessageEvent,
        cursor: str = "",
    ):
        """分页查询入群申请并发送管理员审批按钮。"""
        _, group_openid, _ = self._context(event)
        data = await self._api(event).list_join_requests(
            group_openid,
            limit=JOIN_LIST_LIMIT,
            cursor=cursor,
        )
        requests = data.get("list") or []
        if not requests:
            yield event.plain_result("当前没有待审入群申请。")
            return

        lines = [f"# 入群申请（{len(requests)} 条）"]
        rows = []
        for index, item in enumerate(requests, 1):
            member_openid = str(item.get("member_openid") or "")
            join_request_id = str(item.get("join_request_id") or "")
            lines.extend(
                [
                    f"\n## {index}\\. {self._markdown_text(item.get('username'))}",
                    f"验证：{self._markdown_text(verification_text(item))}",
                    f"风险：{self._markdown_text(item.get('risk_tips'))}",
                    f"时间：{self._markdown_text(item.get('apply_at'))}",
                ]
            )
            if not member_openid or not join_request_id:
                continue
            token = self._approval_token(
                group_openid,
                member_openid,
                join_request_id,
            )
            rows.append(
                {
                    "buttons": [
                        {
                            "id": f"approve-{index}",
                            "render_data": {
                                "label": f"同意 {index}",
                                "visited_label": f"已选择 {index}",
                                "style": 1,
                            },
                            "action": {
                                "type": 1,
                                "permission": {"type": 1},
                                "data": f"qqga:{token}:approve",
                                "unsupport_tips": "当前 QQ 版本不支持审批按钮",
                            },
                        },
                        {
                            "id": f"decline-{index}",
                            "render_data": {
                                "label": f"拒绝 {index}",
                                "visited_label": f"已选择 {index}",
                                "style": 0,
                            },
                            "action": {
                                "type": 1,
                                "permission": {"type": 1},
                                "data": f"qqga:{token}:decline",
                                "unsupport_tips": "当前 QQ 版本不支持审批按钮",
                            },
                        },
                    ]
                }
            )

        next_cursor = str(data.get("next_cursor") or "")
        if next_cursor:
            lines.append(f"\n下一页：/申请列表 {self._markdown_text(next_cursor)}")
        kwargs = {
            "group_openid": group_openid,
            "msg_type": 2,
            "markdown": {"content": "\n".join(lines)},
            "keyboard": {"content": {"rows": rows}},
        }
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if message_id:
            kwargs["msg_id"] = message_id
        try:
            await self._client(event).api.post_group_message(**kwargs)
        except Exception as exc:
            detail = self._plain_text(exc, 240)
            self.logger.warning("发送审批按钮失败：%s", exc)
            raise RuntimeError(
                "发送审批按钮失败；请确认 Markdown 和自定义按钮权限"
                f"（QQ 返回：{detail}）"
            ) from exc
        if hasattr(event, "stop_event"):
            event.stop_event()

    @qq_admin_command("禁言状态")
    async def mute_state(self, event: AstrMessageEvent):
        """查询全员规则和当前成员禁言。"""
        _, group_openid, _ = self._context(event)
        data = await self._api(event).get_mute_state(group_openid)
        global_rule = data.get("global_rule") or {}
        lines = [f"全员禁言模式：{self._value(global_rule.get('mode'))}"]
        for rule in global_rule.get("schedule_rules") or []:
            lines.append(
                "定时规则 "
                f"{self._value(rule.get('task_id'))}：{self._value(rule.get('start_at'))}"
                f" -> {self._value(rule.get('end_at'))}，"
                f"{'启用' if rule.get('enabled') else '停用'}"
            )
        for rule in global_rule.get("recurring_rules") or []:
            lines.append(
                "周期规则 "
                f"{self._value(rule.get('task_id'))}：周{self._list(rule.get('weekdays'))} "
                f"{self._value(rule.get('start_time'))}-{self._value(rule.get('end_time'))}，"
                f"{'启用' if rule.get('enabled') else '停用'}"
            )
        members = data.get("members") or []
        lines.append(f"当前成员禁言：{len(members)} 人")
        for member in members:
            lines.append(
                f"- {self._value(member.get('username'))} "
                f"({self._value(member.get('member_openid'))}) "
                f"至 {self._value(member.get('mute_expire_at'))}"
            )
        for result in self._results(event, "\n".join(lines)):
            yield result

    async def _set_mute(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
    ) -> tuple[str, str]:
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        api = self._api(event)
        state = await api.get_mute_state(group_openid)
        op = (
            "update"
            if any(
                str(item.get("member_openid") or "") == member
                for item in state.get("members") or []
            )
            else "add"
        )
        expire_at = future_rfc3339(parse_duration(duration))
        await api.set_member_mutes(
            group_openid,
            [{"op": op, "member_openid": member, "mute_expire_at": expire_at}],
        )
        return member, expire_at

    async def _send_mute_success(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
        expire_at: str,
    ) -> Any | None:
        template = str(
            self.config.get(
                "mute_success_message",
                "已设置禁言，至 {expire_at}。",
            )
            or "已设置禁言，至 {expire_at}。"
        )
        legacy_at = bool(self.config.get("mute_reply_at_member", False))
        has_at_variable = "{at_user}" in template
        if legacy_at and not has_at_variable:
            template = "{at_user} " + template
            has_at_variable = True
        template = (
            template.replace("{duration}", duration)
            .replace("{expire_at}", expire_at)
            .replace("{member_openid}", member_openid)
        )
        if not has_at_variable and not legacy_at:
            return event.plain_result(template[:1000])

        if not template.strip():
            return None

        _, group_openid, _ = self._context(event)
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if hasattr(event, "stop_event"):
            event.stop_event()
        try:
            await self._send_group_notice(
                self._client(event),
                group_openid,
                template,
                member_openid=member_openid,
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001 - mute already succeeded
            self.logger.warning("发送禁言成功回复失败：%s", exc)
        return None

    @qq_admin_command("禁言")
    async def mute_member(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
    ):
        """新增或更新成员禁言，最长 30 天。"""
        member, expire_at = await self._set_mute(event, member_openid, duration)
        result = await self._send_mute_success(
            event,
            member,
            duration,
            expire_at,
        )
        if result is not None:
            yield result

    @qq_admin_regex(r"^/?禁言(?=<@!?[^>]+>|@\S+)")
    async def mute_member_compact(self, event: AstrMessageEvent):
        """兼容命令与 @成员 之间不留空格。"""
        match = re.fullmatch(
            r"/?禁言(?:<@!?[^>]+>|@\S+)\s+(\S+)",
            event.get_message_str().strip(),
        )
        if not match:
            raise ValueError("用法：/禁言@成员 <60|30m|2h|7d>")
        member, expire_at = await self._set_mute(event, "@", match.group(1))
        result = await self._send_mute_success(
            event,
            member,
            match.group(1),
            expire_at,
        )
        if hasattr(event, "stop_event"):
            event.stop_event()
        if result is not None:
            yield result

    @qq_admin_command("解禁")
    async def mute_remove(self, event: AstrMessageEvent, member_openid: str = ""):
        """立即解除成员禁言。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid or "@")
        await self._api(event).set_member_mutes(
            group_openid,
            [{"op": "del", "member_openid": member, "mute_expire_at": ""}],
        )
        yield event.plain_result("已解除禁言。")

    @qq_admin_regex(r"^/?解禁(?=<@!?[^>]+>|@\S+)")
    async def mute_remove_compact(self, event: AstrMessageEvent):
        """兼容命令与 @成员 之间不留空格。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, "@")
        await self._api(event).set_member_mutes(
            group_openid,
            [{"op": "del", "member_openid": member, "mute_expire_at": ""}],
        )
        if hasattr(event, "stop_event"):
            event.stop_event()
        yield event.plain_result("已解除禁言。")

    async def _recall_recent(
        self,
        event: AstrMessageEvent,
        target_or_count: str,
        count: str = "",
    ) -> str:
        _, group_openid, _ = self._context(event)
        value = str(target_or_count or "1").strip()
        member_openid = ""
        if count:
            member_openid = self._target_member(event, value)
            requested = self._recall_count(count)
        elif value.isdigit():
            requested = self._recall_count(value)
        else:
            member_openid = self._target_member(event, value)
            requested = 1
        current_message_id = str(
            getattr(event.message_obj, "message_id", "") or ""
        )
        message_ids = self._moderation.newest_message_ids(
            group_openid,
            requested,
            member_openid=member_openid,
            exclude_message_id=current_message_id,
        )
        if not message_ids:
            return (
                "缓存内没有可撤回的消息。QQ 官方只允许撤回最近 2 分钟内机器人"
                "实际收到的普通成员消息；请确认已开启“接收所有群消息”。"
            )
        failed = await self._recall_messages(
            self._api(event),
            group_openid,
            message_ids,
        )
        succeeded = len(message_ids) - len(failed)
        missing = requested - len(message_ids)
        scope = "该成员" if member_openid else "本群"
        parts = [f"已撤回{scope}最近 {succeeded} 条消息"]
        if failed:
            parts.append(f"{len(failed)} 条撤回失败")
        if missing:
            parts.append(f"缓存不足 {missing} 条")
        return "；".join(parts) + "。"

    @qq_admin_command("撤回")
    async def recall_recent_messages(
        self,
        event: AstrMessageEvent,
        target_or_count: str = "1",
        count: str = "",
    ):
        """撤回本群或指定普通成员最近收到的消息。"""
        yield event.plain_result(
            await self._recall_recent(event, target_or_count, count)
        )

    @qq_admin_regex(r"^/?撤回(?=<@!?[^>]+>|@\S+)")
    async def recall_recent_messages_compact(self, event: AstrMessageEvent):
        """兼容命令与 @成员 之间不留空格。"""
        match = re.fullmatch(
            r"/?撤回(?:<@!?[^>]+>|@\S+)(?:\s+(\d+))?",
            event.get_message_str().strip(),
        )
        if not match:
            raise ValueError("用法：/撤回@成员 [1-50]")
        yield event.plain_result(
            await self._recall_recent(event, "@", match.group(1) or "1")
        )

    async def _whole_mute_capability(self, event: AstrMessageEvent) -> str:
        _, group_openid, _ = self._context(event)
        state = await self._api(event).get_mute_state(group_openid)
        mode = str((state.get("global_rule") or {}).get("mode") or "-")
        return (
            "未执行：QQ 官方群 OpenAPI 当前只开放全员禁言规则查询，"
            f"没有写入全体禁言或解禁的接口。当前全员模式：{mode}。"
        )

    @qq_admin_command("全体禁言")
    async def mute_all(self, event: AstrMessageEvent):
        """报告 QQ 官方群接口的全员禁言写入能力。"""
        yield event.plain_result(await self._whole_mute_capability(event))

    @qq_admin_command("全体解禁")
    async def unmute_all(self, event: AstrMessageEvent):
        """报告 QQ 官方群接口的全员解禁写入能力。"""
        yield event.plain_result(await self._whole_mute_capability(event))

    async def _auto_strategy(
        self,
        event: AstrMessageEvent,
        *,
        required: bool,
    ) -> tuple[QQGroupAPI, str, dict[str, Any] | None]:
        _, group_openid, _ = self._context(event)
        api = self._api(event)
        data = await api.list_strategies(limit=100)
        strategy = select_group_strategy(
            data.get("strategies") or [],
            group_openid,
        )
        if required and strategy is None:
            raise ValueError("当前群尚未开启自动审核，请先使用 /自动审核开启")
        return api, group_openid, strategy

    async def _scan_pending(self, api: QQGroupAPI, strategy_id: str) -> str:
        try:
            await api.execute_strategy(strategy_id)
        except QQAPIError as exc:
            return f"\n白名单已保存，但待审申请扫描未启动：{exc}"
        return "\n已启动待审申请扫描，QQ 官方预计约 10 分钟完成。"

    async def _sync_group_config(
        self,
        client: Any,
        group_openid: str,
        entry: dict[str, Any],
        platform_id: str,
        *,
        native_enabled: bool | None = None,
        uid_enabled: bool | None = None,
    ) -> str:
        native_enabled = (
            bool(entry.get("enabled", False))
            if native_enabled is None
            else native_enabled
        )
        uid_enabled = (
            self._condition_settings(entry)["enabled"]
            if uid_enabled is None
            else uid_enabled
        )
        if uid_enabled and native_enabled:
            raise ValueError(
                "QQ 号码白名单会绕过 UID 和关键词检查，两种自动审核不能同时启用"
            )

        api = QQGroupAPI(client)
        if native_enabled or uid_enabled:
            state = await api.get_bot_state(group_openid)
            role = str(state.get("member_role") or "unknown")
            if role not in GROUP_ADMIN_ROLES:
                raise RuntimeError(
                    f"QQ 返回机器人在当前群的角色为 {role}，"
                    "启用自动审核需要 admin 或 owner"
                )
        data = await api.list_strategies(limit=100)
        strategy = select_group_strategy(data.get("strategies") or [], group_openid)
        strategy_id = self._strategy_id(strategy) if strategy else ""
        managed_id = str(entry.get("managed_strategy_id") or "")
        if strategy is not None and managed_id != strategy_id:
            raise ValueError(
                "当前群已有未由本插件管理的 QQ 官方策略，不能自动接管或删除"
            )

        entry["enabled"] = native_enabled
        entry["uid_review_enabled"] = uid_enabled
        entry["platform_id"] = platform_id
        if not native_enabled:
            if strategy is not None:
                await api.delete_strategy(strategy_id)
            entry["managed_strategy_id"] = ""
            entry["applied_whitelist"] = ""
            self.config.save_config()
            return (
                "条件审核已启用。"
                if uid_enabled
                else "两种自动审核均已关闭，群绑定已保留。"
            )

        desired = parse_qq_number_text(str(entry.get("whitelist_qq_numbers") or ""))
        if strategy is None:
            strategy = await api.create_strategy(
                group_openids=[group_openid],
                is_enable="on",
                remark="AstrBot WebUI 自动审核",
            )
            strategy_id = self._strategy_id(strategy)
            self._save_group_config(entry, strategy_id, [])
        else:
            await api.update_strategy(strategy_id, {"is_enable": "on"})

        applied = parse_qq_number_text(str(entry.get("applied_whitelist") or ""))
        additions, removals = whitelist_diff(desired, applied)
        current = list(applied)
        for start in range(0, len(removals), 10_000):
            batch = removals[start : start + 10_000]
            await api.update_whitelist(strategy_id, op="del", users=batch)
            removed = set(batch)
            current = [user for user in current if user not in removed]
            self._save_group_config(entry, strategy_id, current)
        for start in range(0, len(additions), 10_000):
            batch = additions[start : start + 10_000]
            await api.update_whitelist(strategy_id, op="add", users=batch)
            current.extend(batch)
            self._save_group_config(entry, strategy_id, current)
        scan_result = (
            await self._scan_pending(api, strategy_id)
            if bool(entry.get("scan_pending", True)) and desired
            else ""
        )
        self._save_group_config(entry, strategy_id, desired)
        return (
            f"QQ 号码白名单已同步：{len(desired)} 人，"
            f"新增 {len(additions)} 人，移除 {len(removals)} 人。{scan_result}"
        )

    async def _apply_settings_button(
        self,
        client: Any,
        group_openid: str,
        platform_id: str,
        action: str,
        group_name: str,
    ) -> None:
        entry = await self._bind_group(
            client,
            group_openid,
            platform_id,
            group_name,
        )
        if action == "bind":
            return
        updates = {
            "uid_on": ("uid_check_enabled", True),
            "uid_off": ("uid_check_enabled", False),
            "direct_on": ("uid_exists_auto_approve", True),
            "direct_off": ("uid_exists_auto_approve", False),
            "all": ("condition_logic", "all"),
            "any": ("condition_logic", "any"),
            "pending": ("fallback_action", "pending"),
            "decline": ("fallback_action", "decline"),
            "approve": ("fallback_action", "approve"),
            "mod_on": ("moderation_enabled", True),
            "mod_off": ("moderation_enabled", False),
            "image_on": ("image_spam_enabled", True),
            "image_off": ("image_spam_enabled", False),
            "repeat_on": ("repeat_review_enabled", True),
            "repeat_off": ("repeat_review_enabled", False),
            "verify_on": ("fallback_human_verify_enabled", True),
            "verify_off": ("fallback_human_verify_enabled", False),
            "bili_dynamic_on": ("bilibili_dynamic_enabled", True),
            "bili_dynamic_off": ("bilibili_dynamic_enabled", False),
            "bili_live_on": ("bilibili_live_enabled", True),
            "bili_live_off": ("bilibili_live_enabled", False),
        }
        if action in updates:
            if action in {
                "bili_dynamic_on",
                "bili_live_on",
            } and not self._bilibili_uids(entry):
                raise ValueError("请先在插件 WebUI 配置 B站 UP 主 UID")
            key, value = updates[action]
            entry[key] = value
            if action == "uid_off":
                entry["uid_exists_auto_approve"] = False
            elif action == "direct_on":
                entry["uid_check_enabled"] = True
            self.config.save_config()
            return
        if action in {"ai_on", "ai_off"}:
            self.config[GLOBAL_AI_ENABLED_KEY] = action == "ai_on"
            self.config.save_config()
            return
        if action in {"uid", "conditional"}:
            await self._sync_group_config(
                client,
                group_openid,
                entry,
                platform_id,
                native_enabled=False,
                uid_enabled=True,
            )
        elif action == "native":
            await self._sync_group_config(
                client,
                group_openid,
                entry,
                platform_id,
                native_enabled=True,
                uid_enabled=False,
            )
        elif action == "off":
            await self._sync_group_config(
                client,
                group_openid,
                entry,
                platform_id,
                native_enabled=False,
                uid_enabled=False,
            )
        else:
            await self._sync_group_config(
                client,
                group_openid,
                entry,
                platform_id,
            )

    def _web_group(self, entry: dict[str, Any]) -> dict[str, Any]:
        group_openid = str(entry.get("group_openid") or "").strip()
        group_name = str(entry.get("group_name") or "").strip()
        native_enabled = bool(entry.get("enabled", False))
        settings = self._condition_settings(entry)
        moderation = self._moderation_settings(entry)
        condition_enabled = settings["enabled"]
        mode = (
            "native"
            if native_enabled
            else "conditional"
            if condition_enabled
            else "off"
        )
        bound = bool(entry.get("platform_id"))
        managed = bool(entry.get("managed_strategy_id"))
        desired = parse_qq_number_text(str(entry.get("whitelist_qq_numbers") or ""))
        applied = parse_qq_number_text(str(entry.get("applied_whitelist") or ""))
        synchronized = (
            bound and managed and desired == applied
            if mode == "native"
            else bound and not managed
            if mode == "conditional"
            else not managed
        )
        keyword_replies = entry.get("keyword_replies")
        if not isinstance(keyword_replies, list):
            keyword_replies = []
        return {
            "group_name": group_name or f"未绑定群 {group_openid[:8]}",
            "group_openid": group_openid,
            "mode": mode,
            "bound": bound,
            "synchronized": synchronized,
            "whitelist_qq_numbers": "\n".join(desired),
            "uid_check_enabled": settings["uid_check_enabled"],
            "uid_exists_auto_approve": settings["uid_exists_auto_approve"],
            "approve_keywords": "\n".join(settings["approve_keywords"]),
            "reject_keywords": "\n".join(settings["reject_keywords"]),
            "condition_logic": settings["condition_logic"],
            "fallback_action": settings["fallback_action"],
            "fallback_human_verify_enabled": settings["fallback_human_verify_enabled"],
            "moderation_enabled": moderation["enabled"],
            "moderation_exempt_admins": moderation["exempt_admins"],
            "member_blacklist": "\n".join(moderation["member_blacklist"]),
            "member_whitelist": "\n".join(moderation["member_whitelist"]),
            "blacklist_reply": moderation["blacklist_reply"],
            "blacklist_at_member": moderation["blacklist_at"],
            "message_reject_keywords": "\n".join(moderation["keywords"]),
            "message_reject_reply": moderation["keyword_reply"],
            "message_reject_at_member": moderation["keyword_at"],
            "ai_review_enabled": moderation["ai_enabled"],
            "ai_review_provider_id": moderation["ai_provider_id"],
            "ai_review_fallback_provider_ids": list(
                moderation["ai_fallback_provider_ids"]
            ),
            # Legacy clients expect one fallback field; expose the first only.
            "ai_review_fallback_provider_id": (
                moderation["ai_fallback_provider_ids"][0]
                if moderation["ai_fallback_provider_ids"]
                else ""
            ),
            "image_keyword_review_enabled": moderation["image_keyword_enabled"],
            "image_reject_keywords": "\n".join(moderation["image_keywords"]),
            "image_reject_reply": moderation["image_keyword_reply"],
            "image_reject_at_member": moderation["image_keyword_at"],
            "image_spam_enabled": moderation["image_enabled"],
            "image_spam_count": moderation["image_count"],
            "image_spam_window_seconds": moderation["image_window"],
            "image_spam_group_min_members": moderation[
                "image_group_min_members"
            ],
            "image_spam_recall_count": moderation["image_recall_count"],
            "image_spam_reply": moderation["image_spam_reply"],
            "image_spam_at_member": moderation["image_spam_at"],
            "repeat_review_enabled": moderation["repeat_enabled"],
            "repeat_count": moderation["repeat_count"],
            "repeat_window_seconds": moderation["repeat_window"],
            "repeat_mute_min_seconds": moderation["repeat_mute_min"],
            "repeat_mute_max_seconds": moderation["repeat_mute_max"],
            "repeat_reply": moderation["repeat_reply"],
            "repeat_at_member": moderation["repeat_at"],
            "bilibili_uids": "\n".join(self._bilibili_uids(entry)),
            "bilibili_dynamic_enabled": bool(
                entry.get("bilibili_dynamic_enabled", False)
            ),
            "bilibili_live_enabled": bool(entry.get("bilibili_live_enabled", False)),
            "keyword_replies": keyword_replies,
            "scan_pending": bool(entry.get("scan_pending", True)),
            "button_reject_reason": str(
                entry.get("button_reject_reason") or "管理员拒绝"
            ),
        }

    async def web_groups(self) -> list[dict[str, Any]]:
        entries = self.config.get("auto_review_groups") or []
        if not isinstance(entries, list):
            raise TypeError("WebUI 自动审核配置格式错误")
        return [
            self._web_group(entry)
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("group_openid") or "").strip()
        ]

    async def web_global_keyword_replies(self) -> dict[str, Any]:
        raw_rules = self.config.get("global_keyword_replies") or []
        if not isinstance(raw_rules, list):
            raise TypeError("WebUI 全局关键词回复配置格式错误")
        rules = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                continue
            keywords = raw.get("keywords", raw.get("keyword", ""))
            if isinstance(keywords, list):
                keywords = "\n".join(str(item) for item in keywords)
            groups = raw.get("group_openids", [])
            if isinstance(groups, str):
                groups = [
                    value
                    for value in re.split(r"[\s,，;；]+", groups.strip())
                    if value and value != "*"
                ]
            rules.append(
                {
                    "name": str(raw.get("name") or raw.get("keyword") or "未命名规则"),
                    "keywords": str(keywords or ""),
                    "condition_logic": str(
                        raw.get("condition_logic") or raw.get("keyword_logic") or "any"
                    ),
                    "reply": str(raw.get("reply") or ""),
                    "match_type": str(raw.get("match_type") or "contains"),
                    "group_openids": groups if isinstance(groups, list) else [],
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
        return {
            "rules": rules,
            "keyword_reply_cooldown_seconds": self._bounded_int(
                self.config.get("keyword_reply_cooldown_seconds"), 0, 0, 3_600
            ),
            "keyword_reply_recall_seconds": self._bounded_int(
                self.config.get("keyword_reply_recall_seconds"), 0, 0, 120
            ),
        }

    async def web_save_global_keyword_replies(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        self.config["global_keyword_replies"] = list(settings["rules"])
        self.config["keyword_reply_cooldown_seconds"] = int(
            settings["keyword_reply_cooldown_seconds"]
        )
        self.config["keyword_reply_recall_seconds"] = int(
            settings["keyword_reply_recall_seconds"]
        )
        self.config.save_config()
        return await self.web_global_keyword_replies()

    async def web_runtime_settings(self) -> dict[str, Any]:
        cookie = str(self.config.get("bilibili_cookie") or "")
        providers = []
        try:
            configured = self.context.get_all_providers()
        except Exception as exc:  # noqa: BLE001 - WebUI remains usable without AI
            self.logger.debug("读取 AI 提供商列表失败：%s", exc)
            configured = []
        for provider in configured:
            try:
                meta = provider.meta()
                provider_id = str(getattr(meta, "id", "") or "").strip()
                if not provider_id:
                    continue
                model = str(getattr(meta, "model", "") or "").strip()
                provider_type = str(getattr(meta, "type", "") or "").strip()
                providers.append(
                    {
                        "id": provider_id,
                        "model": model,
                        "type": provider_type,
                        "label": f"{model} ({provider_id})" if model else provider_id,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - skip malformed providers
                self.logger.debug("忽略无法读取的 AI 提供商：%s", exc)
        return {
            "uid_review_interval_seconds": self._bounded_int(
                self.config.get("uid_review_interval_seconds"), 60, 15, 600
            ),
            "mute_success_message": str(self.config.get("mute_success_message") or ""),
            "settings_panel_auto_recall": bool(
                self.config.get("settings_panel_auto_recall", True)
            ),
            "settings_command_enabled": bool(
                self.config.get("settings_command_enabled", True)
            ),
            "global_reject_keywords": str(
                self.config.get("global_reject_keywords") or ""
            ),
            "global_message_reject_keywords": str(
                self.config.get("global_message_reject_keywords") or ""
            ),
            "global_message_reject_reply": str(
                self.config.get("global_message_reject_reply") or ""
            ),
            "global_message_reject_at_member": bool(
                self.config.get("global_message_reject_at_member", True)
            ),
            "global_member_blacklist": str(
                self.config.get("global_member_blacklist") or ""
            ),
            "global_member_whitelist": str(
                self.config.get("global_member_whitelist") or ""
            ),
            "global_blacklist_reply": str(
                self.config.get("global_blacklist_reply") or ""
            ),
            "global_blacklist_at_member": bool(
                self.config.get("global_blacklist_at_member", True)
            ),
            "global_ai_review_enabled": bool(
                self.config.get(GLOBAL_AI_ENABLED_KEY, False)
            ),
            "global_ai_review_provider_id": str(
                self.config.get(GLOBAL_AI_PROVIDER_KEY) or ""
            ).strip(),
            "global_ai_review_fallback_provider_ids": normalize_provider_ids(
                self.config.get(GLOBAL_AI_FALLBACKS_KEY)
            ),
            "global_ai_review_confirm_provider_id": str(
                self.config.get(GLOBAL_AI_CONFIRM_PROVIDER_KEY) or ""
            ).strip(),
            "global_ai_review_timeout_seconds": self._bounded_int(
                self.config.get(GLOBAL_AI_TIMEOUT_KEY),
                AI_REVIEW_TOTAL_TIMEOUT_SECONDS,
                5,
                120,
            ),
            "global_ai_review_images_enabled": bool(
                self.config.get(GLOBAL_AI_IMAGES_KEY, False)
            ),
            "global_ai_review_block_threshold": self._bounded_int(
                self.config.get(GLOBAL_AI_BLOCK_THRESHOLD_KEY),
                AI_REVIEW_DEFAULT_BLOCK_THRESHOLD,
                50,
                100,
            ),
            "global_ai_review_action": (
                str(self.config.get(GLOBAL_AI_ACTION_KEY) or "record_only")
                if str(self.config.get(GLOBAL_AI_ACTION_KEY) or "record_only")
                in AI_REVIEW_ACTIONS
                else "record_only"
            ),
            "global_ai_reject_reply": str(
                self.config.get("global_ai_reject_reply") or ""
            ),
            "global_ai_reject_at_member": bool(
                self.config.get("global_ai_reject_at_member", True)
            ),
            "global_image_reject_keywords": str(
                self.config.get(GLOBAL_IMAGE_KEYWORDS_KEY) or ""
            ),
            "global_image_reject_reply": str(
                self.config.get("global_image_reject_reply") or ""
            ),
            "global_image_reject_at_member": bool(
                self.config.get("global_image_reject_at_member", True)
            ),
            "global_image_ocr_enabled": bool(
                self.config.get(GLOBAL_IMAGE_OCR_ENABLED_KEY, False)
            ),
            "global_image_ocr_provider_id": str(
                self.config.get(GLOBAL_IMAGE_OCR_PROVIDER_KEY) or ""
            ).strip(),
            "global_image_ocr_timeout_seconds": self._bounded_int(
                self.config.get(GLOBAL_IMAGE_OCR_TIMEOUT_KEY),
                IMAGE_OCR_DEFAULT_TIMEOUT_SECONDS,
                2,
                30,
            ),
            "global_image_ocr_max_images": self._bounded_int(
                self.config.get(GLOBAL_IMAGE_OCR_MAX_IMAGES_KEY),
                IMAGE_OCR_DEFAULT_MAX_IMAGES,
                1,
                3,
            ),
            # Compatibility aliases for older custom pages; behavior remains global.
            "ai_review_enabled": bool(self.config.get(GLOBAL_AI_ENABLED_KEY, False)),
            "ai_review_provider_id": str(
                self.config.get(GLOBAL_AI_PROVIDER_KEY) or ""
            ).strip(),
            "ai_review_fallback_provider_ids": normalize_provider_ids(
                self.config.get(GLOBAL_AI_FALLBACKS_KEY)
            ),
            "ai_review_fallback_provider_id": (
                normalize_provider_ids(self.config.get(GLOBAL_AI_FALLBACKS_KEY))[0]
                if normalize_provider_ids(self.config.get(GLOBAL_AI_FALLBACKS_KEY))
                else ""
            ),
            "bilibili_live_interval_seconds": self._bounded_int(
                self.config.get("bilibili_live_interval_seconds"), 60, 30, 600
            ),
            "bilibili_dynamic_interval_seconds": self._bounded_int(
                self.config.get("bilibili_dynamic_interval_seconds"), 180, 60, 3_600
            ),
            "bilibili_logged_in": "SESSDATA=" in cookie and "bili_jct=" in cookie,
            "providers": providers,
        }

    async def web_save_runtime_settings(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        ai_keys = {
            "global_ai_review_enabled",
            "global_ai_review_provider_id",
            "global_ai_review_fallback_provider_ids",
            "global_ai_review_confirm_provider_id",
            "ai_review_enabled",
            "ai_review_provider_id",
            "ai_review_fallback_provider_ids",
            "ai_review_fallback_provider_id",
        }
        if ai_keys.intersection(settings):
            settings = dict(settings)
            primary = str(
                settings.get(
                    "global_ai_review_provider_id",
                    settings.get(
                        "ai_review_provider_id",
                        self.config.get(GLOBAL_AI_PROVIDER_KEY),
                    ),
                )
                or ""
            ).strip()
            fallback_ids = normalize_provider_ids(
                settings.get(
                    "global_ai_review_fallback_provider_ids",
                    settings.get(
                        "ai_review_fallback_provider_ids",
                        settings.get(
                            "ai_review_fallback_provider_id",
                            self.config.get(GLOBAL_AI_FALLBACKS_KEY),
                        ),
                    ),
                )
            )
            confirm_provider = str(
                settings.get(
                    "global_ai_review_confirm_provider_id",
                    self.config.get(GLOBAL_AI_CONFIRM_PROVIDER_KEY),
                )
                or ""
            ).strip()
            if primary in fallback_ids:
                raise ValueError("AI 审核主模型不能出现在回退模型列表")
            if confirm_provider and (
                confirm_provider == primary or confirm_provider in fallback_ids
            ):
                raise ValueError("AI 二次确认模型不能与主模型或回退模型重复")
            settings["global_ai_review_provider_id"] = primary
            settings["global_ai_review_fallback_provider_ids"] = fallback_ids
            settings["global_ai_review_confirm_provider_id"] = confirm_provider
            if "global_ai_review_enabled" not in settings:
                settings["global_ai_review_enabled"] = bool(
                    settings.get(
                        "ai_review_enabled",
                        self.config.get(GLOBAL_AI_ENABLED_KEY, False),
                    )
                )
        self.config.update(settings)
        self.config.save_config()
        return await self.web_runtime_settings()

    @staticmethod
    def _qr_data_url(value: str) -> str:
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage
        except ImportError as exc:
            raise RuntimeError("AstrBot 缺少 qrcode 组件，无法生成登录二维码") from exc
        output = BytesIO()
        qrcode.make(
            value,
            image_factory=SvgPathImage,
            box_size=8,
            border=2,
        ).save(output)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"

    async def web_bilibili_login_start(self) -> dict[str, Any]:
        now = time.monotonic()
        self._bilibili_logins = {
            key: login
            for key, login in self._bilibili_logins.items()
            if login.expires_at > now
        }
        login = await asyncio.to_thread(start_qr_login)
        self._bilibili_logins[login.qrcode_key] = login
        return {
            "qrcode_key": login.qrcode_key,
            "qr_image": await asyncio.to_thread(self._qr_data_url, login.url),
            "expires_in": max(0, int(login.expires_at - time.monotonic())),
        }

    async def web_bilibili_login_poll(self, qrcode_key: str) -> dict[str, Any]:
        login = self._bilibili_logins.get(qrcode_key)
        if login is None:
            raise LookupError("二维码登录已失效，请重新生成")
        status, cookie = await asyncio.to_thread(poll_qr_login, login)
        if status == "confirmed":
            self.config["bilibili_cookie"] = cookie
            self.config.save_config()
            self._bilibili_logins.pop(qrcode_key, None)
        elif status == "expired":
            self._bilibili_logins.pop(qrcode_key, None)
        return {"status": status, "bilibili_logged_in": status == "confirmed"}

    async def web_save_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        group_openid = str(payload["group_openid"])
        entry = self._group_config(group_openid, required=True)
        self._update_web_group(entry, payload)
        self.config.save_config()
        return self._web_group(entry)

    @staticmethod
    def _update_web_group(
        entry: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        mode = str(payload["mode"])
        entry.update(
            {
                "enabled": mode == "native",
                "uid_review_enabled": mode == "conditional",
                "whitelist_qq_numbers": str(payload["whitelist_qq_numbers"]),
                "uid_check_enabled": bool(payload["uid_check_enabled"]),
                "uid_exists_auto_approve": bool(payload["uid_exists_auto_approve"]),
                "approve_keywords": str(payload["approve_keywords"]),
                "reject_keywords": str(payload["reject_keywords"]),
                "condition_logic": str(payload["condition_logic"]),
                "fallback_action": str(payload["fallback_action"]),
                "scan_pending": bool(payload["scan_pending"]),
                "button_reject_reason": str(payload["button_reject_reason"]),
                "fallback_human_verify_enabled": bool(
                    payload["fallback_human_verify_enabled"]
                ),
                "moderation_enabled": bool(payload["moderation_enabled"]),
                "moderation_exempt_admins": bool(payload["moderation_exempt_admins"]),
                "member_blacklist": str(payload["member_blacklist"]),
                "member_whitelist": str(payload["member_whitelist"]),
                "blacklist_reply": str(payload["blacklist_reply"]),
                "blacklist_at_member": bool(payload["blacklist_at_member"]),
                "message_reject_keywords": str(payload["message_reject_keywords"]),
                "message_reject_reply": str(payload["message_reject_reply"]),
                "message_reject_at_member": bool(payload["message_reject_at_member"]),
                "image_keyword_review_enabled": bool(
                    payload["image_keyword_review_enabled"]
                ),
                "image_reject_keywords": str(payload["image_reject_keywords"]),
                "image_reject_reply": str(payload["image_reject_reply"]),
                "image_reject_at_member": bool(payload["image_reject_at_member"]),
                "image_spam_enabled": bool(payload["image_spam_enabled"]),
                "image_spam_count": int(payload["image_spam_count"]),
                "image_spam_window_seconds": int(payload["image_spam_window_seconds"]),
                "image_spam_group_min_members": int(
                    payload["image_spam_group_min_members"]
                ),
                "image_spam_recall_count": int(payload["image_spam_recall_count"]),
                "image_spam_reply": str(payload["image_spam_reply"]),
                "image_spam_at_member": bool(payload["image_spam_at_member"]),
                "repeat_review_enabled": bool(payload["repeat_review_enabled"]),
                "repeat_count": int(payload["repeat_count"]),
                "repeat_window_seconds": int(payload["repeat_window_seconds"]),
                "repeat_mute_min_seconds": int(payload["repeat_mute_min_seconds"]),
                "repeat_mute_max_seconds": int(payload["repeat_mute_max_seconds"]),
                "repeat_reply": str(payload["repeat_reply"]),
                "repeat_at_member": bool(payload["repeat_at_member"]),
                "bilibili_uids": str(payload["bilibili_uids"]),
                "bilibili_dynamic_enabled": bool(payload["bilibili_dynamic_enabled"]),
                "bilibili_live_enabled": bool(payload["bilibili_live_enabled"]),
                "keyword_replies": list(payload["keyword_replies"]),
            }
        )

    def _identity_items(
        self,
        kind: str,
        groups_by_id: dict[str, str],
    ) -> list[dict[str, Any]]:
        if kind == "bindings":
            items = []
            for binding in self._uid_bindings.values():
                item = dict(binding)
                item["group_names"] = [
                    groups_by_id.get(str(group_id), str(group_id))
                    for group_id in item.get("groups") or []
                ]
                items.append(item)
            items.sort(key=lambda item: str(item.get("uid") or ""))
            return items
        if kind == "suspicious":
            return sorted(
                (
                    {
                        **dict(item),
                        "group_name": groups_by_id.get(
                            str(item.get("group_openid") or ""), ""
                        ),
                    }
                    for item in self._suspicious_members.values()
                ),
                key=lambda item: int(item.get("created_at") or 0),
                reverse=True,
            )
        if kind == "violations":
            items = []
            for record in self._violation_records:
                item = dict(record)
                item["group_name"] = item.get("group_name") or groups_by_id.get(
                    str(item.get("group_openid") or ""), ""
                )
                items.append(item)
            items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
            return items
        raise ValueError("身份记录类型无效")

    @staticmethod
    def _identity_matches(item: dict[str, Any], query: str) -> bool:
        needle = query.casefold()
        fields = (
            "uid",
            "bilibili_uid",
            "username",
            "member_name",
            "identity",
            "member_openid",
            "qq_openid",
            "openid",
            "union_openid",
            "group_openid",
            "group_name",
            "group",
            "groups",
            "group_names",
            "members",
            "last_violation_group",
            "last_violation_reason",
            "last_violation_content",
            "reason",
            "rule",
            "category",
            "content",
            "message",
            "message_content",
            "message_summary",
        )
        values: list[Any] = [item.get(field) for field in fields]
        while values:
            value = values.pop()
            if isinstance(value, dict):
                values.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                values.extend(value)
            elif value is not None and needle in str(value).casefold():
                return True
        return False

    def _identity_groups_by_id(self) -> dict[str, str]:
        return {
            str(item.get("group_openid") or ""): str(item.get("group_name") or "")
            for item in (self.config.get("auto_review_groups") or [])
            if isinstance(item, dict)
        }

    async def web_identities(self) -> dict[str, list[dict[str, Any]]]:
        groups_by_id = self._identity_groups_by_id()
        bindings = self._identity_items("bindings", groups_by_id)
        suspicious = self._identity_items("suspicious", groups_by_id)
        violations = self._identity_items("violations", groups_by_id)
        return {
            "bindings": bindings,
            "suspicious": suspicious,
            "violations": violations,
            "violation_records": violations,
        }

    async def web_identity_page(
        self,
        kind: str,
        query: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        if kind not in {"bindings", "suspicious", "violations"}:
            raise ValueError("身份记录类型无效")
        if page < 1 or page_size not in {10, 20, 50}:
            raise ValueError("身份记录分页参数无效")
        query = str(query or "").strip()
        if len(query) > 256:
            raise ValueError("身份记录搜索词最多 256 个字符")
        items = self._identity_items(kind, self._identity_groups_by_id())
        if query:
            items = [item for item in items if self._identity_matches(item, query)]
        total = len(items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return {
            "kind": kind,
            "items": items[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    async def web_violation_export(self, query: str) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if len(query) > 256:
            raise ValueError("身份记录搜索词最多 256 个字符")
        items = self._identity_items("violations", self._identity_groups_by_id())
        if query:
            items = [item for item in items if self._identity_matches(item, query)]
        return items

    async def web_delete_binding(self, uid: str) -> dict[str, str]:
        if self._uid_bindings.pop(uid, None) is None:
            raise LookupError("找不到该 UID 绑定")
        await self._save_state()
        return {"uid": uid}

    async def web_clear_suspicious(
        self,
        group_openid: str,
        member_openid: str,
    ) -> dict[str, str]:
        key = self._member_state_key(group_openid, member_openid)
        if self._suspicious_members.pop(key, None) is None:
            raise LookupError("找不到该待验证成员")
        await self._save_state()
        return {"group_openid": group_openid, "member_openid": member_openid}

    async def web_batch_save(
        self,
        payloads: list[dict[str, Any]],
    ) -> list[str]:
        entries = [
            self._group_config(str(payload["group_openid"]), required=True)
            for payload in payloads
        ]
        for entry, payload in zip(entries, payloads, strict=True):
            self._update_web_group(entry, payload)
        self.config.save_config()
        return [str(payload["group_openid"]) for payload in payloads]

    async def web_sync_group(self, group_openid: str) -> dict[str, Any]:
        entry = self._group_config(group_openid, required=True)
        platform_id = str(entry.get("platform_id") or "")
        client = self._platform_clients().get(platform_id)
        if client is None:
            raise RuntimeError("请先在目标群发送 /审核设置 并点击绑定此群")
        message = await self._sync_group_config(
            client,
            group_openid,
            entry,
            platform_id,
        )
        result = self._web_group(entry)
        result["result"] = message
        return result

    async def web_batch_sync(self, group_openids: list[str]) -> list[dict[str, Any]]:
        results = []
        for group_openid in group_openids:
            try:
                await self.web_sync_group(group_openid)
            except (QQAPIError, TypeError, ValueError, RuntimeError) as exc:
                results.append(
                    {
                        "group_openid": group_openid,
                        "ok": False,
                        "error": str(exc)[:240],
                    }
                )
            except Exception:
                self.logger.exception("批量应用群审核配置失败：%s", group_openid)
                results.append(
                    {
                        "group_openid": group_openid,
                        "ok": False,
                        "error": "服务器处理失败，请查看 AstrBot 日志",
                    }
                )
            else:
                results.append({"group_openid": group_openid, "ok": True})
        return results

    async def web_delete_group(self, group_openid: str) -> dict[str, Any]:
        entry = self._group_config(group_openid, required=True)
        if (
            entry.get("enabled")
            or entry.get("uid_review_enabled")
            or entry.get("managed_strategy_id")
        ):
            raise RuntimeError("请先将审核方式改为关闭、应用成功后再移除")
        entries = self.config.get("auto_review_groups") or []
        self.config["auto_review_groups"] = [
            item for item in entries if item is not entry
        ]
        self.config.save_config()
        return {"group_openid": group_openid}

    @qq_admin_command("自动审核状态")
    async def auto_review_state(self, event: AstrMessageEvent):
        """查询当前群的两种自动审核状态。"""
        _, group_openid, _ = self._context(event)
        entry = self._group_config(group_openid)
        settings = self._condition_settings(entry)
        condition_enabled = settings["enabled"]
        condition_bound = bool(
            condition_enabled
            and entry
            and entry.get("platform_id")
            and not entry.get("managed_strategy_id")
            and not entry.get("enabled", False)
        )
        _, _, strategy = await self._auto_strategy(event, required=False)
        native_state = "未开启"
        if strategy is not None:
            native_state = "已开启" if strategy.get("is_enable") == "on" else "已停用"
        lines = [
            f"QQ 号码白名单：{native_state}",
            "条件审核："
            + (
                "已开启"
                if condition_bound
                else "待同步"
                if condition_enabled
                else "未开启"
            ),
            f"硬拒绝关键词：{len(settings.get('reject_keywords', []))} 个",
            "有效 UID 直接通过："
            + ("已开启" if settings.get("uid_exists_auto_approve") else "未开启"),
            f"通过关键词：{len(settings.get('approve_keywords', []))} 个",
            "条件组合："
            + ("全部满足" if settings.get("condition_logic") == "all" else "任一满足"),
            "条件审核平台绑定：" + ("已绑定" if condition_bound else "未绑定"),
        ]
        if strategy is not None:
            lines.extend(
                [
                    f"白名单人数：约 {self._value(strategy.get('whitelist_user_count'))}",
                    f"到期时间：{self._value(strategy.get('expire_at'))}",
                ]
            )
        yield event.plain_result("\n".join(lines))

    @qq_admin_command("自动审核开启")
    async def auto_review_enable(self, event: AstrMessageEvent, users: str):
        """为当前群开启 QQ 号码白名单自动审核。"""
        numbers = parse_qq_numbers(users)
        _, current_group, _ = self._context(event)
        self._ensure_native_mode(current_group)
        api, group_openid, strategy = await self._auto_strategy(
            event,
            required=False,
        )
        if strategy is None:
            strategy = await api.create_strategy(
                group_openids=[group_openid],
                is_enable="on",
                remark="AstrBot 自动审核",
            )
        else:
            await api.update_strategy(
                self._strategy_id(strategy),
                {"is_enable": "on"},
            )
        strategy_id = self._strategy_id(strategy)
        data = await api.update_whitelist(
            strategy_id,
            op="add",
            users=numbers,
        )
        scan_result = await self._scan_pending(api, strategy_id)
        self._record_whitelist_change(
            group_openid,
            strategy_id,
            add=numbers,
        )
        yield event.plain_result(
            "自动审核已开启，白名单人数约 "
            f"{self._value(data.get('whitelist_user_count'))}。"
            f"{scan_result}"
        )

    @qq_admin_command("自动审核添加")
    async def auto_review_add(self, event: AstrMessageEvent, users: str):
        """向当前群自动审核策略添加 QQ 号码。"""
        numbers = parse_qq_numbers(users)
        _, current_group, _ = self._context(event)
        self._ensure_native_mode(current_group)
        api, group_openid, strategy = await self._auto_strategy(
            event,
            required=True,
        )
        strategy_id = self._strategy_id(strategy)
        data = await api.update_whitelist(
            strategy_id,
            op="add",
            users=numbers,
        )
        scan_result = await self._scan_pending(api, strategy_id)
        self._record_whitelist_change(
            group_openid,
            strategy_id,
            add=numbers,
        )
        yield event.plain_result(
            "自动审核白名单已添加，当前约 "
            f"{self._value(data.get('whitelist_user_count'))} 人。"
            f"{scan_result}"
        )

    @qq_admin_command("自动审核移除")
    async def auto_review_remove(self, event: AstrMessageEvent, users: str):
        """从当前群自动审核策略移除 QQ 号码。"""
        numbers = parse_qq_numbers(users)
        _, current_group, _ = self._context(event)
        self._ensure_native_mode(current_group)
        api, group_openid, strategy = await self._auto_strategy(
            event,
            required=True,
        )
        strategy_id = self._strategy_id(strategy)
        data = await api.update_whitelist(
            strategy_id,
            op="del",
            users=numbers,
        )
        self._record_whitelist_change(
            group_openid,
            strategy_id,
            remove=numbers,
        )
        yield event.plain_result(
            "自动审核白名单已移除，当前约 "
            f"{self._value(data.get('whitelist_user_count'))} 人。"
        )

    @qq_admin_command("自动审核同步")
    async def auto_review_sync(
        self,
        event: AstrMessageEvent,
        confirmation: str,
    ):
        """将当前群 WebUI 配置同步到 QQ 官方策略。"""
        self._confirm(confirmation)
        _, group_openid, _ = self._context(event)
        entry = self._group_config(group_openid, required=True)
        result = await self._sync_group_config(
            self._client(event),
            group_openid,
            entry,
            str(event.get_platform_id()),
        )
        yield event.plain_result(f"配置已同步：{result}")

    @qq_admin_command("自动审核关闭")
    async def auto_review_close(
        self,
        event: AstrMessageEvent,
        confirmation: str,
    ):
        """删除当前群自动审核策略，需要确认。"""
        self._confirm(confirmation)
        api, group_openid, strategy = await self._auto_strategy(
            event,
            required=True,
        )
        await api.delete_strategy(self._strategy_id(strategy))
        self._clear_group_config(group_openid)
        entry = self._group_config(group_openid)
        if entry and entry.get("platform_id"):
            entry["platform_id"] = ""
            self.config.save_config()
        condition_enabled = self._condition_settings(entry)["enabled"]
        yield event.plain_result(
            "QQ 号码白名单策略及名单已删除。"
            + (
                "WebUI 中条件审核开关已保留，请同步后启用。"
                if condition_enabled
                else ""
            )
        )
