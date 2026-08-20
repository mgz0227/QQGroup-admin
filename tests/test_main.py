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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class TestStar:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("qqgroup-admin-test")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False
        self._kv = {}

    async def get_kv_data(self, key, default=None):
        return self._kv.get(key, default)

    async def put_kv_data(self, key, value):
        self._kv[key] = value


class TestCustomFilter:
    def __init__(self, raise_error=True, **_kwargs):
        self.raise_error = raise_error


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_event = types.ModuleType("astrbot.api.event")
astrbot_star = types.ModuleType("astrbot.api.star")
astrbot_web = types.ModuleType("astrbot.api.web")
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
    regex=identity_decorator,
    custom_filter=identity_decorator,
    CustomFilter=TestCustomFilter,
)
astrbot_star.Context = object
astrbot_star.Star = TestStar
astrbot_web.error_response = lambda message, status_code=500: (message, status_code)
astrbot_web.json_response = lambda data: data
astrbot_web.request = SimpleNamespace()
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.star": astrbot_star,
        "astrbot.api.web": astrbot_web,
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
        return SimpleNamespace(id=f"sent-{len(self.messages)}")

    async def on_interaction_result(self, interaction_id, code):
        self.acks.append((interaction_id, code))


class FakeClient:
    def __init__(self):
        self.api = FakeClientAPI()
        self.intents = 0


class FakeEvent:
    def __init__(self, client, message_str=""):
        self.bot = client
        self.message_str = message_str
        self.stopped = False
        self.is_at_or_wake_command = True
        self.message_obj = SimpleNamespace(
            message_id="message-1",
            message=[],
            raw_message=SimpleNamespace(
                group_openid="group-1",
                author=SimpleNamespace(member_openid="admin-1"),
                mentions=[],
            ),
        )

    def get_platform_id(self):
        return "platform-1"

    def plain_result(self, text):
        return text

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self.message_obj.message

    def get_group_id(self):
        return self.message_obj.raw_message.group_openid

    def get_sender_id(self):
        return self.message_obj.raw_message.author.member_openid

    def stop_event(self):
        self.stopped = True


class PluginFlowTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        client = FakeClient()
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
                    "next_cursor": "next_page",
                }
            )
        )
        plugin._api = lambda _event: api

        results = [result async for result in plugin.join_list(event)]

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        message = client.api.messages[0]
        self.assertEqual(message["msg_type"], 2)
        self.assertIn("markdown", message)
        self.assertNotIn("content", message)
        self.assertIn("/申请列表 next\\_page", message["markdown"]["content"])
        buttons = message["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual([button["action"]["type"] for button in buttons], [1, 1])
        self.assertEqual(
            [button["action"]["permission"]["type"] for button in buttons],
            [1, 1],
        )
        self.assertFalse(hasattr(plugin, "join_approve"))

        approvals = []

        async def approve(*args, **kwargs):
            self.assertEqual(client.api.acks[-1], ("interaction-1", 0))
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
        self.assertTrue(await plugin._handle_interaction(client, interaction))
        interaction.id = "interaction-2"
        self.assertTrue(await plugin._handle_interaction(client, interaction))

        self.assertEqual(approvals[0][1]["op"], "approve")
        self.assertEqual(client.api.acks, [("interaction-1", 0), ("interaction-2", 3)])

        async def rate_limited(*_args, **_kwargs):
            raise module.QQAPIError(status=429)

        plugin._approve_request = rate_limited
        token = plugin._approval_token("group-1", "member-2", "request-2")
        interaction.id = "interaction-3"
        interaction.data.resolved.button_data = f"qqga:{token}:approve"
        self.assertTrue(await plugin._handle_interaction(client, interaction))
        self.assertEqual(client.api.acks[-1], ("interaction-3", 0))

        interaction.id = "interaction-4"
        interaction.data.resolved.button_data = "other:token:action"
        self.assertFalse(await plugin._handle_interaction(client, interaction))
        self.assertNotIn(("interaction-4", 1), client.api.acks)

    async def test_interaction_handler_chains_once_and_restores_previous(self):
        plugin, client = self.plugin()
        previous = AsyncMock()
        client.on_interaction_create = previous
        platform = SimpleNamespace(get_client=lambda: client)
        plugin._qq_platforms = lambda: [platform, platform]

        plugin._patch_qq_clients()
        installed = client.on_interaction_create
        plugin._patch_qq_clients()

        self.assertIs(client.on_interaction_create, installed)
        self.assertTrue(client.intents & module.INTERACTION_INTENT)
        interaction = SimpleNamespace(
            id="foreign",
            type=11,
            chat_type=1,
            group_openid="group-1",
            data=SimpleNamespace(
                resolved=SimpleNamespace(button_data="another:token:button")
            ),
        )
        await client.on_interaction_create(interaction)
        previous.assert_awaited_once_with(interaction)
        self.assertEqual(client.api.acks, [])

        await plugin.terminate()
        self.assertIs(client.on_interaction_create, previous)

    async def test_settings_button_binds_named_group_without_webui_entry(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"] = []
        event = FakeEvent(client)
        api = SimpleNamespace(
            get_group_info=AsyncMock(return_value={"group_name": "测试群"}),
            get_bot_state=AsyncMock(return_value={"member_role": "admin"}),
            list_strategies=AsyncMock(return_value={"strategies": []}),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(plugin, "_schedule_settings_recall") as schedule_recall,
        ):
            results = [result async for result in plugin.review_settings(event)]
        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertEqual(plugin.config["auto_review_groups"], [])

        self.assertEqual(client.api.messages[0]["msg_type"], 2)
        self.assertIn("markdown", client.api.messages[0])
        self.assertNotIn("content", client.api.messages[0])
        buttons = [
            button
            for row in client.api.messages[0]["keyboard"]["content"]["rows"]
            for button in row["buttons"]
        ]
        self.assertEqual(
            [button["action"]["data"].rsplit(":", 1)[1] for button in buttons],
            [
                "bind",
                "native",
                "conditional",
                "off",
                "uid_on",
                "uid_off",
                "direct_on",
                "direct_off",
                "all",
                "any",
                "pending",
                "decline",
                "approve",
                "moderation",
                "sync",
            ],
        )
        self.assertEqual(
            [button["render_data"]["label"] for button in buttons[:8]],
            ["绑定", "白名单", "条件", "关闭", "UID开", "UID关", "直通开", "直通关"],
        )
        self.assertTrue(
            all(
                button["render_data"]["visited_label"] == button["render_data"]["label"]
                for button in buttons
            )
        )
        self.assertTrue(
            all(button["action"]["permission"] == {"type": 1} for button in buttons)
        )

        schedule_recall.assert_called_once_with(client, "group-1", "sent-1")
        uid_data = buttons[2]["action"]["data"]
        interaction = SimpleNamespace(
            id="settings-wrong-group",
            type=11,
            chat_type=1,
            group_openid="other-group",
            data=SimpleNamespace(resolved=SimpleNamespace(button_data=uid_data)),
        )
        with patch.object(module, "QQGroupAPI", return_value=api):
            self.assertTrue(await plugin._handle_interaction(client, interaction))
        self.assertEqual(plugin.config["auto_review_groups"], [])
        self.assertEqual(client.api.acks[-1], ("settings-wrong-group", 4))

        interaction.id = "settings-uid"
        interaction.group_openid = "group-1"
        with patch.object(module, "QQGroupAPI", return_value=api):
            self.assertTrue(await plugin._handle_interaction(client, interaction))
        entry = plugin.config["auto_review_groups"][0]
        self.assertEqual(entry["group_name"], "测试群")
        self.assertEqual(entry["group_openid"], "group-1")
        self.assertEqual(entry["__template_key"], "qq_group")
        self.assertEqual(entry["platform_id"], "platform-1")
        self.assertTrue(entry["uid_review_enabled"])
        self.assertFalse(entry["enabled"])

        interaction.id = "settings-off"
        interaction.data.resolved.button_data = uid_data.rsplit(":", 1)[0] + ":off"
        with patch.object(module, "QQGroupAPI", return_value=api):
            self.assertTrue(await plugin._handle_interaction(client, interaction))
        self.assertFalse(entry["uid_review_enabled"])
        self.assertFalse(entry["enabled"])
        self.assertEqual(client.api.acks[-1], ("settings-off", 0))
        self.assertGreater(plugin.config.save_count, 0)

    async def test_settings_buttons_update_conditions_and_recall(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]

        for action, expected in (
            ("direct_on", ("uid_exists_auto_approve", True)),
            ("any", ("condition_logic", "any")),
            ("approve", ("fallback_action", "approve")),
            ("uid_off", ("uid_check_enabled", False)),
        ):
            await plugin._apply_settings_button(
                client,
                "group-1",
                "platform-1",
                action,
                "测试群",
            )
            self.assertEqual(entry[expected[0]], expected[1])
        self.assertFalse(entry["uid_exists_auto_approve"])

        api = SimpleNamespace(recall_group_message=AsyncMock())
        with (
            patch.object(module.asyncio, "sleep", AsyncMock()) as sleep,
            patch.object(module, "QQGroupAPI", return_value=api),
        ):
            await plugin._recall_settings_message(client, "group-1", "sent-1")
        sleep.assert_awaited_once_with(module.SETTINGS_MESSAGE_TTL)
        api.recall_group_message.assert_awaited_once_with("group-1", "sent-1")

    async def test_settings_panel_can_disable_auto_recall(self):
        plugin, client = self.plugin()
        plugin.config["settings_panel_auto_recall"] = False
        event = FakeEvent(client)
        api = SimpleNamespace(
            get_group_info=AsyncMock(return_value={"group_name": "测试群"}),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(plugin, "_schedule_settings_recall") as schedule_recall,
        ):
            results = [result async for result in plugin.review_settings(event)]

        self.assertEqual(results, [])
        schedule_recall.assert_not_called()
        self.assertIn(
            "面板不会自动撤回",
            client.api.messages[-1]["markdown"]["content"],
        )

    async def test_settings_command_can_be_disabled(self):
        plugin, client = self.plugin()
        plugin.config["settings_command_enabled"] = False
        event = FakeEvent(client)

        results = [result async for result in plugin.review_settings(event)]

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertEqual(client.api.messages, [])

    async def test_uid_button_does_not_take_over_unmanaged_strategy(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            {
                "enabled": False,
                "uid_review_enabled": False,
                "platform_id": "platform-1",
                "managed_strategy_id": "",
            }
        )
        api = SimpleNamespace(
            get_bot_state=AsyncMock(return_value={"member_role": "admin"}),
            list_strategies=AsyncMock(
                return_value={
                    "strategies": [
                        {
                            "strategy_id": "external-strategy",
                            "group_openids": ["group-1"],
                        }
                    ]
                }
            ),
            delete_strategy=AsyncMock(),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            self.assertRaisesRegex(ValueError, "未由本插件管理"),
        ):
            await plugin._sync_group_config(
                client,
                "group-1",
                entry,
                "platform-1",
                native_enabled=False,
                uid_enabled=True,
            )

        self.assertFalse(entry["uid_review_enabled"])
        api.delete_strategy.assert_not_awaited()

    async def test_sync_rejects_non_admin_role_before_changing_config(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        api = SimpleNamespace(
            get_bot_state=AsyncMock(return_value={"member_role": "member"}),
            list_strategies=AsyncMock(),
        )

        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            self.assertRaisesRegex(RuntimeError, "角色为 member"),
        ):
            await plugin._sync_group_config(
                client,
                "group-1",
                entry,
                "platform-1",
                native_enabled=False,
                uid_enabled=True,
            )

        self.assertNotIn("uid_review_enabled", entry)
        self.assertNotIn("enabled", entry)
        api.list_strategies.assert_not_awaited()

    async def test_permission_failure_logs_group_context_and_diagnoses_once(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update({"group_name": "测试群", "platform_id": "platform-1"})
        api = SimpleNamespace(
            get_bot_state=AsyncMock(return_value={"member_role": "admin"}),
        )
        error = module.QQAPIError(
            status=403,
            err_code=40011030,
            message="机器人不是群管理员",
            trace_id="trace-1",
        )

        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            self.assertLogs(plugin.logger, level="WARNING") as captured,
        ):
            await plugin._log_poll_failure(
                client,
                "platform-1",
                "group-1",
                error,
            )
            await plugin._log_poll_failure(
                client,
                "platform-1",
                "group-1",
                error,
            )

        self.assertEqual(api.get_bot_state.await_count, 1)
        output = "\n".join(captured.output)
        self.assertEqual(output.count("bot_role=admin"), 2)
        self.assertIn("group_name=测试群", output)
        self.assertIn("group=group-1", output)
        self.assertIn("platform=platform-1", output)
        self.assertIn("trace-1", output)

    def test_web_payload_validation_normalizes_lists(self):
        payload = module.GroupAdminWeb._validated_save(
            {
                "group_openid": "group-1",
                "mode": "conditional",
                "whitelist_qq_numbers": "123, 456",
                "uid_check_enabled": True,
                "uid_exists_auto_approve": True,
                "approve_keywords": "主页, 老用户",
                "reject_keywords": "广告, 引流",
                "condition_logic": "any",
                "fallback_action": "pending",
                "scan_pending": True,
                "button_reject_reason": "资料不完整",
                "keyword_replies": [
                    {
                        "keyword": "帮助",
                        "reply": "请查看群公告",
                        "match_type": "exact",
                        "enabled": True,
                    }
                ],
            }
        )
        self.assertEqual(payload["whitelist_qq_numbers"], "123\n456")
        self.assertTrue(payload["uid_exists_auto_approve"])
        self.assertEqual(payload["approve_keywords"], "主页\n老用户")
        self.assertEqual(payload["reject_keywords"], "广告\n引流")
        self.assertEqual(
            payload["keyword_replies"],
            [
                {
                    "__template_key": "keyword_reply",
                    "keyword": "帮助",
                    "reply": "请查看群公告",
                    "match_type": "exact",
                    "enabled": True,
                }
            ],
        )
        payload["uid_check_enabled"] = False
        self.assertFalse(
            module.GroupAdminWeb._validated_save(payload)["uid_exists_auto_approve"]
        )
        with self.assertRaises(ValueError):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "group_openid": "bad group",
                }
            )
        with self.assertRaises(ValueError):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "bilibili_uids": "",
                    "bilibili_live_enabled": True,
                }
            )
        with self.assertRaisesRegex(ValueError, "关键词不能为空"):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "keyword_replies": [{"keyword": "", "reply": "回复"}],
                }
            )
        with self.assertRaisesRegex(ValueError, "匹配方式只能是"):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "keyword_replies": [
                        {
                            "keyword": "帮助",
                            "reply": "回复",
                            "match_type": "regex",
                        }
                    ],
                }
            )

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
        result = plugin._uid_review_entries()
        self.assertEqual(result[0][:2], ("platform-1", "group-1"))
        self.assertTrue(result[0][2]["uid_check_enabled"])

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
                {
                    "uid_check_enabled": True,
                    "approve_keywords": [],
                    "reject_keywords": ["广告"],
                    "condition_logic": "all",
                    "fallback_action": "decline",
                },
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

    async def test_condition_logic_and_fallback(self):
        plugin, _ = self.plugin()
        requests = [
            {
                "member_openid": "member-1",
                "join_request_id": "request-1",
                "apply_source": "self_apply",
                "verify_info": {"verify_message": "普通申请"},
            },
            {
                "member_openid": "member-2",
                "join_request_id": "request-2",
                "apply_source": "self_apply",
                "verify_info": {"verify_message": "老用户 UID:188144093"},
            },
        ]
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={"list": requests, "next_cursor": ""}
            ),
            approve_join_request=AsyncMock(),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(module, "bilibili_uid_exists", AsyncMock(return_value=True)),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            await plugin._poll_uid_group(
                object(),
                "platform-1",
                "group-1",
                {
                    "uid_check_enabled": True,
                    "approve_keywords": ["老用户"],
                    "reject_keywords": [],
                    "condition_logic": "all",
                    "fallback_action": "pending",
                },
            )

        calls = api.approve_join_request.await_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["op"], "approve")
        self.assertEqual(calls[0].args[1], "member-2")

    async def test_global_reject_precedes_uid_direct_approve(self):
        plugin, _ = self.plugin()
        plugin.config["global_reject_keywords"] = "全局封禁"
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            {
                "uid_review_enabled": True,
                "uid_check_enabled": True,
                "uid_exists_auto_approve": True,
                "approve_keywords": "老用户",
                "reject_keywords": "",
                "condition_logic": "all",
                "fallback_action": "pending",
            }
        )
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={
                    "list": [
                        {
                            "member_openid": "member-1",
                            "join_request_id": "request-1",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "全局封禁 UID:188144093"},
                        },
                        {
                            "member_openid": "member-2",
                            "join_request_id": "request-2",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "UID:188144093"},
                        },
                    ],
                    "next_cursor": "",
                }
            ),
            approve_join_request=AsyncMock(),
        )
        lookup = AsyncMock(return_value=True)
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(module, "bilibili_uid_exists", lookup),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            await plugin._poll_uid_group(
                object(),
                "platform-1",
                "group-1",
                plugin._condition_settings(entry),
            )

        lookup.assert_awaited_once_with("188144093")
        calls = api.approve_join_request.await_args_list
        self.assertEqual([call.kwargs["op"] for call in calls], ["decline", "approve"])
        self.assertEqual(
            calls[0].kwargs["reject_reason"],
            "验证消息包含全局拒绝关键词",
        )

    async def test_uid_direct_approve_requires_uid_check(self):
        plugin, _ = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            {
                "uid_check_enabled": False,
                "uid_exists_auto_approve": True,
            }
        )

        settings = plugin._condition_settings(entry)

        self.assertFalse(settings["uid_exists_auto_approve"])

    async def test_uid_binding_is_unique_and_fallback_marks_suspicious(self):
        plugin, client = self.plugin()
        plugin._uid_bindings["188144093"] = {"identity": "union:other"}
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={
                    "list": [
                        {
                            "member_openid": "member-1",
                            "union_openid": "union-1",
                            "join_request_id": "request-1",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "UID:188144093"},
                        },
                        {
                            "member_openid": "member-2",
                            "union_openid": "union-2",
                            "join_request_id": "request-2",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "普通申请"},
                        },
                    ],
                    "next_cursor": "",
                }
            ),
            approve_join_request=AsyncMock(),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(module.asyncio, "sleep", AsyncMock()),
            patch.object(plugin, "_send_verification_challenge", AsyncMock()) as send,
        ):
            await plugin._poll_uid_group(
                client,
                "platform-1",
                "group-1",
                {
                    "uid_check_enabled": True,
                    "uid_exists_auto_approve": True,
                    "approve_keywords": [],
                    "reject_keywords": [],
                    "condition_logic": "all",
                    "fallback_action": "approve",
                    "fallback_human_verify_enabled": True,
                },
            )

        calls = api.approve_join_request.await_args_list
        self.assertEqual([call.kwargs["op"] for call in calls], ["decline", "approve"])
        self.assertEqual(
            calls[0].kwargs["reject_reason"], "该 B 站 UID 已绑定其他 QQ 用户"
        )
        self.assertIn("group-1:member-2", plugin._suspicious_members)
        send.assert_awaited_once_with(client, "group-1", "member-2")

    async def test_suspicious_message_is_recalled_and_verified_by_owner(self):
        plugin, client = self.plugin()
        plugin._suspicious_members["group-1:admin-1"] = {"reason": "test"}
        event = FakeEvent(client, "hello")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        message = client.api.messages[-1]
        self.assertEqual(message["msg_type"], 2)
        buttons = message["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertTrue(all(button["render_data"]["style"] == 1 for button in buttons))
        self.assertTrue(
            all(
                button["action"]["permission"]
                == {"type": 0, "specify_user_ids": ["admin-1"]}
                for button in buttons
            )
        )
        token = buttons[0]["action"]["data"].split(":")[1]
        answer = plugin._verification_tokens[token][3]
        interaction = SimpleNamespace(
            id="verify-1",
            type=11,
            chat_type=1,
            group_openid="group-1",
            group_member_openid="admin-1",
            data=SimpleNamespace(
                resolved=SimpleNamespace(button_data=f"qqgv:{token}:{answer}")
            ),
        )
        self.assertTrue(await plugin._handle_interaction(client, interaction))
        self.assertNotIn("group-1:admin-1", plugin._suspicious_members)
        self.assertEqual(client.api.acks[-1], ("verify-1", 0))

    async def test_bot_message_is_not_moderated(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "禁止词")
        event.message_obj.raw_message.raw_data = {
            "author": {"member_openid": "admin-1", "bot": True}
        }
        plugin.config["auto_review_groups"][0].update(
            {"moderation_enabled": True, "message_reject_keywords": "禁止词"}
        )
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertFalse(event.stopped)
        api.recall_group_message.assert_not_awaited()

    async def test_group_keyword_reply_precedes_global_and_stops_llm(self):
        plugin, client = self.plugin()
        plugin.config["global_keyword_replies"] = [
            {
                "keyword": "帮助",
                "reply": "全局帮助",
                "group_openids": "group-1",
            }
        ]
        plugin.config["auto_review_groups"][0]["keyword_replies"] = [
            {"keyword": "帮助", "reply": "本群帮助"}
        ]
        event = FakeEvent(client, "需要帮助")

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        self.assertEqual(client.api.messages[-1]["content"], "本群帮助")
        self.assertEqual(client.api.messages[-1]["msg_id"], "message-1")

    async def test_duplicate_delivery_never_reaches_later_handlers(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "hello")
        event.message_obj.raw_message.msg_seq = 1
        key = ("platform-1", "group-1", "message-1", "1")
        plugin._moderation.remember(key, False)

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)

    async def test_bilibili_dynamic_seed_and_delivery_retry(self):
        plugin, _ = self.plugin()
        plugin.config["bilibili_cookie"] = "cookie"
        subscriptions = {
            "188144093": [
                {
                    "group_openid": "group-1",
                    "platform_id": "platform-1",
                    "dynamic": True,
                    "live": False,
                }
            ]
        }
        item = {
            "id": "dynamic-1",
            "uid": "188144093",
            "author": "UP",
            "pub_ts": 100,
            "title": "新动态",
            "text": "正文",
            "url": "https://www.bilibili.com/opus/dynamic-1",
        }
        with (
            patch.object(module, "fetch_wbi_keys", return_value=("a", "b")),
            patch.object(module, "fetch_space_dynamics", return_value={}),
            patch.object(module, "parse_dynamic_items", return_value=[]),
        ):
            self.assertTrue(await plugin._poll_bilibili_dynamics(subscriptions))
        self.assertEqual(
            plugin._bilibili_state["dynamic"]["188144093"],
            {"seen": [], "max_pub_ts": 0},
        )

        with (
            patch.object(module, "fetch_wbi_keys", return_value=("a", "b")),
            patch.object(module, "fetch_space_dynamics", return_value={}),
            patch.object(module, "parse_dynamic_items", return_value=[item]),
            patch.object(
                plugin, "_push_bilibili_message", AsyncMock(return_value=False)
            ),
        ):
            self.assertFalse(await plugin._poll_bilibili_dynamics(subscriptions))
        self.assertEqual(plugin._bilibili_state["dynamic"]["188144093"]["seen"], [])

        with (
            patch.object(module, "fetch_wbi_keys", return_value=("a", "b")),
            patch.object(module, "fetch_space_dynamics", return_value={}),
            patch.object(module, "parse_dynamic_items", return_value=[item]),
            patch.object(
                plugin, "_push_bilibili_message", AsyncMock(return_value=True)
            ),
        ):
            self.assertTrue(await plugin._poll_bilibili_dynamics(subscriptions))
        self.assertEqual(
            plugin._bilibili_state["dynamic"]["188144093"]["seen"],
            ["dynamic-1"],
        )

    async def test_bilibili_failure_does_not_skip_later_hard_reject(self):
        plugin, _ = self.plugin()
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={
                    "list": [
                        {
                            "member_openid": "member-1",
                            "join_request_id": "request-1",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "UID:188144093"},
                        },
                        {
                            "member_openid": "member-2",
                            "join_request_id": "request-2",
                            "apply_source": "self_apply",
                            "verify_info": {"verify_message": "广告"},
                        },
                    ],
                    "next_cursor": "",
                }
            ),
            approve_join_request=AsyncMock(),
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(
                module,
                "bilibili_uid_exists",
                AsyncMock(side_effect=module.BilibiliLookupError("限流")),
            ),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            await plugin._poll_uid_group(
                object(),
                "platform-1",
                "group-1",
                {
                    "uid_check_enabled": True,
                    "approve_keywords": [],
                    "reject_keywords": ["广告"],
                    "condition_logic": "all",
                    "fallback_action": "decline",
                },
            )

        api.approve_join_request.assert_awaited_once()
        self.assertEqual(
            api.approve_join_request.await_args.args[1],
            "member-2",
        )
        self.assertEqual(
            api.approve_join_request.await_args.kwargs["reject_reason"],
            "验证消息包含拒绝关键词",
        )

    async def test_compact_mute_uses_seconds_and_custom_mention_reply(self):
        plugin, client = self.plugin()
        plugin.config["mute_success_message"] = "已禁言 {duration} 秒：{at_user}"
        event = FakeEvent(client, "/禁言<@member-1> 45")
        event.message_obj.raw_message.mentions = [
            SimpleNamespace(member_openid="member-1", is_you=False)
        ]
        api = SimpleNamespace(
            get_mute_state=AsyncMock(return_value={"members": []}),
            set_member_mutes=AsyncMock(),
        )
        plugin._api = lambda _event: api

        results = [result async for result in plugin.mute_member_compact(event)]

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        mute = api.set_member_mutes.await_args.args[1][0]
        self.assertEqual(mute["op"], "add")
        self.assertEqual(mute["member_openid"], "member-1")
        self.assertEqual(
            client.api.messages[-1]["markdown"]["content"],
            '已禁言 45 秒：<qqbot-at-user id="member-1" />',
        )
        self.assertEqual(client.api.messages[-1]["msg_type"], 2)

    async def test_standard_mute_stops_after_direct_mention_reply(self):
        plugin, client = self.plugin()
        plugin.config["mute_reply_at_member"] = True
        event = FakeEvent(client)
        api = SimpleNamespace(
            get_mute_state=AsyncMock(return_value={"members": []}),
            set_member_mutes=AsyncMock(),
        )
        plugin._api = lambda _event: api

        results = [
            result async for result in plugin.mute_member(event, "member-1", "45")
        ]

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertTrue(
            client.api.messages[-1]["markdown"]["content"].startswith(
                '<qqbot-at-user id="member-1" /> 已设置禁言，至 '
            )
        )

    async def test_mute_template_keeps_at_tag_when_text_is_long(self):
        plugin, client = self.plugin()
        plugin.config["mute_success_message"] = "x" * 995 + " {at_user}"
        event = FakeEvent(client)

        result = await plugin._send_mute_success(
            event,
            "member-1",
            "45",
            "2026-08-19T00:00:00+08:00",
        )

        self.assertIsNone(result)
        content = client.api.messages[-1]["markdown"]["content"]
        self.assertLessEqual(len(content), 1000)
        self.assertTrue(content.endswith('<qqbot-at-user id="member-1" />'))

        plugin.config["mute_success_message"] = "{at_user}" * 40
        await plugin._send_mute_success(event, "member-1", "45", "ignored")
        content = client.api.messages[-1]["markdown"]["content"]
        mention = '<qqbot-at-user id="member-1" />'
        self.assertEqual(content, mention * (1000 // len(mention)))

    async def test_web_batch_save_and_sync(self):
        plugin, _client = self.plugin()
        plugin.config["auto_review_groups"] = [
            {
                "group_openid": "group-1",
                "group_name": "一群",
                "platform_id": "platform-1",
            },
            {
                "group_openid": "group-2",
                "group_name": "二群",
                "platform_id": "platform-1",
            },
        ]
        web = module.GroupAdminWeb(plugin, plugin.context)
        payloads = await web._validated_batch_save(
            {
                "group_openids": ["group-1", "group-2"],
                "changes": {
                    "mode": "conditional",
                    "uid_exists_auto_approve": True,
                    "reject_keywords": "广告,推广",
                },
            }
        )
        saves_before = plugin.config.save_count

        updated = await plugin.web_batch_save(payloads)
        groups = await plugin.web_groups()

        self.assertEqual(plugin.config.save_count, saves_before + 1)
        self.assertEqual(updated, ["group-1", "group-2"])
        self.assertEqual([group["mode"] for group in groups], ["conditional"] * 2)
        for entry in plugin.config["auto_review_groups"]:
            self.assertTrue(entry["uid_check_enabled"])
            self.assertTrue(entry["uid_exists_auto_approve"])
            self.assertEqual(entry["reject_keywords"], "广告\n推广")

        plugin.web_sync_group = AsyncMock(
            side_effect=[groups[0], AssertionError("unexpected"), groups[1]]
        )
        results = await plugin.web_batch_sync(["group-1", "group-2", "group-3"])
        self.assertTrue(results[0]["ok"])
        self.assertNotIn("group", results[0])
        self.assertFalse(results[1]["ok"])
        self.assertEqual(results[1]["error"], "服务器处理失败，请查看 AstrBot 日志")
        self.assertTrue(results[2]["ok"])

        web_module = sys.modules[module.GroupAdminWeb.__module__]
        with (
            patch.object(web_module, "BATCH_TEXT_BUDGET", 1),
            self.assertRaisesRegex(ValueError, "批量配置内容过大"),
        ):
            await web._validated_batch_save(
                {
                    "group_openids": ["group-1"],
                    "changes": {"mode": "conditional"},
                }
            )

        with self.assertRaisesRegex(ValueError, "不支持批量修改字段"):
            await web._validated_batch_save(
                {
                    "group_openids": ["group-1"],
                    "changes": {"platform_id": "other"},
                }
            )

    def test_compact_commands_require_wake_prefix(self):
        _plugin, client = self.plugin()
        event = FakeEvent(client, "禁言<@member-1> 45")
        event.is_at_or_wake_command = False

        command_filter = module.WakeCommandFilter(False)

        self.assertFalse(command_filter.filter(event, TestConfig()))
        event.is_at_or_wake_command = True
        self.assertTrue(command_filter.filter(event, TestConfig()))

    def test_legacy_config_is_migrated_once(self):
        config = TestConfig(
            mute_success_message="已禁言 {duration} 秒",
            mute_reply_at_member=True,
            auto_review_groups=[
                {
                    "group_openid": "group-1",
                    "uid_review_enabled": True,
                    "uid_reject_keywords": "广告",
                }
            ],
        )
        context = SimpleNamespace(get_platform_inst=lambda _platform_id: None)

        module.QQGroupAdmin(context, config)

        entry = config["auto_review_groups"][0]
        self.assertEqual(entry["__template_key"], "qq_group")
        self.assertEqual(entry["reject_keywords"], "广告")
        self.assertFalse(entry["uid_exists_auto_approve"])
        self.assertEqual(entry["fallback_action"], "decline")
        self.assertNotIn("uid_reject_keywords", entry)
        self.assertEqual(
            config["mute_success_message"],
            "{at_user} 已禁言 {duration} 秒",
        )
        self.assertFalse(config["mute_reply_at_member"])
        self.assertEqual(config.save_count, 1)


if __name__ == "__main__":
    unittest.main()
