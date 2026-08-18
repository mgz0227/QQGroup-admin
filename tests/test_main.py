import logging
import sys
import types
import unittest
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def identity_decorator(*_args, **_kwargs):
    return lambda function: function


class TestConfig(dict):
    def save_config(self):
        pass


class TestStar:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("qqgroup-admin-test")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_event = types.ModuleType("astrbot.api.event")
astrbot_star = types.ModuleType("astrbot.api.star")
astrbot_api.AstrBotConfig = TestConfig
astrbot_event.AstrMessageEvent = object
astrbot_event.filter = SimpleNamespace(
    PlatformAdapterType=SimpleNamespace(QQOFFICIAL=1, QQOFFICIAL_WEBHOOK=2),
    EventMessageType=SimpleNamespace(GROUP_MESSAGE=1),
    PermissionType=SimpleNamespace(ADMIN=1),
    command=identity_decorator,
    platform_adapter_type=identity_decorator,
    event_message_type=identity_decorator,
    permission_type=identity_decorator,
    on_platform_loaded=identity_decorator,
)
astrbot_star.Context = object
astrbot_star.Star = TestStar
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.star": astrbot_star,
    }
)

package_name = "qqgroup_admin_test"
package = types.ModuleType(package_name)
package.__path__ = [str(ROOT)]
sys.modules[package_name] = package
spec = util.spec_from_file_location(
    f"{package_name}.main",
    ROOT / "main.py",
)
module = util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeClientAPI:
    def __init__(self):
        self.messages = []
        self.acks = []

    async def post_group_message(self, **kwargs):
        self.messages.append(kwargs)

    async def on_interaction_result(self, interaction_id, code):
        self.acks.append((interaction_id, code))


class FakeEvent:
    def __init__(self, client):
        self.bot = client
        self.message_obj = SimpleNamespace(
            message_id="message-1",
            raw_message=SimpleNamespace(
                group_openid="group-1",
                author=SimpleNamespace(member_openid="admin-1"),
            ),
        )

    def get_platform_id(self):
        return "platform-1"

    def plain_result(self, text):
        return text


class PluginFlowTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        client = SimpleNamespace(api=FakeClientAPI())
        platform = SimpleNamespace(get_client=lambda: client)
        context = SimpleNamespace(get_platform_inst=lambda _platform_id: platform)
        config = TestConfig(
            auto_review_groups=[
                {
                    "group_openid": "group-1",
                    "button_reject_reason": "管理员拒绝",
                }
            ]
        )
        return module.QQGroupAdmin(context, config), client

    async def test_join_list_uses_manager_callback_buttons(self):
        plugin, client = self.plugin()
        event = FakeEvent(client)
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={
                    "list": [
                        {
                            "username": "申请人",
                            "member_openid": "member-1",
                            "join_request_id": "request-1",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "UID:188144093"},
                        }
                    ],
                    "next_cursor": "next",
                }
            )
        )
        plugin._api = lambda _event: api

        results = [result async for result in plugin.join_list(event)]

        self.assertEqual(results, [])
        message = client.api.messages[0]
        buttons = message["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual([button["action"]["type"] for button in buttons], [1, 1])
        self.assertEqual(
            [button["action"]["permission"]["type"] for button in buttons],
            [1, 1],
        )
        self.assertFalse(hasattr(plugin, "join_approve"))

        approvals = []

        async def approve(*args, **kwargs):
            approvals.append((args, kwargs))
            plugin._forget_request_tokens(args[1], args[3])

        plugin._approve_request = approve
        button_data = buttons[0]["action"]["data"]
        interaction = SimpleNamespace(
            id="interaction-1",
            type=11,
            chat_type=1,
            group_openid="group-1",
            data=SimpleNamespace(resolved=SimpleNamespace(button_data=button_data)),
        )
        await plugin._handle_interaction(client, interaction)
        interaction.id = "interaction-2"
        await plugin._handle_interaction(client, interaction)

        self.assertEqual(approvals[0][1]["op"], "approve")
        self.assertEqual(client.api.acks, [("interaction-1", 0), ("interaction-2", 3)])

        async def rate_limited(*_args, **_kwargs):
            raise module.QQAPIError(status=429)

        plugin._approve_request = rate_limited
        token = plugin._approval_token("group-1", "member-2", "request-2")
        interaction.id = "interaction-3"
        interaction.data.resolved.button_data = f"qqga:{token}:approve"
        await plugin._handle_interaction(client, interaction)
        self.assertEqual(client.api.acks[-1], ("interaction-3", 2))

    def test_uid_review_waits_for_native_strategy_sync(self):
        plugin, _ = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            {
                "enabled": False,
                "uid_review_enabled": True,
                "platform_id": "platform-1",
                "managed_strategy_id": "native-strategy",
            }
        )

        self.assertEqual(plugin._uid_review_entries(), [])
        entry["managed_strategy_id"] = ""
        self.assertEqual(
            plugin._uid_review_entries(),
            [("platform-1", "group-1", [])],
        )

    async def test_uid_review_rejects_keyword_before_uid_format(self):
        plugin, _ = self.plugin()
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={
                    "list": [
                        {
                            "member_openid": "member-1",
                            "join_request_id": "request-1",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "广告 UID:188144093"},
                        },
                        {
                            "member_openid": "member-2",
                            "join_request_id": "request-2",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "没有 UID"},
                        },
                        {
                            "member_openid": "member-3",
                            "join_request_id": "request-3",
                            "apply_source": "self_apply",
                            "verify_info": {
                                "review_qa_list": [
                                    {"answer": "188144093"},
                                    {"answer": "从主页看到的"},
                                ]
                            },
                        },
                    ],
                    "next_cursor": "",
                }
            ),
            approve_join_request=AsyncMock(),
        )
        lookup = AsyncMock(return_value=True)
        sleeper = AsyncMock()
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(module, "bilibili_uid_exists", lookup),
            patch.object(module.asyncio, "sleep", sleeper),
        ):
            await plugin._poll_uid_group(
                object(),
                "platform-1",
                "group-1",
                ["广告"],
            )

        lookup.assert_awaited_once_with("188144093")
        reasons = [
            call.kwargs["reject_reason"]
            for call in api.approve_join_request.await_args_list
        ]
        self.assertEqual(
            reasons,
            ["验证消息包含拒绝关键词", "未提供有效的 B 站 UID", ""],
        )
        self.assertTrue(any(call.args[0] >= 1 for call in sleeper.await_args_list))


if __name__ == "__main__":
    unittest.main()
