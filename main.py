from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import suppress
from functools import wraps
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

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
JOIN_LIST_LIMIT = 5


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
        return filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)(
            wrapped
        )

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
/禁言状态
/禁言 <成员OpenID|@成员> <30m|2h|7d>
/解禁 <成员OpenID|@成员>
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
        self._approval_lock = asyncio.Lock()
        self._last_approval_at = 0.0
        self._approval_tokens: dict[str, tuple[float, str, str, str]] = {}
        self._settings_tokens: dict[str, tuple[float, str, str, str]] = {}
        self._poll_cursors: dict[tuple[str, str], str] = {}
        self._patched_clients: dict[Any, Any] = {}
        self._bilibili_retry_at = 0.0
        self._web = GroupAdminWeb(self, context)

    async def initialize(self) -> None:
        self._web.register_routes()
        self._patch_qq_clients()
        self._review_task = asyncio.create_task(
            self._uid_review_loop(),
            name="qqgroup-admin-uid-review",
        )

    async def terminate(self) -> None:
        if self._review_task:
            self._review_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._review_task
            self._review_task = None
        for client, previous in self._patched_clients.items():
            handler = getattr(client, "on_interaction_create", None)
            if getattr(handler, "__qqgroup_admin_owner__", None) is self:
                if previous is None:
                    delattr(client, "on_interaction_create")
                else:
                    client.on_interaction_create = previous
        self._patched_clients.clear()

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

                setattr(interaction_handler, "__qqgroup_admin_owner__", self)
                setattr(
                    interaction_handler,
                    "__qqgroup_admin_previous__",
                    existing,
                )
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
        if (
            getattr(interaction, "type", None) != 11
            or getattr(interaction, "chat_type", None) != 1
            or not group_openid
            or len(parts) != 3
            or parts[0] not in {"qqga", "qqgs"}
            or parts[2]
            not in (
                {"approve", "decline"}
                if parts[0] == "qqga"
                else {"bind", "uid", "sync", "off"}
            )
        ):
            return False

        response_code = 1
        try:
            self._cleanup_tokens()
            if parts[0] == "qqga":
                token_data = self._approval_tokens.get(parts[1])
                if token_data is None:
                    response_code = 3
                elif token_data[1] != group_openid:
                    response_code = 4
                else:
                    _, _, member_openid, join_request_id = token_data
                    entry = self._group_config(group_openid)
                    reason = str(
                        (entry or {}).get("button_reject_reason") or "管理员拒绝"
                    )
                    await self._approve_request(
                        QQGroupAPI(client),
                        group_openid,
                        member_openid,
                        join_request_id,
                        op=parts[2],
                        reject_reason=reason if parts[2] == "decline" else "",
                    )
                    response_code = 0
            else:
                token_data = self._settings_tokens.get(parts[1])
                if token_data is None:
                    response_code = 3
                elif token_data[1] != group_openid:
                    response_code = 4
                else:
                    await self._apply_settings_button(
                        client,
                        group_openid,
                        token_data[2],
                        parts[2],
                        token_data[3],
                    )
                    response_code = 0
        except QQAPIError as exc:
            if exc.status == 429:
                response_code = 2
            self.logger.warning("处理 QQ 群管理按钮失败：%s", exc)
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            self.logger.warning("处理 QQ 群管理按钮失败：%s", exc)
        finally:
            if interaction_id:
                try:
                    await client.api.on_interaction_result(
                        interaction_id,
                        response_code,
                    )
                except Exception as exc:  # noqa: BLE001 - botpy raises transport errors
                    self.logger.warning("回应 QQ 按钮互动事件失败：%s", exc)
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
                "group_name": group_name,
                "group_openid": group_openid,
                "enabled": False,
                "whitelist_qq_numbers": "",
                "scan_pending": True,
                "uid_review_enabled": False,
                "uid_reject_keywords": "",
                "button_reject_reason": "管理员拒绝",
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

    def _uid_settings(self, entry: dict[str, Any] | None) -> tuple[bool, list[str]]:
        if entry is None:
            return False, []
        return bool(entry.get("uid_review_enabled", False)), parse_keywords(
            str(entry.get("uid_reject_keywords") or "")
        )

    def _ensure_native_mode(self, group_openid: str) -> None:
        entry = self._group_config(group_openid)
        uid_enabled, _ = self._uid_settings(entry)
        if uid_enabled:
            raise ValueError(
                "当前群已启用 B 站 UID 审核，不能同时使用 QQ 号码白名单策略"
            )

    def _platform_clients(self) -> dict[str, Any]:
        clients = {}
        for platform in self._qq_platforms():
            platform_id = str(getattr(platform.meta(), "id", "") or "")
            if platform_id:
                clients[platform_id] = platform.get_client()
        return clients

    def _uid_review_entries(self) -> list[tuple[str, str, list[str]]]:
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
                enabled, keywords = self._uid_settings(entry)
            except ValueError as exc:
                self.logger.warning("跳过无效的 UID 审核配置：%s", exc)
                continue
            group_openid = str(entry.get("group_openid") or "").strip()
            platform_id = str(entry.get("platform_id") or "").strip()
            key = (platform_id, group_openid)
            if not enabled or not all(key) or key in seen:
                continue
            seen.add(key)
            result.append((platform_id, group_openid, keywords))
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
        keywords: list[str],
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

            text = verification_text(request)
            keyword = matched_keyword(text, keywords)
            uid = None if keyword else parse_request_bilibili_uid(request)
            if keyword:
                op, reason = "decline", "验证消息包含拒绝关键词"
            elif uid is None:
                op, reason = "decline", "未提供有效的 B 站 UID"
            else:
                if time.monotonic() < self._bilibili_retry_at:
                    return
                try:
                    exists = await bilibili_uid_exists(uid)
                except BilibiliLookupError as exc:
                    self._bilibili_retry_at = time.monotonic() + max(
                        60,
                        self._review_interval(),
                    )
                    self.logger.warning(
                        "B 站 UID 查询暂不可用，本轮保留待审申请：%s", exc
                    )
                    return
                op = "approve" if exists else "decline"
                reason = "" if exists else "B 站 UID 不存在"

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
                self.logger.warning("自动处理 QQ 入群申请失败：%s", exc)
                if exc.status == 429:
                    return
                continue
            self.logger.info(
                "已自动%s QQ 入群申请：group=%s request=%s",
                "同意" if op == "approve" else "拒绝",
                group_openid,
                join_request_id,
            )

    async def _uid_review_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                clients = self._platform_clients()
                for platform_id, group_openid, keywords in self._uid_review_entries():
                    client = clients.get(platform_id)
                    if client is None:
                        continue
                    try:
                        await self._poll_uid_group(
                            client,
                            platform_id,
                            group_openid,
                            keywords,
                        )
                    except (
                        QQAPIError,
                        AttributeError,
                        TypeError,
                        ValueError,
                        RuntimeError,
                    ) as exc:
                        self.logger.warning("轮询 QQ 入群申请失败：%s", exc)
                    await asyncio.sleep(2.1)
            except Exception as exc:  # noqa: BLE001 - keep the background task alive
                self.logger.warning("QQ 入群审核后台任务本轮失败：%s", exc)
            await asyncio.sleep(self._review_interval())

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

    @qq_group_command("审核设置")
    async def review_settings(self, event: AstrMessageEvent):
        """发送仅 QQ 群主或管理员可操作的审核设置按钮。"""
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

        def button(button_id: str, label: str, action: str, style: int) -> dict:
            return {
                "id": button_id,
                "render_data": {
                    "label": label,
                    "visited_label": f"已{label}",
                    "style": style,
                },
                "action": {
                    "type": 1,
                    "permission": {"type": 1},
                    "data": f"qqgs:{token}:{action}",
                    "unsupport_tips": "当前 QQ 版本不支持设置按钮",
                },
            }

        rows = [
            {
                "buttons": [
                    button("bind", "绑定此群", "bind", 1),
                    button("uid", "启用 UID 审核", "uid", 1),
                ]
            },
            {
                "buttons": [
                    button("sync", "应用页面配置", "sync", 0),
                    button("off", "停用自动审核", "off", 0),
                ]
            },
        ]
        kwargs = {
            "group_openid": group_openid,
            "msg_type": 2,
            "markdown": {
                "content": (
                    f"# {self._markdown_text(group_name)}\n"
                    "选择当前群的自动审核操作。设置按钮仅群主或群管理员可用。"
                )
            },
            "keyboard": {"content": {"rows": rows}},
        }
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if message_id:
            kwargs["msg_id"] = message_id
        try:
            await client.api.post_group_message(**kwargs)
        except Exception as exc:
            raise RuntimeError(
                "发送审核设置按钮失败；请确认机器人已开通 Markdown 和自定义按钮权限"
            ) from exc

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
            lines.append(f"\n下一页：`/申请列表 {self._markdown_text(next_cursor)}`")
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
            raise RuntimeError(
                "发送审批按钮失败；请确认机器人已开通 Markdown 和自定义按钮权限"
            ) from exc

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
    ) -> str:
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
        return expire_at

    @qq_admin_command("禁言")
    async def mute_member(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
    ):
        """新增或更新成员禁言，最长 30 天。"""
        expire_at = await self._set_mute(event, member_openid, duration)
        yield event.plain_result(f"已设置禁言，至 {expire_at}。")

    @qq_admin_command("解禁")
    async def mute_remove(self, event: AstrMessageEvent, member_openid: str):
        """立即解除成员禁言。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        await self._api(event).set_member_mutes(
            group_openid,
            [{"op": "del", "member_openid": member, "mute_expire_at": ""}],
        )
        yield event.plain_result("已解除禁言。")

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
            self._uid_settings(entry)[0] if uid_enabled is None else uid_enabled
        )
        if uid_enabled and native_enabled:
            raise ValueError(
                "QQ 号码白名单会绕过 UID 和关键词检查，两种自动审核不能同时启用"
            )

        api = QQGroupAPI(client)
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
                "B 站 UID 与关键词自动审核已启用。"
                if uid_enabled
                else "两种自动审核均已关闭，群绑定已保留。"
            )

        desired = parse_qq_number_text(
            str(entry.get("whitelist_qq_numbers") or "")
        )
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
        if action == "uid":
            await self._sync_group_config(
                client,
                group_openid,
                entry,
                platform_id,
                native_enabled=False,
                uid_enabled=True,
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
        uid_enabled = bool(entry.get("uid_review_enabled", False))
        mode = "native" if native_enabled else "uid" if uid_enabled else "off"
        bound = bool(entry.get("platform_id"))
        managed = bool(entry.get("managed_strategy_id"))
        desired = parse_qq_number_text(
            str(entry.get("whitelist_qq_numbers") or "")
        )
        applied = parse_qq_number_text(str(entry.get("applied_whitelist") or ""))
        synchronized = (
            bound and managed and desired == applied
            if mode == "native"
            else bound and not managed
            if mode == "uid"
            else not managed
        )
        return {
            "group_name": group_name or f"未绑定群 {group_openid[:8]}",
            "group_openid": group_openid,
            "mode": mode,
            "bound": bound,
            "synchronized": synchronized,
            "whitelist_qq_numbers": "\n".join(desired),
            "uid_reject_keywords": str(entry.get("uid_reject_keywords") or ""),
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
            if isinstance(entry, dict)
            and str(entry.get("group_openid") or "").strip()
        ]

    async def web_save_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        group_openid = str(payload["group_openid"])
        entry = self._group_config(group_openid, required=True)
        mode = str(payload["mode"])
        entry.update(
            {
                "enabled": mode == "native",
                "uid_review_enabled": mode == "uid",
                "whitelist_qq_numbers": str(payload["whitelist_qq_numbers"]),
                "uid_reject_keywords": str(payload["uid_reject_keywords"]),
                "scan_pending": bool(payload["scan_pending"]),
                "button_reject_reason": str(payload["button_reject_reason"]),
            }
        )
        self.config.save_config()
        return self._web_group(entry)

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
        uid_enabled, keywords = self._uid_settings(entry)
        uid_bound = bool(
            uid_enabled
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
            "B 站 UID 审核："
            + ("已开启" if uid_bound else "待同步" if uid_enabled else "未开启"),
            f"UID 拒绝关键词：{len(keywords)} 个",
            "UID 平台绑定：" + ("已绑定" if uid_bound else "未绑定"),
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
        uid_enabled, _ = self._uid_settings(entry)
        yield event.plain_result(
            "QQ 号码白名单策略及名单已删除。"
            + ("WebUI 中 B 站 UID 开关已保留，请同步后启用。" if uid_enabled else "")
        )
