from __future__ import annotations

from functools import wraps
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .qq_api import (
    QQAPIError,
    QQGroupAPI,
    future_rfc3339,
    parse_duration,
    parse_group_ids,
    parse_openids,
    parse_qq_numbers,
    validate_choice,
    validate_rfc3339,
)

QQ_PLATFORM_TYPES = (
    filter.PlatformAdapterType.QQOFFICIAL
    | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
)


def guarded(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        event = args[1]
        try:
            async for result in handler(*args, **kwargs):
                yield result
        except (QQAPIError, ValueError, RuntimeError) as exc:
            yield event.plain_result(f"操作失败：{exc}")

    return wrapper


def qq_admin_command(commandable: Any, name: str, *, alias: set[str] | None = None):
    def decorator(handler):
        wrapped = guarded(handler)
        wrapped = commandable.command(name, alias=alias)(wrapped)
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
/群管 标识
/群管 信息
/群管 机器人
/群管 申请 列表 [limit] [cursor]
/群管 申请 同意 <成员OpenID|@成员> <申请ID>
/群管 申请 拒绝 <成员OpenID|@成员> <申请ID> <理由|->
/群管 申请 拒绝拉黑 <成员OpenID|@成员> <申请ID> 确认 <理由|->
/群管 禁言 状态
/群管 禁言 添加 <成员OpenID|@成员> <30m|2h|7d>
/群管 禁言 更新 <成员OpenID|@成员> <30m|2h|7d>
/群管 禁言 解除 <成员OpenID|@成员>
/群管 策略 列表 [limit] [cursor]
/群管 策略 创建当前 <on|off> <RFC3339|-> <备注|->
/群管 策略 创建OpenID <OpenID,...> <on|off> <RFC3339|-> <备注|->
/群管 策略 创建群号 <群号,...> <on|off> <RFC3339|-> <备注|->
/群管 策略 开关 <策略ID> <on|off>
/群管 策略 到期 <策略ID> <RFC3339>
/群管 策略 备注 <策略ID> <备注>
/群管 策略 关联OpenID <策略ID> <add|del> <OpenID,...>
/群管 策略 关联群号 <策略ID> <add|del> <群号,...>
/群管 策略 白名单 <策略ID> <add|del> <QQ号,...>
/群管 策略 删除 <策略ID> 确认
/群管 策略 执行 <策略ID> 确认"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command_group("群管", alias={"qqgroup", "groupadmin"})
    @filter.platform_adapter_type(QQ_PLATFORM_TYPES)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    def group_admin():
        """QQ 官方机器人群聊管理。"""

    @group_admin.group("申请", alias={"join"})
    def join_admin():
        """入群申请审批。"""

    @group_admin.group("禁言", alias={"mute"})
    def mute_admin():
        """成员禁言管理。"""

    @group_admin.group("策略", alias={"strategy"})
    def strategy_admin():
        """入群自动审批策略。"""

    def _context(self, event: AstrMessageEvent) -> tuple[Any, str, str]:
        raw = event.message_obj.raw_message
        group_openid = str(getattr(raw, "group_openid", "") or "")
        author = getattr(raw, "author", None)
        member_openid = str(getattr(author, "member_openid", "") or "")
        if not group_openid or not member_openid:
            raise ValueError("当前会话是 QQ 频道而不是 QQ 群聊")
        return raw, group_openid, member_openid

    def _api(self, event: AstrMessageEvent) -> QQGroupAPI:
        platform = self.context.get_platform_inst(event.get_platform_id())
        client = (
            platform.get_client()
            if platform and hasattr(platform, "get_client")
            else None
        )
        client = client or getattr(event, "bot", None)
        if client is None:
            raise RuntimeError("无法取得 AstrBot QQ 官方客户端")
        return QQGroupAPI(client)

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
    def _expiry(value: str) -> str:
        return "" if value == "-" else validate_rfc3339(value, label="策略到期时间")

    @staticmethod
    def _value(value: Any) -> str:
        return str(value) if value not in {None, ""} else "-"

    @staticmethod
    def _list(value: Any) -> str:
        return ", ".join(str(item) for item in (value or [])) or "-"

    def _results(self, event: AstrMessageEvent, text: str):
        for chunk in split_message(text):
            yield event.plain_result(chunk)

    @qq_admin_command(group_admin, "帮助", alias={"help"})
    async def help_command(self, event: AstrMessageEvent):
        """显示完整命令帮助。"""
        self._context(event)
        yield event.plain_result(self.HELP)

    @qq_admin_command(group_admin, "标识", alias={"id"})
    async def identifiers(self, event: AstrMessageEvent):
        """显示当前群和自己的 OpenID。"""
        _, group_openid, member_openid = self._context(event)
        yield event.plain_result(
            f"群 OpenID：{group_openid}\n你的成员 OpenID：{member_openid}"
        )

    @qq_admin_command(group_admin, "信息", alias={"info"})
    async def group_info(self, event: AstrMessageEvent):
        """查询当前群基本信息。"""
        _, group_openid, _ = self._context(event)
        data = await self._api(event).get_group_info(group_openid)
        yield event.plain_result(
            "\n".join(
                [
                    f"群名称：{self._value(data.get('group_name'))}",
                    f"群 OpenID：{self._value(data.get('group_openid'))}",
                    f"简介：{self._value(data.get('group_finger_memo'))}",
                    f"分类：{self._value(data.get('group_class_text'))}",
                    f"标签：{self._list(data.get('group_tags'))}",
                    f"成员数：{self._value(data.get('group_member_num'))}",
                ]
            )
        )

    @qq_admin_command(group_admin, "机器人", alias={"bot"})
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

    @qq_admin_command(join_admin, "列表", alias={"list"})
    async def join_list(
        self,
        event: AstrMessageEvent,
        limit: int = 20,
        cursor: str = "",
    ):
        """分页查询入群申请。"""
        _, group_openid, _ = self._context(event)
        data = await self._api(event).list_join_requests(
            group_openid,
            limit=limit,
            cursor=cursor,
        )
        requests = data.get("list") or []
        lines = [f"入群申请：{len(requests)} 条"]
        for index, item in enumerate(requests, 1):
            verify = item.get("verify_info") or {}
            if verify.get("method") == "admin_review_qa":
                qa = "；".join(
                    f"{entry.get('question', '-')}：{entry.get('answer', '-')}"
                    for entry in verify.get("review_qa_list") or []
                )
                verification = qa or "-"
            else:
                verification = self._value(verify.get("verify_message"))
            lines.extend(
                [
                    f"\n{index}. {self._value(item.get('username'))}",
                    f"成员 OpenID：{self._value(item.get('member_openid'))}",
                    f"申请 ID：{self._value(item.get('join_request_id'))}",
                    f"时间：{self._value(item.get('apply_at'))}",
                    f"来源：{self._value(item.get('apply_source'))}",
                    f"验证：{verification}",
                    f"风险提示：{self._value(item.get('risk_tips'))}",
                ]
            )
        lines.append(f"\n下一页游标：{self._value(data.get('next_cursor'))}")
        for result in self._results(event, "\n".join(lines)):
            yield result

    @qq_admin_command(join_admin, "同意", alias={"approve"})
    async def join_approve(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        join_request_id: str,
    ):
        """通过一项入群申请。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        await self._api(event).approve_join_request(
            group_openid,
            member,
            op="approve",
            join_request_id=join_request_id,
        )
        yield event.plain_result("已通过入群申请。")

    @qq_admin_command(join_admin, "拒绝", alias={"decline"})
    async def join_decline(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        join_request_id: str,
        reason: GreedyStr,
    ):
        """拒绝入群申请，理由填 - 表示不提供。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        await self._api(event).approve_join_request(
            group_openid,
            member,
            op="decline",
            join_request_id=join_request_id,
            reject_reason="" if reason == "-" else str(reason),
        )
        yield event.plain_result("已拒绝入群申请。")

    @qq_admin_command(join_admin, "拒绝拉黑", alias={"decline-block"})
    async def join_decline_and_block(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        join_request_id: str,
        confirmation: str,
        reason: GreedyStr,
    ):
        """拒绝申请并加入群黑名单。"""
        self._confirm(confirmation)
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        await self._api(event).approve_join_request(
            group_openid,
            member,
            op="decline",
            join_request_id=join_request_id,
            reject_reason="" if reason == "-" else str(reason),
            add_to_member_blacklist=True,
        )
        yield event.plain_result("已拒绝入群申请并加入群黑名单。")

    @qq_admin_command(mute_admin, "状态", alias={"status"})
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
        op: str,
    ) -> str:
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        expire_at = future_rfc3339(parse_duration(duration))
        await self._api(event).set_member_mutes(
            group_openid,
            [{"op": op, "member_openid": member, "mute_expire_at": expire_at}],
        )
        return expire_at

    @qq_admin_command(mute_admin, "添加", alias={"add"})
    async def mute_add(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
    ):
        """新增成员禁言，最长 30 天。"""
        expire_at = await self._set_mute(event, member_openid, duration, "add")
        yield event.plain_result(f"已设置禁言，至 {expire_at}。")

    @qq_admin_command(mute_admin, "更新", alias={"update"})
    async def mute_update(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str,
    ):
        """更新已有成员禁言的到期时间。"""
        expire_at = await self._set_mute(event, member_openid, duration, "update")
        yield event.plain_result(f"已更新禁言，至 {expire_at}。")

    @qq_admin_command(mute_admin, "解除", alias={"del", "remove"})
    async def mute_remove(self, event: AstrMessageEvent, member_openid: str):
        """立即解除成员禁言。"""
        _, group_openid, _ = self._context(event)
        member = self._target_member(event, member_openid)
        await self._api(event).set_member_mutes(
            group_openid,
            [{"op": "del", "member_openid": member, "mute_expire_at": ""}],
        )
        yield event.plain_result("已解除禁言。")

    @qq_admin_command(strategy_admin, "列表", alias={"list"})
    async def strategy_list(
        self,
        event: AstrMessageEvent,
        limit: int = 20,
        cursor: str = "",
    ):
        """分页查询自动审批策略。"""
        self._context(event)
        data = await self._api(event).list_strategies(limit=limit, cursor=cursor)
        strategies = data.get("strategies") or []
        lines = [f"自动审批策略：{len(strategies)} 条"]
        for index, item in enumerate(strategies, 1):
            groups = item.get("group_openids") or item.get("group_ids") or []
            lines.extend(
                [
                    f"\n{index}. {self._value(item.get('strategy_id'))} [{self._value(item.get('is_enable'))}]",
                    f"关联群：{self._list(groups)}",
                    f"白名单数量：{self._value(item.get('whitelist_user_count'))}",
                    f"到期：{self._value(item.get('expire_at'))}",
                    f"备注：{self._value(item.get('remark'))}",
                ]
            )
        lines.append(f"\n下一页游标：{self._value(data.get('next_cursor'))}")
        for result in self._results(event, "\n".join(lines)):
            yield result

    async def _create_strategy(
        self,
        event: AstrMessageEvent,
        *,
        group_openids: list[str] | None = None,
        group_ids: list[int] | None = None,
        is_enable: str,
        expire_at: str,
        remark: str,
    ) -> dict[str, Any]:
        validate_choice(is_enable, {"off", "on"}, label="策略开关")
        return await self._api(event).create_strategy(
            group_openids=group_openids,
            group_ids=group_ids,
            is_enable=is_enable,
            expire_at=self._expiry(expire_at),
            remark="" if remark == "-" else str(remark),
        )

    @qq_admin_command(strategy_admin, "创建当前", alias={"create-current"})
    async def strategy_create_current(
        self,
        event: AstrMessageEvent,
        is_enable: str,
        expire_at: str,
        remark: GreedyStr,
    ):
        """为当前群创建自动审批策略。"""
        _, group_openid, _ = self._context(event)
        data = await self._create_strategy(
            event,
            group_openids=[group_openid],
            is_enable=is_enable,
            expire_at=expire_at,
            remark=remark,
        )
        yield event.plain_result(
            f"已创建策略 {self._value(data.get('strategy_id'))}，"
            f"状态 {self._value(data.get('is_enable'))}，"
            f"到期 {self._value(data.get('expire_at'))}。"
        )

    @qq_admin_command(strategy_admin, "创建OpenID", alias={"create-openid"})
    async def strategy_create_openids(
        self,
        event: AstrMessageEvent,
        group_openids: str,
        is_enable: str,
        expire_at: str,
        remark: GreedyStr,
    ):
        """按群 OpenID 创建自动审批策略。"""
        self._context(event)
        data = await self._create_strategy(
            event,
            group_openids=parse_openids(group_openids),
            is_enable=is_enable,
            expire_at=expire_at,
            remark=remark,
        )
        yield event.plain_result(f"已创建策略 {self._value(data.get('strategy_id'))}。")

    @qq_admin_command(strategy_admin, "创建群号", alias={"create-group-id"})
    async def strategy_create_group_ids(
        self,
        event: AstrMessageEvent,
        group_ids: str,
        is_enable: str,
        expire_at: str,
        remark: GreedyStr,
    ):
        """按 QQ 群号创建自动审批策略。"""
        self._context(event)
        data = await self._create_strategy(
            event,
            group_ids=parse_group_ids(group_ids),
            is_enable=is_enable,
            expire_at=expire_at,
            remark=remark,
        )
        yield event.plain_result(f"已创建策略 {self._value(data.get('strategy_id'))}。")

    @qq_admin_command(strategy_admin, "开关", alias={"enable"})
    async def strategy_enable(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        is_enable: str,
    ):
        """启用或停用策略。"""
        self._context(event)
        validate_choice(is_enable, {"off", "on"}, label="策略开关")
        data = await self._api(event).update_strategy(
            strategy_id,
            {"is_enable": is_enable},
        )
        yield event.plain_result(
            f"策略状态已更新为 {self._value(data.get('is_enable'))}。"
        )

    @qq_admin_command(strategy_admin, "到期", alias={"expire"})
    async def strategy_expire(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        expire_at: str,
    ):
        """修改策略到期时间。"""
        self._context(event)
        value = validate_rfc3339(expire_at, label="策略到期时间")
        data = await self._api(event).update_strategy(
            strategy_id,
            {"expire_at": value},
        )
        yield event.plain_result(
            f"策略到期时间已更新为 {self._value(data.get('expire_at'))}。"
        )

    @qq_admin_command(strategy_admin, "备注", alias={"remark"})
    async def strategy_remark(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        remark: GreedyStr,
    ):
        """修改策略备注，填 - 可清空。"""
        self._context(event)
        value = "" if remark == "-" else str(remark)
        if len(value) > 255:
            raise ValueError("策略备注最多 255 个字符")
        await self._api(event).update_strategy(strategy_id, {"remark": value})
        yield event.plain_result("策略备注已更新。")

    async def _strategy_groups(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        op: str,
        *,
        group_openids: list[str] | None = None,
        group_ids: list[int] | None = None,
    ) -> None:
        validate_choice(op, {"add", "del"}, label="关联群动作")
        action: dict[str, Any] = {"op": op}
        if group_openids is not None:
            action["group_openids"] = group_openids
        if group_ids is not None:
            action["group_ids"] = group_ids
        await self._api(event).update_strategy(strategy_id, {"group_action": action})

    @qq_admin_command(strategy_admin, "关联OpenID", alias={"groups-openid"})
    async def strategy_groups_openid(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        op: str,
        group_openids: str,
    ):
        """增删策略关联的群 OpenID。"""
        self._context(event)
        await self._strategy_groups(
            event,
            strategy_id,
            op,
            group_openids=parse_openids(group_openids),
        )
        yield event.plain_result("策略关联群已更新。")

    @qq_admin_command(strategy_admin, "关联群号", alias={"groups-id"})
    async def strategy_groups_id(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        op: str,
        group_ids: str,
    ):
        """增删策略关联的 QQ 群号。"""
        self._context(event)
        await self._strategy_groups(
            event,
            strategy_id,
            op,
            group_ids=parse_group_ids(group_ids),
        )
        yield event.plain_result("策略关联群已更新。")

    @qq_admin_command(strategy_admin, "白名单", alias={"whitelist"})
    async def strategy_whitelist(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        op: str,
        users: str,
    ):
        """批量增删策略白名单 QQ 号码。"""
        self._context(event)
        data = await self._api(event).update_whitelist(
            strategy_id,
            op=op,
            users=parse_qq_numbers(users),
        )
        yield event.plain_result(
            f"白名单已更新，当前约 {self._value(data.get('whitelist_user_count'))} 人。"
        )

    @qq_admin_command(strategy_admin, "删除", alias={"delete"})
    async def strategy_delete(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        confirmation: str,
    ):
        """永久删除策略，需要确认。"""
        self._context(event)
        self._confirm(confirmation)
        await self._api(event).delete_strategy(strategy_id)
        yield event.plain_result("策略已删除。")

    @qq_admin_command(strategy_admin, "执行", alias={"execute"})
    async def strategy_execute(
        self,
        event: AstrMessageEvent,
        strategy_id: str,
        confirmation: str,
    ):
        """全量执行策略，需要确认。"""
        self._context(event)
        self._confirm(confirmation)
        await self._api(event).execute_strategy(strategy_id)
        yield event.plain_result("策略已开始异步执行，官方预计约 10 分钟完成。")
