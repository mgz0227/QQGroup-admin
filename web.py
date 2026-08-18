from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable

from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .qq_api import parse_qq_number_text
from .review import parse_keywords

PLUGIN_NAME = "astrbot_plugin_qqgroup_admin"


class GroupAdminWeb:
    """Small WebUI adapter; QQ strategy changes stay in the main plugin."""

    def __init__(self, plugin: Any, context: Context) -> None:
        self.plugin = plugin
        self.context = context

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
    def _text(
        value: Any,
        label: str,
        limit: int,
        *,
        required: bool = False,
        multiline: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label}格式错误")
        value = value.strip()
        if required and not value:
            raise ValueError(f"{label}不能为空")
        if len(value) > limit:
            raise ValueError(f"{label}最多 {limit} 个字符")
        allowed_controls = "\r\n\t" if multiline else ""
        if any(ord(char) < 32 and char not in allowed_controls for char in value):
            raise ValueError(f"{label}包含非法控制字符")
        return value

    @classmethod
    def _validated_save(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")

        group_openid = cls._text(
            payload.get("group_openid"), "群 OpenID", 128, required=True
        )
        if any(char.isspace() for char in group_openid):
            raise ValueError("群 OpenID 不能包含空白字符")
        raw_mode = cls._text(
            payload.get("mode"), "审核方式", 16, required=True
        )
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
            payload.get(
                "reject_keywords", payload.get("uid_reject_keywords", "")
            ),
            "拒绝关键词",
            7_000,
            multiline=True,
        )
        uid_check_enabled = payload.get(
            "uid_check_enabled", raw_mode == "uid"
        )
        if not isinstance(uid_check_enabled, bool):
            raise ValueError("UID 检查开关必须是布尔值")
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
            raise ValueError("扫描待审申请必须是布尔值")

        reject_keywords = "\n".join(parse_keywords(reject_text))

        return {
            "group_openid": group_openid,
            "mode": mode,
            "whitelist_qq_numbers": "\n".join(
                parse_qq_number_text(whitelist_text)
            ),
            "uid_check_enabled": uid_check_enabled,
            "approve_keywords": "\n".join(parse_keywords(approve_text)),
            "reject_keywords": reject_keywords,
            "uid_reject_keywords": reject_keywords,
            "condition_logic": condition_logic,
            "fallback_action": fallback_action,
            "scan_pending": scan_pending,
            "button_reject_reason": reject_reason or "管理员拒绝",
        }

    @classmethod
    def _validated_group(cls, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        group_openid = cls._text(
            payload.get("group_openid"), "群 OpenID", 128, required=True
        )
        if any(char.isspace() for char in group_openid):
            raise ValueError("群 OpenID 不能包含空白字符")
        return group_openid

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
                "pending": sum(
                    not bool(item.get("synchronized")) for item in groups
                ),
            }
        )

    async def page_list(self) -> Any:
        return self._response(await self._groups())

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
            except Exception:  # noqa: BLE001 - WebUI trust boundary
                self.plugin.logger.exception("QQ群管理页面请求失败")
                return self._error("服务器处理请求失败", 500)

        return wrapped

    def register_routes(self) -> None:
        routes = (
            ("/overview", self.page_overview, ["GET"], "QQ群审核概览"),
            ("/list", self.page_list, ["GET"], "QQ群审核配置列表"),
            ("/save", self.page_save, ["POST"], "保存QQ群审核配置"),
            ("/delete", self.page_delete, ["POST"], "移除QQ群审核配置"),
            ("/sync", self.page_sync, ["POST"], "应用QQ群审核配置"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                self._wrap(handler),
                methods,
                description,
            )
