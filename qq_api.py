from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

API_BASE = "https://api.bot.qq.com"
MAX_MUTE_DURATION = timedelta(days=30)
UINT64_MAX = (1 << 64) - 1
RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)

ERROR_HINTS = {
    11253: "应用未获群聊管理接口权限；这些接口目前仅对白名单机器人开放",
    11254: "应用的群聊管理接口已被封禁",
    11265: "机器人已被封禁",
    11282: "机器人不是群管理员，或入群时未获得管理员授权",
    12002: "QQ API 拒绝了请求参数",
}


class QQAPIError(RuntimeError):
    def __init__(
        self,
        *,
        status: int | None = None,
        err_code: int | None = None,
        message: str = "",
        trace_id: str = "",
    ) -> None:
        self.status = status
        self.err_code = err_code
        self.message = message
        self.trace_id = trace_id
        super().__init__(self._format())

    def _format(self) -> str:
        if self.status == 429:
            summary = "QQ API 请求过于频繁，请稍后重试"
        else:
            summary = ERROR_HINTS.get(self.err_code, self.message or "QQ API 请求失败")

        details = []
        if self.err_code is not None:
            details.append(f"错误码 {self.err_code}")
        elif self.status is not None:
            details.append(f"HTTP {self.status}")
        if self.trace_id:
            details.append(f"trace_id {self.trace_id}")
        return f"{summary}（{'；'.join(details)}）" if details else summary


def parse_csv(value: str, *, label: str, max_items: int) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    if not items or any(not item for item in items):
        raise ValueError(f"{label}不能为空，多个值请用英文逗号分隔")
    if len(items) > max_items:
        raise ValueError(f"{label}单次最多 {max_items} 个")
    if any(any(ord(char) < 32 for char in item) for item in items):
        raise ValueError(f"{label}包含非法控制字符")
    return list(dict.fromkeys(items))


def parse_openids(value: str, *, max_items: int = 100) -> list[str]:
    return parse_csv(value, label="OpenID", max_items=max_items)


def parse_group_ids(value: str) -> list[int]:
    raw_ids = parse_csv(value, label="群号", max_items=100)
    if any(not item.isdigit() for item in raw_ids):
        raise ValueError("群号只能包含数字")
    ids = [int(item) for item in raw_ids]
    if any(item <= 0 or item > UINT64_MAX for item in ids):
        raise ValueError("群号超出 uint64 范围")
    return ids


def parse_qq_numbers(value: str) -> list[str]:
    numbers = parse_csv(value, label="QQ 号码", max_items=10_000)
    if any(not item.isdigit() for item in numbers):
        raise ValueError("QQ 号码只能包含数字")
    return numbers


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9]\d*)([smhd])", value.strip().lower())
    if not match:
        raise ValueError("时长格式应为 30m、2h 或 7d，最大 30d")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    duration = timedelta(seconds=seconds)
    if duration > MAX_MUTE_DURATION:
        raise ValueError("成员最长只能禁言 30 天")
    return duration


def future_rfc3339(duration: timedelta) -> str:
    value = (datetime.now(timezone.utc) + duration).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def validate_rfc3339(value: str, *, label: str = "时间") -> str:
    if not RFC3339_PATTERN.fullmatch(value):
        raise ValueError(f"{label}必须是带时区的 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}必须是带时区的 RFC3339 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}必须包含时区")
    return value


def validate_choice(value: str, choices: set[str], *, label: str) -> str:
    if value not in choices:
        raise ValueError(f"{label}只能是 {'/'.join(sorted(choices))}")
    return value


class QQGroupAPI:
    """QQ group-management calls over AstrBot's authenticated botpy transport."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        http = getattr(self.client, "http", None)
        if http is None:
            raise QQAPIError(message="AstrBot QQ 客户端不可用")

        try:
            await http.check_session()
            session = getattr(http, "_session", None)
            headers = getattr(http, "_headers", None)
            if session is None or headers is None:
                raise QQAPIError(message="AstrBot QQ 鉴权会话尚未就绪")

            kwargs: dict[str, Any] = {}
            if body is not None:
                kwargs["json"] = body

            # botpy 1.2.1 still hard-codes the legacy API domain. Reusing its
            # refreshed session keeps credentials in AstrBot while honoring the
            # current official api.bot.qq.com base URL and preserving trace IDs.
            async with session.request(
                method,
                API_BASE + path,
                headers=dict(headers),
                timeout=getattr(http, "timeout", 10),
                **kwargs,
            ) as response:
                raw = await response.text()
                trace_id = response.headers.get("X-Tps-trace-ID", "")
                try:
                    data: Any = json.loads(raw) if raw else None
                except json.JSONDecodeError as exc:
                    raise QQAPIError(
                        status=response.status,
                        message="QQ API 返回了非 JSON 响应",
                        trace_id=trace_id,
                    ) from exc

                err_code = None
                message = ""
                if isinstance(data, dict):
                    raw_code = data.get("err_code")
                    try:
                        err_code = int(raw_code) if raw_code is not None else None
                    except (TypeError, ValueError):
                        err_code = None
                    message = str(data.get("message") or "")
                    trace_id = str(data.get("trace_id") or trace_id)

                failed = not 200 <= response.status < 300 or err_code not in {None, 0}
                if failed:
                    raise QQAPIError(
                        status=response.status,
                        err_code=err_code,
                        message=message,
                        trace_id=trace_id,
                    )
                return data
        except QQAPIError:
            raise
        except Exception as exc:
            raise QQAPIError(message=f"QQ API 网络请求失败：{exc}") from exc

    @staticmethod
    def _id(value: str, label: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{label}不能为空")
        return quote(value, safe="")

    async def get_group_info(self, group_openid: str) -> dict[str, Any]:
        group = self._id(group_openid, "群 OpenID")
        return await self._request("GET", f"/v2/groups/{group}/info")

    async def get_bot_state(self, group_openid: str) -> dict[str, Any]:
        group = self._id(group_openid, "群 OpenID")
        return await self._request("GET", f"/v2/groups/{group}/bot_state")

    async def list_join_requests(
        self,
        group_openid: str,
        *,
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        group = self._id(group_openid, "群 OpenID")
        body: dict[str, Any] = {"limit": limit}
        if cursor:
            body["cursor"] = cursor
        return await self._request(
            "GET",
            f"/v2/groups/{group}/join_request_list",
            body=body,
        )

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str,
        reject_reason: str = "",
        add_to_member_blacklist: bool = False,
    ) -> Any:
        validate_choice(op, {"approve", "decline"}, label="审批动作")
        group = self._id(group_openid, "群 OpenID")
        member = self._id(member_openid, "成员 OpenID")
        body: dict[str, Any] = {"op": op, "join_request_id": join_request_id}
        if op == "decline":
            if reject_reason:
                body["reject_reason"] = reject_reason
            if add_to_member_blacklist:
                body["add_to_member_blacklist"] = True
        return await self._request(
            "POST",
            f"/v2/groups/{group}/approval_join_request/{member}",
            body=body,
        )

    async def get_mute_state(self, group_openid: str) -> dict[str, Any]:
        group = self._id(group_openid, "群 OpenID")
        return await self._request(
            "GET",
            f"/v2/groups/{group}/restrict_chat_setting",
        )

    async def set_member_mutes(
        self,
        group_openid: str,
        members: list[dict[str, Any]],
    ) -> Any:
        if not 1 <= len(members) <= 10:
            raise ValueError("每次必须设置 1 到 10 个成员")
        for member in members:
            op = validate_choice(
                str(member.get("op") or ""),
                {"add", "del", "update"},
                label="禁言动作",
            )
            self._id(str(member.get("member_openid") or ""), "成员 OpenID")
            if op != "del":
                expire_at = str(member.get("mute_expire_at") or "")
                validate_rfc3339(expire_at)
                expires = datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
                remaining = expires.astimezone(timezone.utc) - datetime.now(
                    timezone.utc
                )
                if remaining <= timedelta(0) or remaining > MAX_MUTE_DURATION:
                    raise ValueError("禁言到期时间必须在未来 30 天内")
        group = self._id(group_openid, "群 OpenID")
        return await self._request(
            "POST",
            f"/v2/groups/{group}/restrict_chat_setting",
            body={"members": members},
        )

    async def list_strategies(
        self,
        *,
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        body: dict[str, Any] = {"limit": limit}
        if cursor:
            body["cursor"] = cursor
        return await self._request(
            "GET",
            "/v2/groups/join_approval_strategy",
            body=body,
        )

    async def create_strategy(
        self,
        *,
        group_openids: list[str] | None = None,
        group_ids: list[int] | None = None,
        is_enable: str = "on",
        expire_at: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        if bool(group_openids) == bool(group_ids):
            raise ValueError("group_openids 与 group_ids 必须且只能提供一种")
        validate_choice(is_enable, {"off", "on"}, label="策略开关")
        body: dict[str, Any] = {"is_enable": is_enable}
        if group_openids:
            if len(group_openids) > 100:
                raise ValueError("单个策略最多关联 100 个群")
            body["group_openids"] = group_openids
        if group_ids:
            if len(group_ids) > 100:
                raise ValueError("单个策略最多关联 100 个群")
            body["group_ids"] = group_ids
        if expire_at:
            body["expire_at"] = validate_rfc3339(expire_at, label="策略到期时间")
        if len(remark) > 255:
            raise ValueError("策略备注最多 255 个字符")
        if remark:
            body["remark"] = remark
        return await self._request(
            "POST",
            "/v2/groups/join_approval_strategy",
            body=body,
        )

    async def update_strategy(
        self,
        strategy_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if not body:
            raise ValueError("没有需要修改的策略字段")
        strategy = self._id(strategy_id, "策略 ID")
        return await self._request(
            "PATCH",
            f"/v2/groups/join_approval_strategy/{strategy}",
            body=body,
        )

    async def delete_strategy(self, strategy_id: str) -> Any:
        strategy = self._id(strategy_id, "策略 ID")
        return await self._request(
            "DELETE",
            f"/v2/groups/join_approval_strategy/{strategy}",
            body={},
        )

    async def update_whitelist(
        self,
        strategy_id: str,
        *,
        op: str,
        users: list[str],
    ) -> dict[str, Any]:
        validate_choice(op, {"add", "del"}, label="白名单动作")
        if not 1 <= len(users) <= 10_000:
            raise ValueError("每次必须提交 1 到 10000 个 QQ 号码")
        strategy = self._id(strategy_id, "策略 ID")
        return await self._request(
            "POST",
            f"/v2/groups/join_approval_strategy/{strategy}/whitelist_users",
            body={"op": op, "whitelist_users": users},
        )

    async def execute_strategy(self, strategy_id: str) -> Any:
        strategy = self._id(strategy_id, "策略 ID")
        return await self._request(
            "POST",
            f"/v2/groups/join_approval_strategy/{strategy}/execute",
            body={},
        )
