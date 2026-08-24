from __future__ import annotations

import csv
import re
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from io import StringIO
from typing import Any

from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .bilibili import parse_bilibili_uids
from .qq_api import parse_qq_number_text
from .review import parse_keywords

PLUGIN_NAME = "astrbot_plugin_qqgroup_admin"
BATCH_GROUP_LIMIT = 100
BATCH_TEXT_BUDGET = 4_000_000
KEYWORD_REPLY_LIMIT = 100
WELCOME_RULE_LIMIT = 100
WELCOME_MESSAGE_LIMIT = 4000
MAX_AI_FALLBACK_PROVIDERS = 3
MAX_MEMBER_LIST_ITEMS = 10_000
VIOLATION_REVIEW_LABELS = {
    "pending": "待复核",
    "confirmed": "确认违规",
    "false_positive": "误判",
}
BATCH_FIELDS = {
    "mode",
    "whitelist_qq_numbers",
    "uid_check_enabled",
    "uid_exists_auto_approve",
    "approve_keywords",
    "reject_keywords",
    "condition_logic",
    "fallback_action",
    "scan_pending",
    "button_reject_reason",
    "fallback_human_verify_enabled",
    "moderation_enabled",
    "moderation_exempt_admins",
    "member_blacklist",
    "member_whitelist",
    "blacklist_reply",
    "blacklist_at_member",
    "message_reject_keywords",
    "message_reject_reply",
    "message_reject_at_member",
    "ai_review_enabled",
    "ai_review_provider_id",
    "ai_review_fallback_provider_id",
    "image_keyword_review_enabled",
    "image_reject_keywords",
    "image_reject_reply",
    "image_reject_at_member",
    "image_spam_enabled",
    "image_spam_count",
    "image_spam_window_seconds",
    "image_spam_group_min_members",
    "image_spam_recall_count",
    "image_spam_reply",
    "image_spam_at_member",
    "repeat_review_enabled",
    "repeat_count",
    "repeat_window_seconds",
    "repeat_mute_min_seconds",
    "repeat_mute_max_seconds",
    "repeat_reply",
    "repeat_at_member",
    "bilibili_uids",
    "bilibili_dynamic_enabled",
    "bilibili_live_enabled",
}
BATCH_TEXT_FIELDS = {
    "whitelist_qq_numbers",
    "approve_keywords",
    "reject_keywords",
    "button_reject_reason",
    "message_reject_keywords",
    "message_reject_reply",
    "member_blacklist",
    "member_whitelist",
    "blacklist_reply",
    "ai_review_provider_id",
    "ai_review_fallback_provider_id",
    "image_reject_keywords",
    "image_reject_reply",
    "image_spam_reply",
    "repeat_reply",
    "bilibili_uids",
}


class GroupAdminWeb:
    """Small WebUI adapter; QQ strategy changes stay in the main plugin."""

    def __init__(self, plugin: Any, context: Context) -> None:
        self.plugin = plugin
        self.context = context

    @staticmethod
    def _provider_ids(value: Any) -> list[str]:
        values = value if isinstance(value, (list, tuple)) else re.split(
            r"[,，;；\r\n]+", str(value or "")
        )
        result: list[str] = []
        for item in values:
            provider_id = GroupAdminWeb._text(item, "AI 回退模型", 256)
            if provider_id and provider_id not in result:
                result.append(provider_id)
        if len(result) > MAX_AI_FALLBACK_PROVIDERS:
            raise ValueError(
                f"AI 回退模型最多选择 {MAX_AI_FALLBACK_PROVIDERS} 个"
            )
        return result

    @staticmethod
    def _response(data: Any = None, *, message: str = "") -> Any:
        payload: dict[str, Any] = {"ok": True}
        if data is not None:
            payload["data"] = data
        if message:
            payload["message"] = message
        return json_response(payload)

    @staticmethod
    def _error(message: str, status: int) -> Any:
        return error_response(message, status_code=status)

    @staticmethod
    def _csv_safe(value: Any) -> str:
        text = "" if value is None else str(value)
        if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return "'" + text
        return text

    @staticmethod
    def _text(
        value: Any,
        label: str,
        limit: int,
        *,
        required: bool = False,
        multiline: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label}格式错误")
        value = value.strip()
        if required and not value:
            raise ValueError(f"{label}不能为空")
        if len(value) > limit:
            raise ValueError(f"{label}最多 {limit} 个字符")
        allowed_controls = "\r\n\t" if multiline else ""
        if any(ord(char) < 32 and char not in allowed_controls for char in value):
            raise ValueError(f"{label}包含非法控制字符")
        return value

    @staticmethod
    def _bool(payload: dict[str, Any], key: str, default: bool, label: str) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            raise TypeError(f"{label}必须是布尔值")
        return value

    @classmethod
    def _member_list(cls, value: Any, label: str) -> str:
        text = cls._text(value, label, 1_400_000, multiline=True)
        items = [
            item.strip()
            for item in re.split(r"[\s,，;；]+", text)
            if item.strip()
        ]
        items = list(dict.fromkeys(items))
        if len(items) > MAX_MEMBER_LIST_ITEMS:
            raise ValueError(f"{label}最多 {MAX_MEMBER_LIST_ITEMS} 个")
        if any(len(item) > 128 for item in items):
            raise ValueError(f"{label}中的成员 OpenID 最多 128 个字符")
        return "\n".join(items)

    @staticmethod
    def _int(
        payload: dict[str, Any],
        key: str,
        default: int,
        minimum: int,
        maximum: int,
        label: str,
    ) -> int:
        value = payload.get(key, default)
        if isinstance(value, bool):
            raise TypeError(f"{label}必须是整数")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{label}必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}必须在 {minimum}-{maximum} 之间")
        return value

    @classmethod
    def _keyword_replies(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("关键词回复必须是列表")
        if len(value) > KEYWORD_REPLY_LIMIT:
            raise ValueError(f"每群最多配置 {KEYWORD_REPLY_LIMIT} 条关键词回复")

        replies = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise TypeError(f"第 {index} 条关键词回复格式错误")
            name = cls._text(
                item.get("name"), f"第 {index} 条规则名称", 80, required=True
            )
            keywords = parse_keywords(
                cls._text(
                    item.get("keywords", item.get("keyword", "")),
                    f"第 {index} 条关键词",
                    2_100,
                    required=True,
                    multiline=True,
                )
            )
            if not keywords:
                raise ValueError(f"第 {index} 条关键词不能为空")
            if len(keywords) > 20:
                raise ValueError(f"第 {index} 条关键词最多 20 个")
            reply = cls._text(
                item.get("reply"),
                f"第 {index} 条回复内容",
                1_000,
                required=True,
                multiline=True,
            )
            match_type = cls._text(
                item.get("match_type", "contains"), f"第 {index} 条匹配方式", 16
            )
            if match_type not in {"contains", "exact"}:
                raise ValueError(f"第 {index} 条匹配方式只能是 contains 或 exact")
            condition_logic = cls._text(
                item.get(
                    "condition_logic",
                    item.get("keyword_logic", item.get("logic", "any")),
                ),
                f"第 {index} 条关键词组合",
                8,
            )
            if condition_logic not in {"all", "any"}:
                raise ValueError(f"第 {index} 条关键词组合只能是 all 或 any")
            if match_type == "exact" and condition_logic == "all" and len(keywords) > 1:
                raise ValueError(
                    f"第 {index} 条完全匹配不能与多个关键词的全部满足组合使用"
                )
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise TypeError(f"第 {index} 条启用状态必须是布尔值")
            replies.append(
                {
                    "__template_key": "keyword_reply",
                    "name": name,
                    "keywords": "\n".join(keywords),
                    "condition_logic": condition_logic,
                    "reply": reply,
                    "match_type": match_type,
                    "enabled": enabled,
                }
            )
        return replies

    @classmethod
    def _welcome_rules(
        cls, value: Any, allowed_group_openids: set[str]
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("入群欢迎规则必须是列表")
        if len(value) > WELCOME_RULE_LIMIT:
            raise ValueError(f"最多配置 {WELCOME_RULE_LIMIT} 条入群欢迎规则")
        rules: list[dict[str, Any]] = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise TypeError(f"第 {index} 条入群欢迎规则格式错误")
            name = cls._text(
                item.get("name", ""), f"第 {index} 条规则名称", 80, required=True
            )
            message = cls._text(
                item.get("message", item.get("content", "")),
                f"第 {index} 条欢迎内容",
                WELCOME_MESSAGE_LIMIT,
                required=True,
                multiline=True,
            )
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise TypeError(f"第 {index} 条启用状态必须是布尔值")
            at_member = item.get("at_member", "{at_user}" in message)
            if not isinstance(at_member, bool):
                raise TypeError(f"第 {index} 条艾特开关必须是布尔值")
            group_values = item.get("group_openids", item.get("groups", []))
            if isinstance(group_values, str):
                group_values = re.split(r"[\s,，;；]+", group_values.strip())
            if not isinstance(group_values, list):
                raise TypeError(f"第 {index} 条覆盖群必须是列表")
            group_openids = [
                cls._validated_group({"group_openid": value})
                for value in group_values
                if str(value or "").strip()
            ]
            group_openids = list(dict.fromkeys(group_openids))
            if len(group_openids) > BATCH_GROUP_LIMIT:
                raise ValueError(f"第 {index} 条最多覆盖 {BATCH_GROUP_LIMIT} 个群")
            unknown = set(group_openids) - allowed_group_openids
            if unknown:
                raise ValueError(f"第 {index} 条包含未绑定群：{min(unknown)}")
            recall = cls._int(
                item,
                "auto_recall_seconds",
                0,
                0,
                120,
                f"第 {index} 条自动撤回秒数",
            )
            rules.append(
                {
                    "__template_key": "welcome_rule",
                    "name": name,
                    "message": message,
                    "group_openids": group_openids,
                    "enabled": enabled,
                    "at_member": at_member,
                    "auto_recall_seconds": recall,
                }
            )
        return rules

    @classmethod
    def _global_keyword_replies(
        cls,
        value: Any,
        allowed_group_openids: set[str],
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise TypeError("全局关键词回复必须是列表")
        if len(value) > KEYWORD_REPLY_LIMIT:
            raise ValueError(f"最多配置 {KEYWORD_REPLY_LIMIT} 条全局关键词回复")
        replies = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise TypeError(f"第 {index} 条全局关键词回复格式错误")
            name = cls._text(
                item.get("name"), f"第 {index} 条规则名称", 80, required=True
            )
            keywords = parse_keywords(
                cls._text(
                    item.get("keywords", ""),
                    f"第 {index} 条关键词",
                    2_100,
                    required=True,
                    multiline=True,
                )
            )
            if not keywords:
                raise ValueError(f"第 {index} 条关键词不能为空")
            if len(keywords) > 20:
                raise ValueError(f"第 {index} 条关键词最多 20 个")
            reply = cls._text(
                item.get("reply"),
                f"第 {index} 条回复内容",
                1_000,
                required=True,
                multiline=True,
            )
            condition_logic = cls._text(
                item.get(
                    "condition_logic",
                    item.get("keyword_logic", item.get("logic", "any")),
                ),
                f"第 {index} 条关键词组合",
                8,
            )
            if condition_logic not in {"all", "any"}:
                raise ValueError(f"第 {index} 条关键词组合只能是 all 或 any")
            match_type = cls._text(
                item.get("match_type", "contains"), f"第 {index} 条匹配方式", 16
            )
            if match_type not in {"contains", "exact"}:
                raise ValueError(f"第 {index} 条匹配方式只能是 contains 或 exact")
            if match_type == "exact" and condition_logic == "all" and len(keywords) > 1:
                raise ValueError(
                    f"第 {index} 条完全匹配不能与多个关键词的全部满足组合使用"
                )
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise TypeError(f"第 {index} 条启用状态必须是布尔值")
            raw_groups = item.get("group_openids", [])
            if not isinstance(raw_groups, list):
                raise TypeError(f"第 {index} 条覆盖群必须是列表")
            group_openids = [
                cls._validated_group({"group_openid": group_openid})
                for group_openid in raw_groups
            ]
            if len(group_openids) > BATCH_GROUP_LIMIT:
                raise ValueError(f"第 {index} 条最多覆盖 {BATCH_GROUP_LIMIT} 个群")
            if len(set(group_openids)) != len(group_openids):
                raise ValueError(f"第 {index} 条覆盖群不能重复")
            unknown = set(group_openids) - allowed_group_openids
            if unknown:
                raise ValueError(f"第 {index} 条包含未绑定群：{min(unknown)}")
            replies.append(
                {
                    "__template_key": "keyword_reply",
                    "name": name,
                    "keywords": "\n".join(keywords),
                    "condition_logic": condition_logic,
                    "match_type": match_type,
                    "reply": reply,
                    "group_openids": "\n".join(group_openids),
                    "enabled": enabled,
                }
            )
        return replies

    @classmethod
    def _runtime_settings(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        settings = {
            "uid_review_interval_seconds": cls._int(
                payload, "uid_review_interval_seconds", 60, 15, 600, "审核轮询间隔"
            ),
            "mute_success_message": cls._text(
                payload.get("mute_success_message", ""),
                "禁言成功回复",
                1_000,
                multiline=True,
            ),
            "settings_panel_auto_recall": cls._bool(
                payload, "settings_panel_auto_recall", True, "设置面板自动撤回"
            ),
            "settings_command_enabled": cls._bool(
                payload, "settings_command_enabled", True, "审核设置命令"
            ),
            "global_reject_keywords": "\n".join(
                parse_keywords(
                    cls._text(
                        payload.get("global_reject_keywords", ""),
                        "全局入群拒绝关键词",
                        7_000,
                        multiline=True,
                    )
                )
            ),
            "global_message_reject_keywords": "\n".join(
                parse_keywords(
                    cls._text(
                        payload.get("global_message_reject_keywords", ""),
                        "全局消息撤回关键词",
                        7_000,
                        multiline=True,
                    )
                )
            ),
            "global_message_reject_reply": cls._text(
                payload.get("global_message_reject_reply", ""),
                "全局文字关键词撤回回复",
                1_000,
                multiline=True,
            ),
            "global_message_reject_at_member": cls._bool(
                payload,
                "global_message_reject_at_member",
                True,
                "全局文字关键词撤回艾特",
            ),
            "global_member_blacklist": cls._member_list(
                payload.get("global_member_blacklist", ""), "全局成员黑名单"
            ),
            "global_member_whitelist": cls._member_list(
                payload.get("global_member_whitelist", ""), "全局成员白名单"
            ),
            "global_blacklist_reply": cls._text(
                payload.get("global_blacklist_reply", ""),
                "全局黑名单撤回回复",
                1_000,
                multiline=True,
            ),
            "global_blacklist_at_member": cls._bool(
                payload,
                "global_blacklist_at_member",
                True,
                "全局黑名单撤回艾特",
            ),
            "global_image_reject_keywords": "\n".join(
                parse_keywords(
                    cls._text(
                        payload.get("global_image_reject_keywords", ""),
                        "全局图片文字撤回关键词",
                        7_000,
                        multiline=True,
                    )
                )
            ),
            "global_image_reject_reply": cls._text(
                payload.get("global_image_reject_reply", ""),
                "全局图片关键词撤回回复",
                1_000,
                multiline=True,
            ),
            "global_image_reject_at_member": cls._bool(
                payload,
                "global_image_reject_at_member",
                True,
                "全局图片关键词撤回艾特",
            ),
            "global_ai_reject_reply": cls._text(
                payload.get("global_ai_reject_reply", ""),
                "AI 撤回回复",
                1_000,
                multiline=True,
            ),
            "global_ai_reject_at_member": cls._bool(
                payload,
                "global_ai_reject_at_member",
                True,
                "AI 撤回艾特",
            ),
            "bilibili_live_interval_seconds": cls._int(
                payload, "bilibili_live_interval_seconds", 60, 30, 600, "直播轮询间隔"
            ),
            "bilibili_dynamic_interval_seconds": cls._int(
                payload,
                "bilibili_dynamic_interval_seconds",
                180,
                60,
                3_600,
                "动态轮询间隔",
            ),
        }
        ai_enabled_key = (
            "global_ai_review_enabled"
            if "global_ai_review_enabled" in payload
            else "ai_review_enabled"
            if "ai_review_enabled" in payload
            else None
        )
        primary_key = (
            "global_ai_review_provider_id"
            if "global_ai_review_provider_id" in payload
            else "ai_review_provider_id"
            if "ai_review_provider_id" in payload
            else None
        )
        fallback_key = next(
            (
                key
                for key in (
                    "global_ai_review_fallback_provider_ids",
                    "ai_review_fallback_provider_ids",
                    "ai_review_fallback_provider_id",
                )
                if key in payload
            ),
            None,
        )
        if ai_enabled_key is not None:
            settings["global_ai_review_enabled"] = cls._bool(
                payload, ai_enabled_key, False, "全局 AI 审核开关"
            )
        if "global_ai_review_action" in payload:
            action = cls._text(
                payload["global_ai_review_action"], "AI 命中动作", 16
            )
            if action not in {"record_only", "recall"}:
                raise ValueError("AI 命中动作只能是 record_only 或 recall")
            settings["global_ai_review_action"] = action
        if primary_key is not None:
            settings["global_ai_review_provider_id"] = cls._text(
                payload[primary_key], "全局 AI 审核主模型", 256
            )
        if fallback_key is not None:
            settings["global_ai_review_fallback_provider_ids"] = cls._provider_ids(
                payload[fallback_key]
            )
        if "global_ai_review_confirm_provider_id" in payload:
            settings["global_ai_review_confirm_provider_id"] = cls._text(
                payload["global_ai_review_confirm_provider_id"],
                "AI 二次确认模型",
                256,
            )
        for key, default, minimum, maximum, label in (
            (
                "global_ai_review_timeout_seconds",
                20,
                5,
                120,
                "AI 审核总超时",
            ),
            (
                "global_ai_review_block_threshold",
                95,
                50,
                100,
                "AI 审核拦截置信度",
            ),
            (
                "global_image_ocr_timeout_seconds",
                4,
                2,
                30,
                "图片 OCR 超时",
            ),
            (
                "global_image_ocr_max_images",
                1,
                1,
                3,
                "单条 OCR 图片数",
            ),
        ):
            if key in payload:
                settings[key] = cls._int(payload, key, default, minimum, maximum, label)
        for key, label in (
            ("global_ai_review_images_enabled", "AI 图片审核开关"),
            ("global_image_ocr_enabled", "图片 OCR 开关"),
        ):
            if key in payload:
                settings[key] = cls._bool(payload, key, False, label)
        if "global_image_ocr_provider_id" in payload:
            settings["global_image_ocr_provider_id"] = cls._text(
                payload["global_image_ocr_provider_id"], "图片 OCR 模型", 256
            )
        if (
            "global_ai_review_provider_id" in settings
            and "global_ai_review_fallback_provider_ids" in settings
            and settings["global_ai_review_provider_id"]
            in settings["global_ai_review_fallback_provider_ids"]
        ):
            raise ValueError("AI 审核主模型不能出现在回退模型列表")
        confirm_provider = settings.get("global_ai_review_confirm_provider_id", "")
        if confirm_provider and (
            confirm_provider == settings.get("global_ai_review_provider_id")
            or confirm_provider
            in settings.get("global_ai_review_fallback_provider_ids", [])
        ):
            raise ValueError("AI 二次确认模型不能与主模型或回退模型重复")
        # Older cached WebUI bundles do not send the newer per-reason reply
        # fields.  Treat those keys as a partial update so a stale page cannot
        # erase values already configured in the current runtime settings.
        for key in (
            "global_message_reject_reply",
            "global_message_reject_at_member",
            "global_member_blacklist",
            "global_member_whitelist",
            "global_blacklist_reply",
            "global_blacklist_at_member",
            "global_image_reject_reply",
            "global_image_reject_at_member",
            "global_ai_reject_reply",
            "global_ai_reject_at_member",
        ):
            if key not in payload:
                settings.pop(key, None)
        return settings

    @classmethod
    def _validated_save(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")

        group_openid = cls._text(
            payload.get("group_openid"), "群 OpenID", 128, required=True
        )
        if any(char.isspace() for char in group_openid):
            raise ValueError("群 OpenID 不能包含空白字符")
        raw_mode = cls._text(payload.get("mode"), "审核方式", 16, required=True)
        mode = "conditional" if raw_mode == "uid" else raw_mode
        if mode not in {"off", "conditional", "native"}:
            raise ValueError("审核方式只能是 off、conditional 或 native")

        whitelist_text = cls._text(
            payload.get("whitelist_qq_numbers", ""),
            "白名单",
            2_000_000,
            multiline=True,
        )
        approve_text = cls._text(
            payload.get("approve_keywords", ""),
            "通过关键词",
            7_000,
            multiline=True,
        )
        reject_text = cls._text(
            payload.get("reject_keywords", payload.get("uid_reject_keywords", "")),
            "拒绝关键词",
            7_000,
            multiline=True,
        )
        uid_check_enabled = payload.get("uid_check_enabled", raw_mode == "uid")
        if not isinstance(uid_check_enabled, bool):
            raise TypeError("UID 检查开关必须是布尔值")
        uid_exists_auto_approve = payload.get("uid_exists_auto_approve", False)
        if not isinstance(uid_exists_auto_approve, bool):
            raise TypeError("有效 UID 直接通过开关必须是布尔值")
        uid_exists_auto_approve = uid_check_enabled and uid_exists_auto_approve
        condition_logic = cls._text(
            payload.get("condition_logic", "all"), "条件组合", 8
        )
        if condition_logic not in {"all", "any"}:
            raise ValueError("条件组合只能是 all 或 any")
        fallback_action = cls._text(
            payload.get("fallback_action", "pending"), "兜底动作", 8
        )
        if fallback_action not in {"pending", "decline", "approve"}:
            raise ValueError("兜底动作只能是 pending、decline 或 approve")
        reject_reason = cls._text(
            payload.get("button_reject_reason", "管理员拒绝"),
            "拒绝理由",
            128,
        )
        scan_pending = payload.get("scan_pending", True)
        if not isinstance(scan_pending, bool):
            raise TypeError("扫描待审申请必须是布尔值")

        reject_keywords = "\n".join(parse_keywords(reject_text))
        message_keywords = "\n".join(
            parse_keywords(
                cls._text(
                    payload.get("message_reject_keywords", ""),
                    "消息撤回关键词",
                    7_000,
                    multiline=True,
                )
            )
        )
        image_keywords = "\n".join(
            parse_keywords(
                cls._text(
                    payload.get("image_reject_keywords", ""),
                    "图片文字撤回关键词",
                    7_000,
                    multiline=True,
                )
            )
        )
        message_reply = cls._text(
            payload.get("message_reject_reply", ""),
            "本群文字关键词撤回回复",
            1_000,
            multiline=True,
        )
        member_blacklist = cls._member_list(
            payload.get("member_blacklist", ""), "本群成员黑名单"
        )
        member_whitelist = cls._member_list(
            payload.get("member_whitelist", ""), "本群成员白名单"
        )
        blacklist_reply = cls._text(
            payload.get("blacklist_reply", ""),
            "本群黑名单撤回回复",
            1_000,
            multiline=True,
        )
        image_reply = cls._text(
            payload.get("image_reject_reply", ""),
            "本群图片关键词撤回回复",
            1_000,
            multiline=True,
        )
        image_spam_reply = cls._text(
            payload.get("image_spam_reply", ""),
            "连续发图撤回回复",
            1_000,
            multiline=True,
        )
        repeat_reply = cls._text(
            payload.get("repeat_reply", ""),
            "复读处置回复",
            1_000,
            multiline=True,
        )
        bili_uids = "\n".join(
            parse_bilibili_uids(
                cls._text(
                    payload.get("bilibili_uids", ""),
                    "B站 UID",
                    2_100,
                    multiline=True,
                )
            )
        )
        bili_dynamic_enabled = cls._bool(
            payload, "bilibili_dynamic_enabled", False, "动态推送开关"
        )
        bili_live_enabled = cls._bool(
            payload, "bilibili_live_enabled", False, "直播推送开关"
        )
        if (bili_dynamic_enabled or bili_live_enabled) and not bili_uids:
            raise ValueError("启用 B站推送时至少填写一个 UP 主 UID")
        mute_min = cls._int(
            payload, "repeat_mute_min_seconds", 60, 1, 2_592_000, "最短禁言秒数"
        )
        mute_max = cls._int(
            payload,
            "repeat_mute_max_seconds",
            600,
            mute_min,
            2_592_000,
            "最长禁言秒数",
        )
        ai_provider_id = cls._text(
            payload.get("ai_review_provider_id", ""),
            "AI 审核主模型",
            256,
        )
        ai_fallback_provider_id = cls._text(
            payload.get("ai_review_fallback_provider_id", ""),
            "AI 审核回退模型",
            256,
        )
        if ai_provider_id and ai_provider_id == ai_fallback_provider_id:
            raise ValueError("AI 审核主模型和回退模型不能相同")

        return {
            "group_openid": group_openid,
            "mode": mode,
            "whitelist_qq_numbers": "\n".join(parse_qq_number_text(whitelist_text)),
            "uid_check_enabled": uid_check_enabled,
            "uid_exists_auto_approve": uid_exists_auto_approve,
            "approve_keywords": "\n".join(parse_keywords(approve_text)),
            "reject_keywords": reject_keywords,
            "uid_reject_keywords": reject_keywords,
            "condition_logic": condition_logic,
            "fallback_action": fallback_action,
            "scan_pending": scan_pending,
            "button_reject_reason": reject_reason or "管理员拒绝",
            "fallback_human_verify_enabled": cls._bool(
                payload, "fallback_human_verify_enabled", False, "兜底真人验证开关"
            ),
            "moderation_enabled": cls._bool(
                payload, "moderation_enabled", False, "消息审查开关"
            ),
            "moderation_exempt_admins": cls._bool(
                payload, "moderation_exempt_admins", True, "管理员免审开关"
            ),
            "member_blacklist": member_blacklist,
            "member_whitelist": member_whitelist,
            "blacklist_reply": blacklist_reply,
            "blacklist_at_member": cls._bool(
                payload, "blacklist_at_member", True, "本群黑名单撤回艾特"
            ),
            "message_reject_keywords": message_keywords,
            "message_reject_reply": message_reply,
            "message_reject_at_member": cls._bool(
                payload, "message_reject_at_member", True, "本群文字关键词撤回艾特"
            ),
            "image_keyword_review_enabled": cls._bool(
                payload, "image_keyword_review_enabled", False, "图片文字审核开关"
            ),
            "image_reject_keywords": image_keywords,
            "image_reject_reply": image_reply,
            "image_reject_at_member": cls._bool(
                payload, "image_reject_at_member", True, "本群图片关键词撤回艾特"
            ),
            "keyword_replies": cls._keyword_replies(payload.get("keyword_replies", [])),
            "ai_review_enabled": cls._bool(
                payload, "ai_review_enabled", False, "AI 审核开关"
            ),
            "ai_review_provider_id": ai_provider_id,
            "ai_review_fallback_provider_id": ai_fallback_provider_id,
            "image_spam_enabled": cls._bool(
                payload, "image_spam_enabled", False, "连续发图检测开关"
            ),
            "image_spam_count": cls._int(
                payload, "image_spam_count", 5, 2, 20, "连续图片数量"
            ),
            "image_spam_window_seconds": cls._int(
                payload, "image_spam_window_seconds", 15, 3, 120, "连续发图时间窗"
            ),
            "image_spam_group_min_members": cls._int(
                payload,
                "image_spam_group_min_members",
                2,
                2,
                10,
                "跨成员刷图最少人数",
            ),
            "image_spam_recall_count": cls._int(
                payload,
                "image_spam_recall_count",
                5,
                1,
                50,
                "图片触发时撤回数量",
            ),
            "image_spam_reply": image_spam_reply,
            "image_spam_at_member": cls._bool(
                payload, "image_spam_at_member", True, "连续发图撤回艾特"
            ),
            "repeat_review_enabled": cls._bool(
                payload, "repeat_review_enabled", False, "复读检测开关"
            ),
            "repeat_count": cls._int(payload, "repeat_count", 4, 3, 20, "复读触发次数"),
            "repeat_window_seconds": cls._int(
                payload, "repeat_window_seconds", 30, 5, 120, "复读时间窗"
            ),
            "repeat_mute_min_seconds": mute_min,
            "repeat_mute_max_seconds": mute_max,
            "repeat_reply": repeat_reply,
            "repeat_at_member": cls._bool(
                payload, "repeat_at_member", True, "复读处置艾特"
            ),
            "bilibili_uids": bili_uids,
            "bilibili_dynamic_enabled": bili_dynamic_enabled,
            "bilibili_live_enabled": bili_live_enabled,
        }

    @classmethod
    def _validated_group(cls, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        group_openid = cls._text(
            payload.get("group_openid"), "群 OpenID", 128, required=True
        )
        if any(char.isspace() for char in group_openid):
            raise ValueError("群 OpenID 不能包含空白字符")
        return group_openid

    @classmethod
    def _validated_groups(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("请至少选择一个群")
        if len(value) > BATCH_GROUP_LIMIT:
            raise ValueError(f"单次最多处理 {BATCH_GROUP_LIMIT} 个群")
        group_openids = [cls._validated_group({"group_openid": item}) for item in value]
        if len(set(group_openids)) != len(group_openids):
            raise ValueError("群列表不能包含重复项")
        return group_openids

    async def _validated_batch_save(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        group_openids = self._validated_groups(payload.get("group_openids"))
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("请至少选择一项批量修改内容")
        unknown = set(changes) - BATCH_FIELDS
        if unknown:
            raise ValueError(f"不支持批量修改字段：{', '.join(sorted(unknown))}")

        current = {
            str(group.get("group_openid") or ""): group
            for group in await self._groups()
        }
        missing = [group_id for group_id in group_openids if group_id not in current]
        if missing:
            raise LookupError(f"找不到群配置：{missing[0]}")
        validated = []
        text_size = 0
        for group_id in group_openids:
            merged = {
                **current[group_id],
                **changes,
                **(
                    {"uid_check_enabled": True}
                    if changes.get("uid_exists_auto_approve") is True
                    and "uid_check_enabled" not in changes
                    else {}
                ),
                "group_openid": group_id,
            }
            text_size += sum(
                len(str(merged.get(key) or "")) for key in BATCH_TEXT_FIELDS
            )
            if text_size > BATCH_TEXT_BUDGET:
                raise ValueError("批量配置内容过大，请减少群数量后分批处理")
            validated.append(self._validated_save(merged))
        return validated

    async def _payload(self) -> Any:
        return await request.json(default=None)

    async def _groups(self) -> list[dict[str, Any]]:
        groups = await self.plugin.web_groups()
        if not isinstance(groups, list):
            raise TypeError("群配置格式错误")
        return groups

    async def page_overview(self) -> Any:
        groups = await self._groups()
        return self._response(
            {
                "total": len(groups),
                "bound": sum(bool(item.get("bound")) for item in groups),
                "active": sum(
                    item.get("mode") in {"uid", "conditional", "native"}
                    and bool(item.get("synchronized"))
                    for item in groups
                ),
                "pending": sum(not bool(item.get("synchronized")) for item in groups),
            }
        )

    async def page_list(self) -> Any:
        return self._response(await self._groups())

    async def page_global_keyword_replies(self) -> Any:
        groups = await self._groups()
        settings = await self.plugin.web_global_keyword_replies()
        if isinstance(settings, list):
            settings = {"rules": settings}
        if not isinstance(settings, dict):
            raise TypeError("全局关键词回复配置格式错误")
        return self._response(
            {
                "groups": [
                    {
                        "group_name": group["group_name"],
                        "group_openid": group["group_openid"],
                    }
                    for group in groups
                    if group.get("bound")
                ],
                "rules": settings.get("rules", []),
                "keyword_reply_cooldown_seconds": settings.get(
                    "keyword_reply_cooldown_seconds", 0
                ),
                "keyword_reply_recall_seconds": settings.get(
                    "keyword_reply_recall_seconds", 0
                ),
            }
        )

    async def page_global_keyword_replies_save(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        bound_group_openids = {
            str(group["group_openid"])
            for group in await self._groups()
            if group.get("bound")
        }
        settings = {
            "rules": self._global_keyword_replies(
                payload.get("rules"), bound_group_openids
            ),
            "keyword_reply_cooldown_seconds": self._int(
                payload,
                "keyword_reply_cooldown_seconds",
                0,
                0,
                3_600,
                "关键词回复单群冷却",
            ),
            "keyword_reply_recall_seconds": self._int(
                payload,
                "keyword_reply_recall_seconds",
                0,
                0,
                120,
                "关键词回复自动撤回",
            ),
        }
        return self._response(
            await self.plugin.web_save_global_keyword_replies(settings),
            message="全局关键词回复已保存",
        )

    async def page_welcome_rules(self) -> Any:
        groups = await self._groups()
        settings = await self.plugin.web_welcome_rules()
        if not isinstance(settings, dict):
            raise TypeError("入群欢迎配置格式错误")
        return self._response(
            {
                "groups": [
                    {
                        "group_name": group["group_name"],
                        "group_openid": group["group_openid"],
                    }
                    for group in groups
                    if group.get("bound")
                ],
                "rules": settings.get("rules", []),
            }
        )

    async def page_welcome_rules_save(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        bound_group_openids = {
            str(group["group_openid"])
            for group in await self._groups()
            if group.get("bound")
        }
        settings = {
            "rules": self._welcome_rules(
                payload.get("rules"), bound_group_openids
            )
        }
        return self._response(
            await self.plugin.web_save_welcome_rules(settings),
            message="入群欢迎规则已保存",
        )

    async def page_runtime(self) -> Any:
        return self._response(await self.plugin.web_runtime_settings())

    async def page_runtime_save(self) -> Any:
        return self._response(
            await self.plugin.web_save_runtime_settings(
                self._runtime_settings(await self._payload())
            ),
            message="全局运行配置已保存",
        )

    async def page_bilibili_login_start(self) -> Any:
        return self._response(await self.plugin.web_bilibili_login_start())

    async def page_bilibili_login_poll(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        qrcode_key = self._text(
            payload.get("qrcode_key"), "二维码登录标识", 256, required=True
        )
        return self._response(await self.plugin.web_bilibili_login_poll(qrcode_key))

    async def page_save(self) -> Any:
        result = await self.plugin.web_save_group(
            self._validated_save(await self._payload())
        )
        return self._response(result, message="配置已保存")

    async def page_delete(self) -> Any:
        result = await self.plugin.web_delete_group(
            self._validated_group(await self._payload())
        )
        return self._response(result, message="群配置已移除")

    async def page_sync(self) -> Any:
        result = await self.plugin.web_sync_group(
            self._validated_group(await self._payload())
        )
        return self._response(result, message="配置已应用")

    async def page_batch_save(self) -> Any:
        result = await self.plugin.web_batch_save(
            await self._validated_batch_save(await self._payload())
        )
        return self._response(result, message=f"已更新 {len(result)} 个群")

    async def page_batch_sync(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        result = await self.plugin.web_batch_sync(
            self._validated_groups(payload.get("group_openids"))
        )
        succeeded = sum(bool(item.get("ok")) for item in result)
        return self._response(
            result,
            message=f"应用完成：成功 {succeeded} 个，失败 {len(result) - succeeded} 个",
        )

    async def page_identities(self) -> Any:
        kind = self._text(
            request.query.get("kind", ""),
            "身份记录类型",
            20,
            required=True,
        )
        if kind not in {"bindings", "suspicious", "violations"}:
            raise ValueError("身份记录类型无效")
        query = self._text(
            request.query.get("query", ""),
            "身份记录搜索词",
            256,
        )
        review_status = self._text(
            request.query.get("review_status", ""),
            "违规复核状态",
            32,
        )
        try:
            page = int(request.query.get("page", "1"))
            page_size = int(request.query.get("page_size", "10"))
        except (TypeError, ValueError) as exc:
            raise ValueError("页码和每页条数必须是整数") from exc
        if page < 1:
            raise ValueError("页码必须是正整数")
        if page_size not in {10, 20, 50}:
            raise ValueError("每页条数只能是 10、20 或 50")
        return self._response(
            await self.plugin.web_identity_page(
                kind, query, page, page_size, review_status
            )
        )

    async def page_violation_export(self) -> Any:
        query = self._text(
            request.query.get("query", ""),
            "身份记录搜索词",
            256,
        )
        review_status = self._text(
            request.query.get("review_status", ""),
            "违规复核状态",
            32,
        )
        records = await self.plugin.web_violation_export(query, review_status)
        output = StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            (
                "时间",
                "群名称",
                "群 OpenID",
                "成员名称",
                "成员 OpenID",
                "联合 OpenID",
                "B站 UID",
                "命中规则",
                "处理动作",
                "消息内容",
                "审核模型",
                "AI 判定",
                "AI 置信度",
                "AI 理由",
                "确认模型",
                "确认判定",
                "确认置信度",
                "确认理由",
                "复核状态",
                "复核时间",
                "消息 ID",
            )
        )
        for record in records:
            action = str(record.get("action") or "")
            writer.writerow(
                self._csv_safe(value)
                for value in (
                    record.get("created_at"),
                    record.get("group_name"),
                    record.get("group_openid"),
                    record.get("username") or record.get("member_name"),
                    record.get("member_openid")
                    or record.get("qq_openid")
                    or record.get("openid"),
                    record.get("union_openid"),
                    record.get("uid") or record.get("bilibili_uid"),
                    record.get("reason")
                    or record.get("rule")
                    or record.get("category"),
                    "仅记录"
                    if action == "record_only"
                    else ("已撤回" if action == "recall" else action),
                    record.get("content")
                    or record.get("message")
                    or record.get("message_content")
                    or record.get("message_summary"),
                    record.get("ai_provider"),
                    record.get("ai_decision"),
                    record.get("ai_confidence"),
                    record.get("ai_reason"),
                    record.get("ai_confirm_provider"),
                    record.get("ai_confirm_decision"),
                    record.get("ai_confirm_confidence"),
                    record.get("ai_confirm_reason"),
                    VIOLATION_REVIEW_LABELS.get(
                        str(record.get("review_status") or ""),
                        "待复核",
                    ),
                    record.get("reviewed_at"),
                    record.get("message_id"),
                )
            )
        return self._response(
            {
                "filename": time.strftime(
                    "qqgroup-admin-violations-%Y%m%d-%H%M%S.csv"
                ),
                "content": "\ufeff" + output.getvalue(),
                "count": len(records),
            }
        )

    async def page_violation_review(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        record_id = self._text(
            payload.get("record_id"), "违规记录 ID", 64, required=True
        )
        review_status = self._text(
            payload.get("review_status"), "违规复核状态", 32, required=True
        )
        return self._response(
            await self.plugin.web_review_violation(record_id, review_status),
            message="违规记录复核状态已更新",
        )

    async def page_binding_delete(self) -> Any:
        payload = await self._payload()
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        uid = self._text(payload.get("uid"), "B站 UID", 20, required=True)
        if not uid.isdigit() or uid == "0":
            raise ValueError("B站 UID 必须是正整数")
        return self._response(
            await self.plugin.web_delete_binding(uid),
            message="UID 绑定已解除",
        )

    async def page_suspicious_clear(self) -> Any:
        payload = await self._payload()
        group_openid = self._validated_group(payload)
        if not isinstance(payload, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        member_openid = self._text(
            payload.get("member_openid"), "成员 OpenID", 128, required=True
        )
        return self._response(
            await self.plugin.web_clear_suspicious(group_openid, member_openid),
            message="可疑标记已解除",
        )

    def _wrap(
        self, handler: Callable[[], Awaitable[Any]]
    ) -> Callable[[], Awaitable[Any]]:
        @wraps(handler)
        async def wrapped() -> Any:
            try:
                return await handler()
            except (TypeError, ValueError) as exc:
                return self._error(str(exc), 400)
            except LookupError as exc:
                return self._error(str(exc), 404)
            except RuntimeError as exc:
                return self._error(str(exc), 409)
            except Exception:
                self.plugin.logger.exception("QQ群管理页面请求失败")
                return self._error("服务器处理请求失败", 500)

        return wrapped

    def register_routes(self) -> None:
        routes = (
            ("/overview", self.page_overview, ["GET"], "QQ群审核概览"),
            ("/list", self.page_list, ["GET"], "QQ群审核配置列表"),
            (
                "/global-keyword-replies",
                self.page_global_keyword_replies,
                ["GET"],
                "查询全局关键词回复",
            ),
            (
                "/global-keyword-replies/save",
                self.page_global_keyword_replies_save,
                ["POST"],
                "保存全局关键词回复",
            ),
            (
                "/welcome-rules",
                self.page_welcome_rules,
                ["GET"],
                "查询入群欢迎规则",
            ),
            (
                "/welcome-rules/save",
                self.page_welcome_rules_save,
                ["POST"],
                "保存入群欢迎规则",
            ),
            ("/runtime", self.page_runtime, ["GET"], "查询全局运行配置"),
            ("/runtime/save", self.page_runtime_save, ["POST"], "保存全局运行配置"),
            (
                "/bilibili-login/start",
                self.page_bilibili_login_start,
                ["POST"],
                "开始 B站二维码登录",
            ),
            (
                "/bilibili-login/poll",
                self.page_bilibili_login_poll,
                ["POST"],
                "查询 B站二维码登录状态",
            ),
            ("/save", self.page_save, ["POST"], "保存QQ群审核配置"),
            ("/delete", self.page_delete, ["POST"], "移除QQ群审核配置"),
            ("/sync", self.page_sync, ["POST"], "应用QQ群审核配置"),
            ("/batch-save", self.page_batch_save, ["POST"], "批量保存QQ群审核配置"),
            ("/batch-sync", self.page_batch_sync, ["POST"], "批量应用QQ群审核配置"),
            ("/identities", self.page_identities, ["GET"], "查询 UID 绑定和待验证成员"),
            (
                "/violations/export",
                self.page_violation_export,
                ["GET"],
                "导出违规记录",
            ),
            (
                "/violation-review",
                self.page_violation_review,
                ["POST"],
                "更新违规记录复核状态",
            ),
            ("/binding-delete", self.page_binding_delete, ["POST"], "解除 UID 绑定"),
            (
                "/suspicious-clear",
                self.page_suspicious_clear,
                ["POST"],
                "解除待验证标记",
            ),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                self._wrap(handler),
                methods,
                description,
            )
