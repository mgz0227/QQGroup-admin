import asyncio
import json
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
    def test_permission_error_40011030_has_actionable_hint(self):
        error = module.QQAPIError(err_code=40011030, trace_id="trace-1")
        self.assertIn("入群申请接口判定机器人不是群管理员", str(error))
        self.assertIn("trace-1", str(error))

    def plugin(self):
        client = FakeClient()
        platform = SimpleNamespace(get_client=lambda: client)
        context = SimpleNamespace(get_platform_inst=lambda _platform_id: platform)
        config = TestConfig(
            auto_review_groups=[
                {
                    "group_openid": "group-1",
                    "platform_id": "platform-1",
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
                "sync",
                "conditions",
                "moderation",
                "keywords",
                "bilibili",
                "off",
            ],
        )
        self.assertEqual(
            [button["render_data"]["label"] for button in buttons],
            [
                "绑定此群",
                "应用配置",
                "审核条件",
                "消息审查",
                "关键词回复",
                "B站推送",
                "关闭自动审核",
            ],
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
        conditions_data = buttons[2]["action"]["data"]
        interaction = SimpleNamespace(
            id="settings-wrong-group",
            type=11,
            chat_type=1,
            group_openid="other-group",
            data=SimpleNamespace(resolved=SimpleNamespace(button_data=conditions_data)),
        )
        with patch.object(module, "QQGroupAPI", return_value=api):
            self.assertTrue(await plugin._handle_interaction(client, interaction))
        self.assertEqual(plugin.config["auto_review_groups"], [])
        self.assertEqual(client.api.acks[-1], ("settings-wrong-group", 4))

        plugin.config["settings_panel_auto_recall"] = False
        interaction.id = "settings-conditions"
        interaction.group_openid = "group-1"
        with patch.object(module, "QQGroupAPI", return_value=api):
            self.assertTrue(await plugin._handle_interaction(client, interaction))
        condition_buttons = [
            button
            for row in client.api.messages[-1]["keyboard"]["content"]["rows"]
            for button in row["buttons"]
        ]
        uid_data = next(
            button["action"]["data"]
            for button in condition_buttons
            if button["action"]["data"].endswith(":conditional")
        )
        interaction.id = "settings-uid"
        interaction.data.resolved.button_data = uid_data
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

    async def test_settings_submenus_are_compact_and_cover_keyword_and_bilibili(self):
        plugin, client = self.plugin()
        plugin.config.update(
            settings_panel_auto_recall=False,
            keyword_reply_cooldown_seconds=30,
            keyword_reply_recall_seconds=20,
            global_keyword_replies=[
                {"name": "全局帮助", "keywords": ["帮助"], "enabled": True}
            ],
        )
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            keyword_replies=[
                {
                    "name": "群公告",
                    "keywords": ["公告", "规则"],
                    "logic": "all",
                    "enabled": True,
                }
            ],
            bilibili_uids="188144093",
        )
        token = plugin._settings_token("group-1", "platform-1", "测试群")

        await plugin._send_condition_settings(client, "group-1", token, "测试群")
        await plugin._send_moderation_settings(client, "group-1", token, "测试群")
        await plugin._send_keyword_settings(client, "group-1", token, "测试群")
        await plugin._send_bilibili_settings(client, "group-1", token, "测试群")

        for message in client.api.messages:
            rows = message["keyboard"]["content"]["rows"]
            self.assertLessEqual(len(rows), 5)
            self.assertTrue(all(len(row["buttons"]) <= 3 for row in rows))
        keyword_text = client.api.messages[2]["markdown"]["content"]
        self.assertIn("群公告", keyword_text)
        self.assertIn("每群冷却：30 秒", keyword_text)
        self.assertIn("回复撤回：20 秒", keyword_text)
        bili_actions = {
            button["action"]["data"].rsplit(":", 1)[1]
            for row in client.api.messages[3]["keyboard"]["content"]["rows"]
            for button in row["buttons"]
        }
        self.assertEqual(
            bili_actions,
            {
                "bili_dynamic_on",
                "bili_dynamic_off",
                "bili_live_on",
                "bili_live_off",
                "home",
            },
        )
        await plugin._apply_settings_button(
            client,
            "group-1",
            "platform-1",
            "bili_dynamic_on",
            "测试群",
        )
        self.assertTrue(entry["bilibili_dynamic_enabled"])

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

    async def test_settings_buttons_update_scoped_media_policy(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        await plugin._apply_settings_button(
            client, "group-1", "platform-1", "image_on", "测试群"
        )
        await plugin._apply_settings_button(
            client, "group-1", "platform-1", "repeat_on", "测试群"
        )
        self.assertTrue(plugin.config["global_image_spam_enabled"])
        self.assertTrue(plugin.config["global_repeat_review_enabled"])
        self.assertFalse(entry.get("image_spam_enabled", False))
        self.assertFalse(entry.get("repeat_review_enabled", False))

        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "p1",
                "name": "媒体策略",
                "enabled": True,
                "group_openids": ["group-1"],
            }
        ]
        await plugin._apply_settings_button(
            client, "group-1", "platform-1", "image_off", "测试群"
        )
        self.assertFalse(plugin.config["global_policy_profiles"][0]["global_image_spam_enabled"])

    async def test_settings_button_creates_scoped_policy_for_uncovered_group(self):
        plugin, client = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "other",
                "name": "其他群策略",
                "enabled": True,
                "group_openids": ["group-2"],
            }
        ]
        await plugin._apply_settings_button(
            client, "group-1", "platform-1", "repeat_on", "测试群"
        )
        created = plugin.config["global_policy_profiles"][-1]
        self.assertEqual(created["group_openids"], ["group-1"])
        self.assertTrue(created["global_repeat_review_enabled"])
        self.assertEqual(plugin.config["global_policy_profiles"][0]["group_openids"], ["group-2"])

    async def test_settings_button_splits_shared_policy_before_changing_one_group(self):
        plugin, _client = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "shared",
                "name": "共享媒体策略",
                "enabled": True,
                "group_openids": ["group-1", "group-2"],
                "global_repeat_review_enabled": False,
            }
        ]
        plugin._set_global_policy_value_for_group(
            "group-1", "global_repeat_review_enabled", True
        )
        profiles = plugin.config["global_policy_profiles"]
        self.assertEqual(profiles[0]["group_openids"], ["group-1"])
        self.assertTrue(profiles[0]["global_repeat_review_enabled"])
        self.assertEqual(profiles[1]["group_openids"], ["group-2"])
        self.assertFalse(profiles[1]["global_repeat_review_enabled"])

    def test_global_policy_scope_warnings_report_ordered_overlap(self):
        plugin, _client = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "specific",
                "name": "专用群策略",
                "enabled": True,
                "group_openids": ["group-1"],
            },
            {
                "profile_id": "fallback",
                "name": "全部群兜底",
                "enabled": True,
                "group_openids": [],
            },
        ]
        warnings = plugin._global_policy_scope_warnings({"group-1", "group-2"})
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["first_name"], "专用群策略")
        self.assertEqual(warnings[0]["second_name"], "全部群兜底")
        self.assertEqual(warnings[0]["group_openids"], ["group-1"])

    async def test_uncovered_group_does_not_fall_back_to_legacy_media_policy(self):
        plugin, _client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(image_spam_enabled=True, repeat_review_enabled=True)
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "other",
                "name": "其他群策略",
                "enabled": True,
                "group_openids": ["group-2"],
            }
        ]
        settings = plugin._moderation_settings(entry)
        self.assertFalse(settings["image_enabled"])
        self.assertFalse(settings["repeat_enabled"])

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

    async def test_upload_group_file_uses_public_url_and_reports_type(self):
        plugin, client = self.plugin()
        event = FakeEvent(client)
        api = SimpleNamespace(
            upload_group_file=AsyncMock(return_value={"id": "message-2"})
        )
        plugin._api = lambda _event: api

        results = [
            result
            async for result in plugin.upload_group_file(
                event,
                "https://cdn.example.test/demo.mp4?sig=abc",
                "demo.mp4",
            )
        ]

        self.assertEqual(results, ["群视频已发送。"])
        api.upload_group_file.assert_awaited_once_with(
            "group-1",
            "https://cdn.example.test/demo.mp4?sig=abc",
            file_name="demo.mp4",
        )

    async def test_sync_command_panel_creates_then_updates_managed_panel(self):
        plugin, client = self.plugin()
        event = FakeEvent(client)
        api = SimpleNamespace(
            list_group_panels=AsyncMock(
                side_effect=[
                    {"records": []},
                    {
                        "records": [
                            {
                                "panel_id": "panel-1",
                                "target_type": "all",
                                "panel": {
                                    "remark": module.COMMAND_PANEL_REMARK,
                                },
                            }
                        ]
                    },
                ]
            ),
            create_group_panel=AsyncMock(return_value={"panel_id": "panel-1"}),
            update_panel=AsyncMock(),
        )
        plugin._api = lambda _event: api

        created = [
            result async for result in plugin.sync_command_panel(event)
        ]
        updated = [
            result async for result in plugin.sync_command_panel(event)
        ]

        api.create_group_panel.assert_awaited_once_with(module.COMMAND_PANEL)
        api.update_panel.assert_awaited_once_with(
            "panel-1",
            module.COMMAND_PANEL,
        )
        self.assertIn("已创建", created[0])
        self.assertIn("已更新", updated[0])

    async def test_sync_command_panel_finds_managed_panel_on_later_page(self):
        plugin, client = self.plugin()
        event = FakeEvent(client)
        api = SimpleNamespace(
            list_group_panels=AsyncMock(
                side_effect=[
                    {"records": [], "next_cursor": "page-2", "is_end": False},
                    {
                        "records": [
                            {
                                "panel_id": "panel-2",
                                "target_type": "all",
                                "panel": {"remark": module.COMMAND_PANEL_REMARK},
                            }
                        ],
                        "next_cursor": "",
                        "is_end": True,
                    },
                ]
            ),
            create_group_panel=AsyncMock(),
            update_panel=AsyncMock(),
        )
        plugin._api = lambda _event: api

        results = [result async for result in plugin.sync_command_panel(event)]

        self.assertIn("已更新", results[0])
        api.create_group_panel.assert_not_awaited()
        api.update_panel.assert_awaited_once_with("panel-2", module.COMMAND_PANEL)
        self.assertEqual(
            [call.kwargs for call in api.list_group_panels.await_args_list],
            [{}, {"cursor": "page-2"}],
        )

    async def test_sync_command_panel_rejects_repeated_cursor(self):
        plugin, client = self.plugin()
        api = SimpleNamespace(
            list_group_panels=AsyncMock(
                side_effect=[
                    {"records": [], "next_cursor": "same", "is_end": False},
                    {"records": [], "next_cursor": "same", "is_end": False},
                ]
            ),
            create_group_panel=AsyncMock(),
            update_panel=AsyncMock(),
        )
        plugin._api = lambda _event: api

        results = [
            result async for result in plugin.sync_command_panel(FakeEvent(client))
        ]

        self.assertIn("游标重复", results[0])
        api.create_group_panel.assert_not_awaited()

    async def test_sync_command_panel_serializes_concurrent_creation(self):
        plugin, client = self.plugin()
        records = []
        create_count = 0

        async def list_group_panels():
            await asyncio.sleep(0)
            return {"records": list(records)}

        async def create_group_panel(_panel):
            nonlocal create_count
            await asyncio.sleep(0)
            create_count += 1
            records.append(
                {
                    "panel_id": "panel-1",
                    "target_type": "all",
                    "panel": {"remark": module.COMMAND_PANEL_REMARK},
                }
            )
            return {"panel_id": "panel-1"}

        api = SimpleNamespace(
            list_group_panels=list_group_panels,
            create_group_panel=create_group_panel,
            update_panel=AsyncMock(),
        )
        plugin._api = lambda _event: api

        async def sync():
            return [
                result
                async for result in plugin.sync_command_panel(
                    FakeEvent(client)
                )
            ]

        await asyncio.gather(sync(), sync())

        self.assertEqual(create_count, 1)
        api.update_panel.assert_awaited_once_with(
            "panel-1",
            module.COMMAND_PANEL,
        )

    async def test_keyword_reply_cooldown_and_recall(self):
        plugin, client = self.plugin()
        plugin.config.update(
            keyword_reply_cooldown_seconds=60,
            keyword_reply_recall_seconds=20,
        )
        entry = plugin.config["auto_review_groups"][0]
        entry["keyword_replies"] = [
            {
                "name": "群帮助",
                "keywords": "帮助\n指南",
                "condition_logic": "any",
                "match_type": "contains",
                "reply": "请查看群公告",
                "enabled": True,
            }
        ]
        event = FakeEvent(client, "需要帮助")

        with patch.object(plugin, "_schedule_recall") as schedule_recall:
            self.assertTrue(
                await plugin._reply_to_keyword(
                    event, "group-1", "message-1", "需要帮助", entry
                )
            )
            self.assertFalse(
                await plugin._reply_to_keyword(
                    event, "group-1", "message-2", "需要帮助", entry
                )
            )

        self.assertEqual(len(client.api.messages), 1)
        schedule_recall.assert_called_once_with(
            client, "group-1", "sent-1", 20, "keyword-reply"
        )
        self.assertTrue(event.stopped)

    async def test_keyword_reply_cooldown_is_reserved_before_send(self):
        plugin, client = self.plugin()
        plugin.config["keyword_reply_cooldown_seconds"] = 60
        entry = plugin.config["auto_review_groups"][0]
        entry["keyword_replies"] = [{"keyword": "帮助", "reply": "群帮助"}]
        event = FakeEvent(client, "帮助")
        started = module.asyncio.Event()
        release = module.asyncio.Event()
        calls = 0

        async def delayed_send(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return SimpleNamespace(id=f"sent-{calls}")

        with patch.object(plugin, "_send_group_text", side_effect=delayed_send):
            first = module.asyncio.create_task(
                plugin._reply_to_keyword(event, "group-1", "message-1", "帮助", entry)
            )
            await started.wait()
            second = await plugin._reply_to_keyword(
                event, "group-1", "message-2", "帮助", entry
            )
            release.set()
            self.assertTrue(await first)

        self.assertFalse(second)
        self.assertEqual(calls, 1)

    async def test_unbound_group_does_not_use_global_keyword_reply(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.pop("platform_id")
        plugin.config["global_keyword_replies"] = [
            {"keyword": "帮助", "reply": "全局帮助"}
        ]

        self.assertFalse(
            await plugin._reply_to_keyword(
                FakeEvent(client, "帮助"), "group-1", "message-1", "帮助", entry
            )
        )
        self.assertEqual(client.api.messages, [])

    async def test_runtime_and_bilibili_qr_login_never_echo_cookie(self):
        plugin, _ = self.plugin()
        runtime = {
            "uid_review_interval_seconds": 45,
            "mute_success_message": "已禁言 {at_user}",
            "settings_panel_auto_recall": True,
            "settings_command_enabled": True,
            "global_reject_keywords": "广告",
            "global_message_reject_keywords": "刷屏",
            "bilibili_live_interval_seconds": 60,
            "bilibili_dynamic_interval_seconds": 180,
        }
        saved = await plugin.web_save_runtime_settings(runtime)
        self.assertEqual(saved["uid_review_interval_seconds"], 45)
        self.assertFalse(saved["bilibili_logged_in"])

        login = SimpleNamespace(
            qrcode_key="qrcode-key",
            url="https://passport.bilibili.com/qr",
            expires_at=module.time.monotonic() + 180,
        )
        with (
            patch.object(module, "start_qr_login", return_value=login),
            patch.object(
                plugin, "_qr_data_url", return_value="data:image/svg+xml;base64,AA=="
            ),
        ):
            started = await plugin.web_bilibili_login_start()
        self.assertEqual(started["qrcode_key"], "qrcode-key")
        self.assertNotIn("cookie", repr(started).lower())

        with patch.object(
            module,
            "poll_qr_login",
            return_value=("confirmed", "SESSDATA=secret; bili_jct=csrf"),
        ):
            confirmed = await plugin.web_bilibili_login_poll("qrcode-key")
        self.assertTrue(confirmed["bilibili_logged_in"])
        self.assertEqual(
            plugin.config["bilibili_cookie"], "SESSDATA=secret; bili_jct=csrf"
        )
        self.assertNotIn("secret", repr(confirmed))

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
                "member_blacklist": "member-1, union-1",
                "member_whitelist": "188144093",
                "blacklist_reply": "黑名单消息已撤回",
                "blacklist_at_member": False,
                "keyword_replies": [
                    {
                        "name": "群帮助",
                        "keywords": "帮助, 指南",
                        "condition_logic": "any",
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
        self.assertEqual(payload["member_blacklist"], "member-1\nunion-1")
        self.assertEqual(payload["member_whitelist"], "188144093")
        self.assertFalse(payload["blacklist_at_member"])
        self.assertEqual(payload["ai_review_provider_id"], "")
        self.assertEqual(payload["ai_review_fallback_provider_id"], "")
        self.assertEqual(payload["image_spam_group_min_members"], 2)
        self.assertEqual(
            payload["keyword_replies"],
            [
                {
                    "__template_key": "keyword_reply",
                    "name": "群帮助",
                    "keywords": "帮助\n指南",
                    "condition_logic": "any",
                    "reply": "请查看群公告",
                    "match_type": "exact",
                    "enabled": True,
                }
            ],
        )
        self.assertFalse(payload["_legacy_media_fields_present"])
        legacy_entry = {
            "image_spam_enabled": True,
            "image_spam_count": 9,
            "repeat_review_enabled": True,
            "repeat_count": 8,
        }
        module.QQGroupAdmin._update_web_group(legacy_entry, payload)
        self.assertTrue(legacy_entry["image_spam_enabled"])
        self.assertEqual(legacy_entry["image_spam_count"], 9)
        self.assertTrue(legacy_entry["repeat_review_enabled"])
        self.assertEqual(legacy_entry["repeat_count"], 8)
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
                    "keyword_replies": [
                        {"name": "空规则", "keywords": "", "reply": "回复"}
                    ],
                }
            )
        with self.assertRaisesRegex(ValueError, "匹配方式只能是"):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "keyword_replies": [
                        {
                            "name": "错误规则",
                            "keywords": "帮助",
                            "reply": "回复",
                            "match_type": "regex",
                        }
                    ],
                }
            )
        with self.assertRaisesRegex(ValueError, "完全匹配不能"):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "keyword_replies": [
                        {
                            "name": "无效组合",
                            "keywords": "帮助\n指南",
                            "condition_logic": "all",
                            "reply": "回复",
                            "match_type": "exact",
                        }
                    ],
                }
            )
        with self.assertRaisesRegex(ValueError, "主模型和回退模型不能相同"):
            module.GroupAdminWeb._validated_save(
                {
                    **payload,
                    "ai_review_provider_id": "provider-1",
                    "ai_review_fallback_provider_id": "provider-1",
                }
            )

    def test_web_global_keyword_replies_use_bound_group_scope(self):
        rules = module.GroupAdminWeb._global_keyword_replies(
            [
                {
                    "name": "公告与帮助",
                    "keywords": "公告, 帮助",
                    "condition_logic": "all",
                    "reply": "请查看群公告",
                    "match_type": "contains",
                    "enabled": True,
                    "group_openids": ["group-1"],
                }
            ],
            {"group-1"},
        )
        self.assertEqual(rules[0]["group_openids"], "group-1")
        self.assertEqual(rules[0]["keywords"], "公告\n帮助")
        self.assertEqual(rules[0]["condition_logic"], "all")
        with self.assertRaisesRegex(ValueError, "包含未绑定群"):
            module.GroupAdminWeb._global_keyword_replies(
                [
                    {
                        "name": "公告",
                        "keywords": "公告",
                        "reply": "回复",
                        "group_openids": ["unknown-group"],
                    }
                ],
                {"group-1"},
            )

        runtime = module.GroupAdminWeb._runtime_settings(
            {
                "uid_review_interval_seconds": 45,
                "mute_success_message": "已禁言 {at_user}",
                "settings_panel_auto_recall": True,
                "settings_command_enabled": False,
                "global_reject_keywords": "广告, 引流",
                "global_message_reject_keywords": "刷屏",
                "global_message_reject_reply": "文字已撤回",
                "global_message_reject_at_member": False,
                "global_member_blacklist": "member-1, union-1",
                "global_member_whitelist": "188144093",
                "global_blacklist_reply": "黑名单已撤回",
                "global_blacklist_at_member": True,
                "global_image_reject_reply": "图片已撤回",
                "global_image_reject_at_member": True,
                "global_ai_review_action": "record_only",
                "global_ai_reject_reply": "AI 已撤回",
                "global_ai_reject_at_member": False,
                "bilibili_live_interval_seconds": 60,
                "bilibili_dynamic_interval_seconds": 180,
            }
        )
        self.assertEqual(runtime["global_reject_keywords"], "广告\n引流")
        self.assertFalse(runtime["settings_command_enabled"])
        self.assertEqual(runtime["global_message_reject_reply"], "文字已撤回")
        self.assertFalse(runtime["global_message_reject_at_member"])
        self.assertEqual(runtime["global_member_blacklist"], "member-1\nunion-1")
        self.assertEqual(runtime["global_member_whitelist"], "188144093")
        self.assertEqual(runtime["global_blacklist_reply"], "黑名单已撤回")
        self.assertTrue(runtime["global_blacklist_at_member"])
        self.assertEqual(runtime["global_image_reject_reply"], "图片已撤回")
        self.assertTrue(runtime["global_image_reject_at_member"])
        self.assertEqual(runtime["global_ai_review_action"], "record_only")

    async def test_web_scope_save_keeps_existing_unbound_group(self):
        plugin, _client = self.plugin()
        plugin.config["welcome_rules"] = [
            {
                "name": "欢迎",
                "message": "欢迎 {username}",
                "group_openids": ["group-2"],
            }
        ]
        web = module.GroupAdminWeb(plugin, plugin.context)
        web_module = sys.modules[module.GroupAdminWeb.__module__]
        payload = {
            "rules": [
                {
                    "name": "欢迎",
                    "message": "欢迎 {username}",
                    "group_openids": ["group-2"],
                }
            ]
        }
        with patch.object(
            web_module.request, "json", AsyncMock(return_value=payload), create=True
        ):
            await web.page_welcome_rules_save()
        self.assertEqual(plugin.config["welcome_rules"][0]["group_openids"], ["group-2"])

        scope = await web._scope_group_openids(
            {"rules": [{"group_openids": ["group-2"]}]}
        )
        self.assertIn("group-2", scope)

    def test_runtime_validation_keeps_new_fields_partial_for_cached_pages(self):
        runtime = module.GroupAdminWeb._runtime_settings(
            {
                "uid_review_interval_seconds": 60,
                "mute_success_message": "ok",
                "settings_panel_auto_recall": True,
                "settings_command_enabled": True,
                "global_reject_keywords": "",
                "global_message_reject_keywords": "",
                "bilibili_live_interval_seconds": 60,
                "bilibili_dynamic_interval_seconds": 180,
            }
        )
        self.assertNotIn("global_message_reject_reply", runtime)
        self.assertNotIn("global_image_reject_reply", runtime)
        self.assertNotIn("global_ai_reject_reply", runtime)

    def test_global_policy_profiles_are_scoped_and_ordered(self):
        plugin, _ = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "general",
                "name": "普通群",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_message_reject_keywords": "广告",
                "keyword_reply_cooldown_seconds": 12,
            },
            {
                "profile_id": "fallback",
                "name": "兜底群",
                "enabled": True,
                "group_openids": [],
                "global_message_reject_keywords": "默认",
            },
        ]
        entry = plugin.config["auto_review_groups"][0]
        first = plugin._moderation_settings(entry)
        self.assertEqual(first["global_keywords"], ["广告"])
        self.assertEqual(first["keyword_reply_cooldown_seconds"], 12)
        entry["group_openid"] = "group-2"
        second = plugin._moderation_settings(entry)
        self.assertEqual(second["global_keywords"], ["默认"])

    def test_old_global_profile_inherits_top_level_ai_settings(self):
        plugin, _ = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary",
            global_ai_review_fallback_provider_ids=["fallback-1", "fallback-2"],
            global_ai_review_timeout_seconds=42,
        )
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "legacy-ai",
                "name": "旧策略",
                "enabled": True,
                "group_openids": ["group-1"],
            }
        ]
        entry = plugin.config["auto_review_groups"][0]
        settings = plugin._moderation_settings(entry)
        self.assertTrue(settings["ai_enabled"])
        self.assertEqual(settings["ai_provider_id"], "primary")
        self.assertEqual(
            settings["ai_fallback_provider_ids"], ["fallback-1", "fallback-2"]
        )
        self.assertEqual(settings["ai_timeout"], 42)
        displayed = plugin._global_policy_profiles_for_web()[0]
        self.assertTrue(displayed["global_ai_review_enabled"])
        self.assertEqual(displayed["global_ai_review_provider_id"], "primary")

    def test_global_policy_web_validation_rejects_duplicate_or_unknown_scope(self):
        profile = {
            "profile_id": "p1",
            "name": "测试策略",
            "enabled": True,
            "group_openids": ["group-1"],
            "global_ai_review_fallback_provider_ids": ["fallback-1", "fallback-2"],
        }
        profiles = module.GroupAdminWeb._global_policy_profiles(
            [profile], {"group-1"}
        )
        self.assertEqual(profiles[0]["group_openids"], ["group-1"])
        self.assertEqual(
            profiles[0]["global_ai_review_fallback_provider_ids"],
            ["fallback-1", "fallback-2"],
        )
        with self.assertRaisesRegex(ValueError, "未绑定群"):
            module.GroupAdminWeb._global_policy_profiles(
                [{**profile, "group_openids": ["unknown"]}], {"group-1"}
            )
        with self.assertRaisesRegex(ValueError, "策略 ID 重复"):
            module.GroupAdminWeb._global_policy_profiles(
                [profile, {**profile, "name": "第二套"}], {"group-1"}
            )

    def test_global_policy_web_validation_accepts_media_and_repeat_fields(self):
        profile = {
            "profile_id": "media-repeat",
            "name": "媒体复读",
            "enabled": True,
            "group_openids": ["group-1"],
            "global_image_spam_enabled": True,
            "global_image_spam_count": 8,
            "global_image_spam_window_seconds": 20,
            "global_image_spam_group_min_members": 3,
            "global_image_spam_recall_count": 6,
            "global_image_spam_reply": "图片已清理",
            "global_image_spam_at_member": False,
            "global_repeat_review_enabled": True,
            "global_repeat_count": 5,
            "global_repeat_window_seconds": 40,
            "global_repeat_mute_min_seconds": 10,
            "global_repeat_mute_max_seconds": 30,
            "global_repeat_reply": "复读处理",
            "global_repeat_at_member": False,
        }
        profiles = module.GroupAdminWeb._global_policy_profiles(
            [profile], {"group-1"}
        )
        self.assertTrue(profiles[0]["global_image_spam_enabled"])
        self.assertEqual(profiles[0]["global_image_spam_count"], 8)
        self.assertEqual(profiles[0]["global_repeat_mute_max_seconds"], 30)
        with self.assertRaisesRegex(ValueError, "最长禁言不能小于最短禁言"):
            module.GroupAdminWeb._global_policy_profiles(
                [
                    {
                        **profile,
                        "global_repeat_mute_min_seconds": 40,
                        "global_repeat_mute_max_seconds": 30,
                    }
                ],
                {"group-1"},
            )

    def test_global_policy_web_validation_accepts_rate_limit_fields(self):
        profiles = module.GroupAdminWeb._global_policy_profiles(
            [
                {
                    "profile_id": "rate",
                    "name": "频率限制",
                    "enabled": True,
                    "group_openids": ["group-1"],
                    "global_rate_limit_enabled": True,
                    "global_rate_limit_count": 3,
                    "global_rate_limit_window_seconds": 12,
                    "global_rate_limit_recall_count": 2,
                    "global_rate_limit_reply": "太快了 {at_user}",
                    "global_rate_limit_at_member": False,
                }
            ],
            {"group-1"},
        )
        profile = profiles[0]
        self.assertTrue(profile["global_rate_limit_enabled"])
        self.assertEqual(profile["global_rate_limit_count"], 3)
        self.assertEqual(profile["global_rate_limit_window_seconds"], 12)
        self.assertEqual(profile["global_rate_limit_recall_count"], 2)
        self.assertFalse(profile["global_rate_limit_at_member"])

    def test_global_policy_web_surfaces_legacy_media_values(self):
        plugin, _client = self.plugin()
        plugin.config["auto_review_groups"][0].update(
            image_spam_enabled=True,
            image_spam_count=9,
            repeat_review_enabled=True,
            repeat_mute_min_seconds=13,
            repeat_mute_max_seconds=27,
        )
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "legacy",
                "name": "旧媒体策略",
                "enabled": True,
                "group_openids": ["group-1"],
            }
        ]
        profile = plugin._global_policy_profiles_for_web()[0]
        self.assertTrue(profile["global_image_spam_enabled"])
        self.assertEqual(profile["global_image_spam_count"], 9)
        self.assertTrue(profile["global_repeat_review_enabled"])
        self.assertEqual(profile["global_repeat_mute_min_seconds"], 13)
        self.assertEqual(profile["global_repeat_mute_max_seconds"], 27)

    async def test_legacy_media_policy_round_trip_preserves_unchanged_values(self):
        plugin, _client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            image_spam_enabled=True,
            image_spam_count=9,
            repeat_review_enabled=True,
            repeat_mute_min_seconds=13,
            repeat_mute_max_seconds=27,
        )
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "legacy",
                "name": "旧媒体策略",
                "enabled": True,
                "group_openids": ["group-1"],
            }
        ]
        displayed = plugin._global_policy_profiles_for_web()
        validated = module.GroupAdminWeb._global_policy_profiles(
            displayed, {"group-1"}
        )
        await plugin.web_save_global_policies({"profiles": validated})
        saved = plugin.config["global_policy_profiles"][0]
        self.assertNotIn("global_image_spam_enabled", saved)
        self.assertNotIn("global_repeat_review_enabled", saved)
        settings = plugin._moderation_settings(entry)
        self.assertTrue(settings["image_enabled"])
        self.assertEqual(settings["image_count"], 9)
        self.assertTrue(settings["repeat_enabled"])
        self.assertEqual(settings["repeat_mute_max"], 27)

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

    async def test_member_lists_are_checked_before_suspicious_challenge(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            moderation_enabled=True,
            moderation_exempt_admins=False,
            member_whitelist="member-1",
            member_blacklist="member-2",
        )
        plugin._suspicious_members["group-1:member-1"] = {"reason": "stale"}
        plugin._suspicious_members["group-1:member-2"] = {"reason": "stale"}
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        trusted = FakeEvent(client, "普通消息")
        trusted.message_obj.raw_message.author.member_openid = "member-1"
        await plugin.audit_group_message(trusted)
        self.assertFalse(trusted.stopped)
        self.assertEqual(client.api.messages, [])
        api.recall_group_message.assert_not_awaited()

        blocked = FakeEvent(client, "普通消息")
        blocked.message_obj.raw_message.author.member_openid = "member-2"
        blocked.message_obj.message_id = "message-2"
        await plugin.audit_group_message(blocked)
        self.assertTrue(blocked.stopped)
        self.assertIn(
            "成员命中本群黑名单，消息已撤回。",
            client.api.messages[-1]["markdown"]["content"],
        )
        api.recall_group_message.assert_awaited_once_with("group-1", "message-2")

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

    async def test_global_keyword_uses_its_own_reply_without_mention(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_message_reject_keywords="禁止词",
            global_message_reject_reply="这条文字消息已撤回。",
            global_message_reject_at_member=False,
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        event = FakeEvent(client, "包含禁止词")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        self.assertEqual(client.api.messages[-1]["msg_type"], 0)
        self.assertEqual(client.api.messages[-1]["content"], "这条文字消息已撤回。")

    async def test_scoped_global_keyword_runs_when_group_audit_is_disabled(self):
        plugin, client = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "strict-text",
                "name": "严格文字策略",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_message_reject_keywords": "禁止词",
                "global_message_reject_reply": "全局策略已撤回。",
                "global_message_reject_at_member": False,
            }
        ]
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        event = FakeEvent(client, "包含禁止词")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        self.assertEqual(client.api.messages[-1]["content"], "全局策略已撤回。")

    async def test_scoped_global_image_and_repeat_settings_are_used(self):
        plugin, _client = self.plugin()
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "media-repeat",
                "name": "媒体与复读策略",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_image_spam_enabled": True,
                "global_image_spam_count": 7,
                "global_image_spam_window_seconds": 21,
                "global_image_spam_group_min_members": 3,
                "global_image_spam_recall_count": 4,
                "global_image_spam_reply": "图片过多",
                "global_image_spam_at_member": False,
                "global_repeat_review_enabled": True,
                "global_repeat_count": 6,
                "global_repeat_window_seconds": 42,
                "global_repeat_mute_min_seconds": 11,
                "global_repeat_mute_max_seconds": 22,
                "global_repeat_reply": "复读处理 {duration}",
                "global_repeat_at_member": False,
            }
        ]
        settings = plugin._moderation_settings(
            plugin.config["auto_review_groups"][0]
        )
        self.assertFalse(settings["enabled"])
        self.assertTrue(settings["image_enabled"])
        self.assertEqual(settings["image_count"], 7)
        self.assertEqual(settings["image_window"], 21)
        self.assertEqual(settings["image_group_min_members"], 3)
        self.assertEqual(settings["image_recall_count"], 4)
        self.assertTrue(settings["repeat_enabled"])
        self.assertEqual(settings["repeat_count"], 6)
        self.assertEqual(settings["repeat_window"], 42)
        self.assertEqual(settings["repeat_mute_min"], 11)
        self.assertEqual(settings["repeat_mute_max"], 22)
        self.assertEqual(settings["repeat_reply"], "复读处理 {duration}")
        self.assertFalse(settings["repeat_at"])

    async def test_global_image_spam_runs_when_local_audit_is_disabled(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "images",
                "name": "连续发图",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_image_spam_enabled": True,
                "global_image_spam_count": 2,
                "global_image_spam_window_seconds": 30,
                "global_image_spam_group_min_members": 2,
                "global_image_spam_recall_count": 5,
                "global_image_spam_reply": "图片过多",
                "global_image_spam_at_member": False,
            }
        ]
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api
        image_type = type("Image", (), {})

        for index, member in enumerate(("member-1", "member-2"), 1):
            event = FakeEvent(client, "")
            event.is_at_or_wake_command = False
            event.message_obj.message_id = f"image-{index}"
            event.message_obj.raw_message.author = SimpleNamespace(
                member_openid=member
            )
            image = image_type()
            image.url = f"https://example.test/{index}.png"
            event.message_obj.message = [image]
            with patch.object(module.asyncio, "sleep", AsyncMock()):
                await plugin.audit_group_message(event)
            if index == 1:
                self.assertFalse(event.stopped)
            else:
                self.assertTrue(event.stopped)

        self.assertEqual(
            api.recall_group_message.await_args_list,
            [
                unittest.mock.call("group-1", "image-1"),
                unittest.mock.call("group-1", "image-2"),
            ],
        )

    async def test_global_repeat_runs_when_local_audit_is_disabled(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "repeat",
                "name": "复读禁言",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_repeat_review_enabled": True,
                "global_repeat_count": 3,
                "global_repeat_window_seconds": 30,
                "global_repeat_mute_min_seconds": 10,
                "global_repeat_mute_max_seconds": 20,
                "global_repeat_reply": "复读 {duration} 秒",
                "global_repeat_at_member": False,
            }
        ]
        api = SimpleNamespace(
            set_member_mutes=AsyncMock(),
            recall_group_message=AsyncMock(),
        )
        plugin._api = lambda _event: api
        members = ("member-1", "member-2", "member-1")
        with (
            patch.object(module.secrets, "choice", return_value="member-2"),
            patch.object(module.secrets, "randbelow", return_value=7),
            patch.object(module, "future_rfc3339", return_value="expires-17"),
            patch.object(module.asyncio, "sleep", AsyncMock()),
        ):
            for index, member in enumerate(members, 1):
                event = FakeEvent(client, "同一条消息")
                event.is_at_or_wake_command = False
                event.message_obj.message_id = f"repeat-{index}"
                event.message_obj.raw_message.author = SimpleNamespace(
                    member_openid=member
                )
                await plugin.audit_group_message(event)
        self.assertTrue(event.stopped)
        api.set_member_mutes.assert_awaited_once()
        api.recall_group_message.assert_awaited_once_with("group-1", "repeat-3")

    async def test_global_rate_limit_runs_when_local_audit_is_disabled(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "rate",
                "name": "消息频率",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_rate_limit_enabled": True,
                "global_rate_limit_count": 2,
                "global_rate_limit_window_seconds": 30,
                "global_rate_limit_recall_count": 2,
                "global_rate_limit_reply": "请稍后再发",
                "global_rate_limit_at_member": False,
            }
        ]
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api
        for index in range(1, 3):
            event = FakeEvent(client, f"普通消息 {index}")
            event.is_at_or_wake_command = False
            event.message_obj.message_id = f"rate-{index}"
            event.message_obj.raw_message.author = SimpleNamespace(
                member_openid="member-1"
            )
            await plugin.audit_group_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(
            api.recall_group_message.await_args_list,
            [
                unittest.mock.call("group-1", "rate-1"),
                unittest.mock.call("group-1", "rate-2"),
            ],
        )
        self.assertEqual(client.api.messages[-1]["content"], "请稍后再发")

    async def test_command_clears_global_rate_window(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        plugin.config["global_policy_profiles"] = [
            {
                "profile_id": "rate",
                "name": "消息频率",
                "enabled": True,
                "group_openids": ["group-1"],
                "global_rate_limit_enabled": True,
                "global_rate_limit_count": 2,
                "global_rate_limit_window_seconds": 30,
                "global_rate_limit_recall_count": 2,
            }
        ]
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        first = FakeEvent(client, "普通消息")
        first.is_at_or_wake_command = False
        first.message_obj.message_id = "rate-first"
        await plugin.audit_group_message(first)

        command = FakeEvent(client, "/设置")
        command.message_obj.message_id = "rate-command"
        await plugin.audit_group_message(command)

        after_command = FakeEvent(client, "普通消息")
        after_command.is_at_or_wake_command = False
        after_command.message_obj.message_id = "rate-after-command"
        await plugin.audit_group_message(after_command)

        self.assertFalse(after_command.stopped)
        api.recall_group_message.assert_not_awaited()

    async def test_recall_reply_variable_overrides_disabled_auto_mention(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_message_reject_keywords="禁止词",
            global_message_reject_reply="{at_user} 这条消息已撤回。",
            global_message_reject_at_member=False,
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        event = FakeEvent(client, "包含禁止词")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertEqual(client.api.messages[-1]["msg_type"], 2)
        self.assertEqual(
            client.api.messages[-1]["markdown"]["content"],
            '<qqbot-at-user id="admin-1" /> 这条消息已撤回。',
        )

    async def test_empty_recall_reply_can_disable_notice_when_mention_is_off(self):
        plugin, _client = self.plugin()
        plugin.config.update(
            global_message_reject_reply="",
            global_message_reject_at_member=False,
        )
        settings = plugin._moderation_settings(plugin.config["auto_review_groups"][0])
        self.assertEqual(settings["global_keyword_reply"], "")
        self.assertFalse(settings["global_keyword_at"])

    async def test_empty_recall_reply_with_mention_sends_only_the_mention(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_message_reject_keywords="禁止词",
            global_message_reject_reply="",
            global_message_reject_at_member=True,
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        event = FakeEvent(client, "包含禁止词")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertEqual(client.api.messages[-1]["msg_type"], 2)
        self.assertEqual(
            client.api.messages[-1]["markdown"]["content"],
            '<qqbot-at-user id="admin-1" />',
        )

    async def test_global_image_keyword_has_separate_reply_and_mention_setting(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_image_ocr_enabled=True,
            global_image_reject_keywords="图片违禁词",
            global_image_reject_reply="图片文字违规，已撤回。",
            global_image_reject_at_member=False,
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        event = FakeEvent(client)
        event.message_obj.raw_message.raw_data = {
            "author": {"member_openid": "admin-1"},
            "attachments": [
                {"content_type": "image/png", "url": "https://example.com/a.png"}
            ],
        }
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        with patch.object(
            plugin, "_image_ocr_text", AsyncMock(return_value="图片违禁词")
        ):
            await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        self.assertEqual(client.api.messages[-1]["msg_type"], 0)
        self.assertEqual(client.api.messages[-1]["content"], "图片文字违规，已撤回。")

    async def test_qq_face_label_matches_image_keyword_without_vision_model(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_image_ocr_enabled=True,
            global_image_reject_keywords="龙年快乐",
            global_image_reject_reply="表情文字违规，已撤回。",
            global_image_reject_at_member=False,
            global_image_ocr_provider_id="vision",
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        event = FakeEvent(client, "[表情:[龙年快乐]] [图片]")
        event.message_obj.raw_message.raw_data = {
            "author": {"member_openid": "admin-1"},
            "attachments": [
                {
                    "content_type": "image/gif",
                    "url": "base64:data:image/gif;base64,R0lGODlhAQABAIAAAAUEBA==",
                }
            ],
        }
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        with patch.object(plugin, "_image_ocr_text", AsyncMock()) as image_ocr:
            await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        image_ocr.assert_not_awaited()
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        self.assertEqual(client.api.messages[-1]["content"], "表情文字违规，已撤回。")

    async def test_media_processing_skips_when_cpu_gate_is_busy(self):
        plugin, _client = self.plugin()
        await plugin._media_semaphore.acquire()
        try:
            with patch.object(
                module.asyncio,
                "to_thread",
                AsyncMock(return_value="should not run"),
            ) as worker:
                result = await plugin._bounded_media_thread(
                    lambda: "unused",
                    timeout=1,
                )
            self.assertIsNone(result)
            worker.assert_not_awaited()
        finally:
            plugin._media_semaphore.release()

    async def test_image_ocr_skips_vision_when_ai_gate_is_busy(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock()
        await plugin._ai_semaphore.acquire()
        await plugin._ai_semaphore.acquire()
        try:
            with patch.object(
                plugin,
                "_bounded_media_thread",
                AsyncMock(side_effect=["", "vision-ref"]),
            ):
                result = await plugin._image_ocr_text(
                    FakeEvent(client),
                    ["https://example.test/image.png"],
                    "vision",
                    2,
                    1,
                )
        finally:
            plugin._ai_semaphore.release()
            plugin._ai_semaphore.release()
        self.assertEqual(result, "")
        plugin.context.llm_generate.assert_not_awaited()

    async def test_media_timeout_keeps_cpu_gate_until_worker_finishes(self):
        plugin, _client = self.plugin()
        started = module.asyncio.Event()
        finish = module.asyncio.Event()

        async def slow_to_thread(_function, *_args):
            started.set()
            await finish.wait()
            return "done"

        with patch.object(module.asyncio, "to_thread", slow_to_thread):
            task = module.asyncio.create_task(
                plugin._bounded_media_thread(lambda: None, timeout=0.5)
            )
            await module.asyncio.wait_for(started.wait(), timeout=1)
            with self.assertRaises(module.asyncio.TimeoutError):
                await task
            self.assertTrue(plugin._media_semaphore.locked())
            self.assertIsNone(
                await plugin._bounded_media_thread(lambda: None, timeout=0.5)
            )
            finish.set()
            await module.asyncio.sleep(0)
            await module.asyncio.sleep(0)
            self.assertFalse(plugin._media_semaphore.locked())

    async def test_global_member_blacklist_overrides_disabled_group_audit(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_member_blacklist="member-1",
            global_blacklist_reply="黑名单消息已撤回。",
            global_blacklist_at_member=False,
        )
        event = FakeEvent(client, "普通消息")
        event.message_obj.raw_message.author.member_openid = "member-1"
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")
        self.assertEqual(client.api.messages[-1]["content"], "黑名单消息已撤回。")

    async def test_member_whitelist_skips_group_audit_but_keeps_keyword_reply(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0].update(
            moderation_enabled=True,
            moderation_exempt_admins=False,
            member_whitelist="member-1",
            message_reject_keywords="禁止词",
        )
        event = FakeEvent(client, "包含禁止词")
        event.message_obj.raw_message.author.member_openid = "member-1"
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertFalse(event.stopped)
        api.recall_group_message.assert_not_awaited()

    async def test_member_blacklist_wins_over_whitelist(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0].update(
            moderation_enabled=True,
            moderation_exempt_admins=False,
            member_blacklist="member-1",
            member_whitelist="member-1",
        )
        event = FakeEvent(client, "普通消息")
        event.message_obj.raw_message.author.member_openid = "member-1"
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")

    async def test_repeat_mutes_random_member_and_sends_separate_notice(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            moderation_enabled=True,
            moderation_exempt_admins=False,
            repeat_review_enabled=True,
            repeat_count=3,
            repeat_window_seconds=30,
            repeat_mute_min_seconds=10,
            repeat_mute_max_seconds=20,
            repeat_reply="复读成员已禁言 {duration} 秒。",
            repeat_at_member=False,
        )
        order = []

        async def mute(*args):
            order.append(("mute", args))

        async def recall(*args):
            order.append(("recall", args))

        async def notice(**kwargs):
            order.append(("notice", kwargs))
            return SimpleNamespace(id="notice-1")

        api = SimpleNamespace(
            set_member_mutes=mute,
            recall_group_message=recall,
        )
        plugin._api = lambda _event: api
        client.api.post_group_message = notice
        members = ["member-1", "member-2", "member-1"]

        with (
            patch.object(module.secrets, "choice", return_value="member-2"),
            patch.object(module.secrets, "randbelow", return_value=7),
            patch.object(module, "future_rfc3339", return_value="expires-17"),
        ):
            for index, member in enumerate(members, 1):
                event = FakeEvent(client, "同一条消息")
                event.message_obj.message_id = f"message-{index}"
                event.message_obj.raw_message.author = SimpleNamespace(
                    member_openid=member
                )
                await plugin.audit_group_message(event)

        self.assertEqual([item[0] for item in order], ["mute", "notice", "recall"])
        self.assertEqual(
            order[0][1],
            (
                "group-1",
                [
                    {
                        "op": "add",
                        "member_openid": "member-2",
                        "mute_expire_at": "expires-17",
                    }
                ],
            ),
        )
        self.assertEqual(order[1][1]["content"], "复读成员已禁言 17 秒。")
        self.assertEqual(order[2][1], ("group-1", "message-3"))

    async def test_repeat_mute_failure_does_not_send_success_notice(self):
        plugin, client = self.plugin()
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            moderation_enabled=True,
            moderation_exempt_admins=False,
            repeat_review_enabled=True,
            repeat_count=3,
            repeat_window_seconds=30,
            repeat_mute_min_seconds=10,
            repeat_mute_max_seconds=20,
            repeat_reply="禁言成功 {duration} 秒。",
            repeat_at_member=True,
        )
        notice = AsyncMock()
        client.api.post_group_message = notice
        api = SimpleNamespace(
            set_member_mutes=AsyncMock(side_effect=module.QQAPIError(status=500)),
            recall_group_message=AsyncMock(),
        )
        plugin._api = lambda _event: api

        with (
            patch.object(module.secrets, "choice", return_value="member-2"),
            patch.object(module.secrets, "randbelow", return_value=0),
            patch.object(module, "future_rfc3339", return_value="expires-10"),
        ):
            for index, member in enumerate(
                ["member-1", "member-2", "member-1"], 1
            ):
                event = FakeEvent(client, "同一条消息")
                event.message_obj.message_id = f"message-{index}"
                event.message_obj.raw_message.author = SimpleNamespace(
                    member_openid=member
                )
                await plugin.audit_group_message(event)

        notice.assert_not_awaited()

    async def test_member_list_matches_union_openid_and_bound_uid(self):
        plugin, client = self.plugin()
        plugin.config.update(global_member_whitelist="union-1")
        plugin._uid_bindings["188144093"] = {
            "uid": "188144093",
            "groups": ["group-1"],
            "members": {"group-1": "member-1"},
        }
        event = FakeEvent(client, "普通消息")
        event.message_obj.raw_message.author = SimpleNamespace(
            member_openid="member-1", union_openid="union-1"
        )
        self.assertTrue(
            plugin._member_list_matches(
                event, "group-1", "member-1", ["UNION-1"]
            )
        )
        plugin.config["global_member_whitelist"] = "188144093"
        self.assertTrue(
            plugin._member_list_matches(
                event,
                "group-1",
                "member-1",
                module.parse_member_list("188144093"),
            )
        )

    async def test_ai_record_only_keeps_message_and_binding_violation_count(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_action="record_only",
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        plugin._uid_bindings["188144093"] = {
            "uid": "188144093",
            "member_openid": "admin-1",
            "groups": ["group-1"],
            "violation_count": 2,
        }
        event = FakeEvent(client, "普通聊天")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        async def block_with_trace(*_args, **kwargs):
            kwargs["result"].update(
                provider="provider-1",
                decision="BLOCK",
                confidence=96,
                reason="测试判定",
            )
            return True

        with patch.object(plugin, "_ai_blocks_message", side_effect=block_with_trace):
            await plugin.audit_group_message(event)

        self.assertFalse(event.stopped)
        api.recall_group_message.assert_not_awaited()
        self.assertEqual(client.api.messages, [])
        self.assertEqual(plugin._uid_bindings["188144093"]["violation_count"], 2)
        self.assertEqual(plugin._violation_records[-1]["action"], "record_only")
        self.assertEqual(plugin._violation_records[-1]["ai_provider"], "provider-1")

    async def test_ai_record_only_still_sends_keyword_reply(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_action="record_only",
        )
        entry = plugin.config["auto_review_groups"][0]
        entry.update(
            moderation_enabled=False,
            keyword_replies=[{"keyword": "帮助", "reply": "群帮助"}],
        )
        event = FakeEvent(client, "需要帮助")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        with patch.object(plugin, "_ai_blocks_message", AsyncMock(return_value=True)):
            await plugin.audit_group_message(event)

        api.recall_group_message.assert_not_awaited()
        self.assertEqual(client.api.messages[-1]["content"], "群帮助")
        self.assertEqual(plugin._violation_records[-1]["action"], "record_only")

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
            "type": "DYNAMIC_TYPE_DRAW",
            "title": "新动态",
            "text": "正文",
            "url": "https://www.bilibili.com/opus/dynamic-1",
            "cover": "https://i0.hdslb.com/bfs/archive/cover.jpg",
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

        push = AsyncMock(return_value=True)
        with (
            patch.object(module, "fetch_wbi_keys", return_value=("a", "b")),
            patch.object(module, "fetch_space_dynamics", return_value={}),
            patch.object(module, "parse_dynamic_items", return_value=[item]),
            patch.object(plugin, "_push_bilibili_message", push),
        ):
            self.assertTrue(await plugin._poll_bilibili_dynamics(subscriptions))
        push_text = push.await_args.args[1]
        self.assertIn("# B站动态", push_text)
        self.assertIn("**UP** · 图文", push_text)
        self.assertIn(
            "![封面 #300px #169px](https://i0.hdslb.com/bfs/archive/cover.jpg)",
            push_text,
        )
        self.assertIn("正文", push_text)
        self.assertNotIn("> 正文", push_text)
        self.assertIn(
            "[查看原动态 ↗](https://www.bilibili.com/opus/dynamic-1)",
            push_text,
        )
        self.assertNotIn("\n-\n", push_text)

    async def test_bilibili_card_delivery_uses_media_and_keeps_original_link(self):
        plugin, client = self.plugin()
        plugin._platform_clients = lambda: {"platform-1": client}
        send_card = AsyncMock(return_value=SimpleNamespace(id="card-1"))
        with (
            patch.object(
                module,
                "render_bilibili_card",
                return_value=b"\x89PNG\r\ncard",
            ),
            patch.object(plugin, "_send_group_card", send_card),
        ):
            delivered = await plugin._push_bilibili_message(
                [
                    {
                        "group_openid": "group-1",
                        "platform_id": "platform-1",
                        "dynamic": True,
                        "live": False,
                    }
                ],
                "# B站动态\n\n[查看原动态](https://www.bilibili.com/opus/1)",
                "dynamic",
                card_data={
                    "author": "UP",
                    "kind": "图文",
                    "link": "https://www.bilibili.com/opus/1",
                },
            )

        self.assertTrue(delivered)
        self.assertEqual(send_card.await_args.args[1], "group-1")
        self.assertEqual(
            send_card.await_args.kwargs["link"],
            "https://www.bilibili.com/opus/1",
        )
        self.assertEqual(client.api.messages, [])

    async def test_bilibili_card_renderer_accepts_temp_file_and_removes_it(self):
        plugin, _client = self.plugin()
        temp_path = __import__("tempfile").NamedTemporaryFile(
            suffix=".png", delete=False
        )
        temp_path.write(b"\x89PNG\r\ncard")
        temp_path.close()
        plugin.html_render = AsyncMock(return_value=temp_path.name)

        with patch.object(
            module,
            "render_bilibili_card",
            side_effect=RuntimeError("local unavailable"),
        ):
            rendered = await plugin._render_bilibili_card(
                {"author": "UP", "kind": "图文"}
            )

        self.assertTrue(rendered.startswith(b"\x89PNG"))
        self.assertFalse(__import__("os").path.exists(temp_path.name))

    async def test_bilibili_group_card_uploads_then_sends_media(self):
        plugin, client = self.plugin()
        upload = AsyncMock(
            return_value={"file_uuid": "uuid-1", "file_info": "info-1", "ttl": 60}
        )
        with patch.object(module.QQGroupAPI, "upload_group_image", upload):
            await plugin._send_group_card(
                client,
                "group-1",
                b"\x89PNG\r\ncard",
                link="https://www.bilibili.com/opus/1",
            )

        upload.assert_awaited_once_with("group-1", b"\x89PNG\r\ncard")
        self.assertEqual(
            client.api.messages[-1],
            {
                "group_openid": "group-1",
                "msg_type": 7,
                "media": {"file_info": "info-1"},
                "content": "查看原动态：https://www.bilibili.com/opus/1",
            },
        )

    async def test_bilibili_live_push_uses_named_room_link(self):
        plugin, _client = self.plugin()
        subscriptions = {
            "188144093": [
                {
                    "group_openid": "group-1",
                    "platform_id": "platform-1",
                    "dynamic": False,
                    "live": True,
                }
            ]
        }
        plugin._bilibili_state["live"]["188144093"] = {
            "live_status": 0,
            "live_time": "",
            "room_id": "123",
            "uname": "UP",
            "title": "旧标题",
        }
        push = AsyncMock(return_value=True)
        current = {
            "live_status": 1,
            "live_time": "2026-08-24 12:00:00",
            "room_id": "123",
            "uname": "UP",
            "title": "新标题",
            "user_cover": "https://i0.hdslb.com/bfs/live/cover.jpg",
        }
        with (
            patch.object(module, "fetch_live_statuses", return_value={"188144093": current}),
            patch.object(plugin, "_push_bilibili_message", push),
        ):
            self.assertTrue(await plugin._poll_bilibili_live(subscriptions))
        text = push.await_args.args[1]
        self.assertIn("## 🔴 正在直播", text)
        self.assertIn("**UP** · 直播中", text)
        self.assertIn(
            "![封面 #300px #169px](https://i0.hdslb.com/bfs/live/cover.jpg)",
            text,
        )
        self.assertIn("**新标题**", text)
        self.assertIn("[进入直播间 ↗](https://live.bilibili.com/123)", text)
        self.assertEqual(
            plugin._bilibili_state["live"]["188144093"]["live_status"],
            1,
        )

    async def test_bilibili_empty_dynamic_card_has_no_placeholder_text(self):
        plugin, _client = self.plugin()
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
            "id": "dynamic-empty",
            "uid": "188144093",
            "author": "UP",
            "pub_ts": 100,
            "type": "DYNAMIC_TYPE_DRAW",
            "title": "-",
            "text": "-",
            "url": "https://www.bilibili.com/opus/dynamic-empty",
            "cover": "",
        }
        plugin._bilibili_state["dynamic"]["188144093"] = {
            "seen": [],
            "max_pub_ts": 0,
        }
        push = AsyncMock(return_value=True)
        with (
            patch.object(module, "fetch_wbi_keys", return_value=("a", "b")),
            patch.object(module, "fetch_space_dynamics", return_value={}),
            patch.object(module, "parse_dynamic_items", return_value=[item]),
            patch.object(plugin, "_push_bilibili_message", push),
        ):
            self.assertTrue(await plugin._poll_bilibili_dynamics(subscriptions))

        text = push.await_args.args[1]
        self.assertEqual(
            text,
            "# B站动态\n\n**UP** · 图文 · 01-01 08:01\n\n"
            "**发布了一条图文动态**\n\n"
            "[查看原动态 ↗](https://www.bilibili.com/opus/dynamic-empty)",
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
        self.assertIn(
            '<qqbot-at-user id="member-1" />',
            client.api.messages[-1]["markdown"]["content"],
        )

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

    async def test_notice_fallback_never_exposes_raw_mention_markup(self):
        plugin, client = self.plugin()
        client.api.post_group_message = AsyncMock(
            side_effect=[RuntimeError("markdown disabled"), SimpleNamespace(id="sent")]
        )

        await plugin._send_group_notice(
            client,
            "group-1",
            "图片文字命中禁止关键词，已撤回。",
            member_openid="member-1",
        )

        calls = client.api.post_group_message.await_args_list
        self.assertEqual(calls[0].kwargs["msg_type"], 2)
        self.assertEqual(calls[1].kwargs["msg_type"], 0)
        self.assertNotIn("qqbot-at-user", calls[1].kwargs["content"])
        self.assertEqual(calls[1].kwargs["content"], "图片文字命中禁止关键词，已撤回。")

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
        self.assertLessEqual(len(content), 4000)
        self.assertTrue(content.endswith('<qqbot-at-user id="member-1" />'))

        plugin.config["mute_success_message"] = "{at_user}" * 40
        await plugin._send_mute_success(event, "member-1", "45", "ignored")
        content = client.api.messages[-1]["markdown"]["content"]
        mention = '<qqbot-at-user id="member-1" />'
        self.assertEqual(content, mention * 40)

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

    async def test_ai_moderation_uses_fallback_only_after_primary_failure(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                RuntimeError("primary unavailable"),
                SimpleNamespace(
                    role="assistant", completion_text="BLOCK confidence=95 reason=明确违规"
                ),
            ]
        )

        blocked = await plugin._ai_blocks_message(
            event,
            "待审核",
            [],
            "primary",
            "fallback",
        )

        self.assertTrue(blocked)
        self.assertEqual(
            [call.kwargs["chat_provider_id"] for call in plugin.context.llm_generate.await_args_list],
            ["primary", "fallback"],
        )

        plugin.context.llm_generate.reset_mock()
        plugin.context.llm_generate.side_effect = None
        plugin.context.llm_generate.return_value = SimpleNamespace(
            role="assistant", completion_text="ALLOW"
        )
        self.assertFalse(
            await plugin._ai_blocks_message(
                event,
                "正常消息",
                [],
                "primary",
                "fallback",
            )
        )
        plugin.context.llm_generate.assert_awaited_once()

    async def test_ai_error_response_keeps_detail_and_tries_fallback(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        error_response = SimpleNamespace(
            role="err",
            completion_text="",
            result_chain=SimpleNamespace(
                get_plain_text=lambda: "provider unavailable: quota exhausted"
            ),
        )
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                error_response,
                SimpleNamespace(
                    role="assistant",
                    completion_text="BLOCK confidence=96 reason=明确违规",
                ),
            ]
        )

        with self.assertLogs(plugin.logger, level="DEBUG") as captured:
            blocked = await plugin._ai_blocks_message(
                event,
                "待审核",
                [],
                "primary",
                ["fallback"],
            )

        self.assertTrue(blocked)
        self.assertIn("quota exhausted", "\n".join(captured.output))
        self.assertEqual(
            [call.kwargs["chat_provider_id"] for call in plugin.context.llm_generate.await_args_list],
            ["primary", "fallback"],
        )

    async def test_ai_image_review_caps_preprocessing_images(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="ALLOW confidence=99 reason=正常"
            )
        )

        async def bounded(_function, value, *, timeout):
            return value

        with patch.object(plugin, "_bounded_media_thread", side_effect=bounded) as media:
            blocked = await plugin._ai_blocks_message(
                FakeEvent(client, "图片"),
                "图片",
                [f"https://example.test/{index}.png" for index in range(10)],
                "primary",
                image_review_enabled=True,
            )

        self.assertFalse(blocked)
        self.assertEqual(media.await_count, module.AI_REVIEW_MAX_IMAGES)
        self.assertEqual(
            len(plugin.context.llm_generate.await_args.kwargs["image_urls"]),
            module.AI_REVIEW_MAX_IMAGES,
        )

    async def test_ai_image_review_skips_image_when_normalization_fails(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="ALLOW confidence=99 reason=正常"
            )
        )

        with patch.object(
            plugin,
            "_bounded_media_thread",
            AsyncMock(side_effect=RuntimeError("invalid image")),
        ):
            blocked = await plugin._ai_blocks_message(
                FakeEvent(client, "图片说明"),
                "图片说明",
                ["https://example.test/bad.png"],
                "primary",
                image_review_enabled=True,
            )

        self.assertFalse(blocked)
        plugin.context.llm_generate.assert_awaited_once()
        self.assertIsNone(
            plugin.context.llm_generate.await_args.kwargs["image_urls"]
        )

    async def test_ai_image_preprocessing_does_not_consume_provider_timeout(self):
        plugin, client = self.plugin()
        clock = [0.0]
        calls = []

        async def normalize(_function, value, *, timeout):
            clock[0] += 4.9
            return value

        async def provider_call(**kwargs):
            calls.append(kwargs["chat_provider_id"])
            if len(calls) == 1:
                clock[0] += 0.2
                raise RuntimeError("primary unavailable")
            return SimpleNamespace(
                role="assistant",
                completion_text="ALLOW confidence=99 reason=正常",
            )

        plugin.context.llm_generate = provider_call
        with (
            patch.object(plugin, "_bounded_media_thread", side_effect=normalize),
            patch.object(module.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            blocked = await plugin._ai_blocks_message(
                FakeEvent(client, "图片说明"),
                "图片说明",
                ["https://example.test/image.png"],
                "primary",
                ["fallback"],
                timeout_seconds=5,
                image_review_enabled=True,
            )

        self.assertFalse(blocked)
        self.assertEqual(calls, ["primary", "fallback"])

    async def test_join_config_backup_restores_missing_entries(self):
        plugin, _client = self.plugin()
        await plugin._save_config_backup()
        backup = plugin._config_backup
        plugin.config.pop("auto_review_groups")
        plugin._config_reset_candidate = True

        self.assertTrue(await plugin._restore_config_backup())
        self.assertEqual(
            plugin.config["auto_review_groups"],
            backup["auto_review_groups"],
        )

    async def test_join_config_backup_restores_schema_default_empty_list(self):
        plugin, _client = self.plugin()
        await plugin._save_config_backup()
        backup = plugin._config_backup
        plugin.config["auto_review_groups"] = []
        plugin._config_reset_candidate = True

        self.assertTrue(await plugin._restore_config_backup())
        self.assertEqual(
            plugin.config["auto_review_groups"],
            backup["auto_review_groups"],
        )

    async def test_startup_migration_does_not_overwrite_join_backup(self):
        config = TestConfig(auto_review_groups=[])
        plugin = module.QQGroupAdmin(SimpleNamespace(), config)
        plugin._kv[module.CONFIG_BACKUP_KEY] = {
            "auto_review_groups": [{"group_openid": "kept-group"}],
            module.WELCOME_RULES_KEY: [
                {"name": "入群欢迎", "group_openids": "kept-group"}
            ],
        }

        await plugin._load_state()
        self.assertEqual(
            plugin._config_backup["auto_review_groups"][0]["group_openid"],
            "kept-group",
        )
        self.assertTrue(await plugin._restore_config_backup())
        self.assertEqual(
            plugin.config["auto_review_groups"][0]["group_openid"],
            "kept-group",
        )
        self.assertEqual(
            plugin.config[module.WELCOME_RULES_KEY][0]["name"],
            "入群欢迎",
        )

    async def test_partial_welcome_reset_restores_without_touching_groups(self):
        plugin, _client = self.plugin()
        plugin.config[module.WELCOME_RULES_KEY] = []
        plugin._config_backup = {
            "auto_review_groups": [{"group_openid": "kept-group"}],
            module.WELCOME_RULES_KEY: [{"name": "欢迎", "group_openids": "kept-group"}],
        }
        plugin._config_reset_keys = {module.WELCOME_RULES_KEY}
        plugin._config_reset_candidate = True

        self.assertTrue(await plugin._restore_config_backup())
        self.assertEqual(
            plugin.config["auto_review_groups"][0]["group_openid"],
            "group-1",
        )
        self.assertEqual(plugin.config[module.WELCOME_RULES_KEY][0]["name"], "欢迎")

    async def test_corrupt_state_still_loads_separate_config_backup(self):
        config = TestConfig(auto_review_groups=[])
        plugin = module.QQGroupAdmin(SimpleNamespace(), config)
        plugin._kv[module.STATE_KEY] = "corrupt"
        plugin._kv[module.CONFIG_BACKUP_KEY] = {
            "auto_review_groups": [{"group_openid": "recovered"}],
        }

        await plugin._load_state()
        self.assertEqual(
            plugin._config_backup["auto_review_groups"][0]["group_openid"],
            "recovered",
        )

    async def test_backup_save_retries_after_inflight_config_change(self):
        plugin, _client = self.plugin()
        plugin._config_backup_ready = True
        writes = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def put(key, value):
            writes.append((key, value["auto_review_groups"][0]["group_openid"]))
            if len(writes) == 1:
                entered.set()
                await release.wait()

        plugin.put_kv_data = put
        plugin._schedule_config_backup()
        await asyncio.sleep(0.25)
        await entered.wait()
        plugin.config["auto_review_groups"][0]["group_openid"] = "latest"
        plugin._schedule_config_backup()
        release.set()
        await asyncio.wait_for(plugin._config_backup_task, timeout=1)

        self.assertEqual(writes[-1][1], "latest")
        self.assertGreaterEqual(len(writes), 2)

    async def test_join_config_backup_does_not_restore_intentional_empty_list(self):
        plugin, _client = self.plugin()
        await plugin._save_config_backup()
        plugin.config["auto_review_groups"] = []
        plugin._config_reset_candidate = False

        self.assertFalse(await plugin._restore_config_backup())
        self.assertEqual(plugin.config["auto_review_groups"], [])

    async def test_ai_confirmation_allow_overrides_primary_block(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    role="assistant",
                    completion_text="BLOCK confidence=98 reason=疑似违规",
                ),
                SimpleNamespace(
                    role="assistant",
                    completion_text="ALLOW confidence=99 reason=正常聊天",
                ),
            ]
        )
        result = {}

        blocked = await plugin._ai_blocks_message(
            event,
            "待审核",
            [],
            "primary",
            confirm_provider_id="confirm",
            result=result,
        )

        self.assertFalse(blocked)
        self.assertEqual(
            [
                call.kwargs["chat_provider_id"]
                for call in plugin.context.llm_generate.await_args_list
            ],
            ["primary", "confirm"],
        )
        self.assertEqual(result["confirm_provider"], "confirm")
        self.assertEqual(result["confirm_decision"], "ALLOW")

    async def test_ai_confirmation_block_confirms_primary_block(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    role="assistant",
                    completion_text="BLOCK confidence=98 reason=疑似违规",
                ),
                SimpleNamespace(
                    role="assistant",
                    completion_text="拦截 置信度=97 原因=确认违规",
                ),
            ]
        )
        result = {}

        blocked = await plugin._ai_blocks_message(
            event,
            "待审核",
            [],
            "primary",
            confirm_provider_id="confirm",
            result=result,
        )

        self.assertTrue(blocked)
        self.assertEqual(result["confirm_decision"], "BLOCK")
        self.assertFalse(result["confirmation_failed"])

    async def test_ai_confirmation_failure_downgrades_recall_to_record_only(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary",
            global_ai_review_confirm_provider_id="confirm",
            global_ai_review_action="recall",
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = True
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    role="assistant",
                    completion_text="BLOCK confidence=98 reason=疑似违规",
                ),
                RuntimeError("Authorization: Bearer sk-confirm-secret"),
            ]
        )
        event = FakeEvent(client, "待审核")
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertFalse(event.stopped)
        api.recall_group_message.assert_not_awaited()
        record = plugin._violation_records[-1]
        self.assertEqual(record["action"], "record_only")
        self.assertEqual(record["ai_confirm_provider"], "confirm")
        self.assertEqual(record["ai_confirm_decision"], "ERROR")
        self.assertEqual(record["ai_confirm_reason"], "模型调用失败")
        self.assertNotIn("sk-confirm-secret", str(record))

    async def test_ai_provider_failures_do_not_persist_or_log_credentials(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                RuntimeError("Authorization: Bearer sk-primary-secret"),
                RuntimeError(
                    "api_key=sk-fallback-secret url=https://api.example.test/v1"
                ),
            ]
        )
        result = {}

        with self.assertLogs(plugin.logger, level="DEBUG") as captured:
            blocked = await plugin._ai_blocks_message(
                FakeEvent(client, "待审核"),
                "待审核",
                [],
                "primary",
                ["fallback"],
                result=result,
            )

        evidence = str(result) + "\n" + "\n".join(captured.output)
        self.assertFalse(blocked)
        self.assertEqual(result["decision"], "ERROR")
        self.assertIn("<redacted>", evidence)
        self.assertIn("<url>", evidence)
        self.assertNotIn("sk-primary-secret", evidence)
        self.assertNotIn("sk-fallback-secret", evidence)
        self.assertNotIn("api.example.test", evidence)

    async def test_ai_confirmation_provider_cannot_be_initial_candidate(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                role="assistant",
                completion_text="BLOCK confidence=98 reason=疑似违规",
            )
        )
        result = {}

        blocked = await plugin._ai_blocks_message(
            FakeEvent(client, "待审核"),
            "待审核",
            [],
            "primary",
            ["confirm"],
            confirm_provider_id="confirm",
            result=result,
        )

        self.assertTrue(blocked)
        plugin.context.llm_generate.assert_awaited_once()
        self.assertTrue(result["confirmation_failed"])
        self.assertEqual(result["confirm_reason"], "确认模型与初判候选模型重复")

    async def test_ai_image_review_is_opt_in(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "")
        plugin.context.llm_generate = AsyncMock()
        self.assertFalse(
            await plugin._ai_blocks_message(event, "", ["https://example.test/a.png"], "primary")
        )
        plugin.context.llm_generate.assert_not_awaited()

    async def test_ai_image_review_skips_signed_gif_attachment(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "")
        gif_url = "https://example.test/signed-media?id=1"
        event.message_obj.raw_message.raw_data = {
            "author": {"member_openid": "admin-1"},
            "attachments": [{"content_type": "image/gif", "url": gif_url}],
        }
        plugin.context.llm_generate = AsyncMock()

        self.assertFalse(
            await plugin._ai_blocks_message(
                event,
                "",
                [gif_url],
                "primary",
                image_review_enabled=True,
            )
        )
        plugin.context.llm_generate.assert_not_awaited()

    async def test_global_ai_skips_qq_gif_placeholders_at_event_entry(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary",
            global_ai_review_images_enabled=True,
            global_ai_review_action="recall",
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        gif_url = "https://example.test/signed-media?id=1"
        event = FakeEvent(client, "[表情:[龙年快乐]] [图片]")
        event.message_obj.raw_message.raw_data = {
            "author": {"member_openid": "admin-1"},
            "attachments": [{"content_type": "image/gif", "url": gif_url}],
        }
        plugin.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                role="assistant",
                completion_text="BLOCK confidence=99 reason=误判",
            )
        )
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        await plugin.audit_group_message(event)

        self.assertFalse(event.stopped)
        plugin.context.llm_generate.assert_not_awaited()
        api.recall_group_message.assert_not_awaited()

    async def test_global_ai_runs_for_bound_group_without_local_review(self):
        plugin, client = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary",
            global_ai_review_action="recall",
            global_ai_reject_reply="",
            global_ai_reject_at_member=False,
        )
        plugin.config["auto_review_groups"][0]["moderation_enabled"] = False
        plugin.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                role="assistant",
                completion_text="BLOCK confidence=99 reason=明确诈骗引流",
            )
        )
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api
        event = FakeEvent(client, "明确诈骗引流")

        await plugin.audit_group_message(event)

        self.assertTrue(event.stopped)
        plugin.context.llm_generate.assert_awaited_once()
        api.recall_group_message.assert_awaited_once_with("group-1", "message-1")

    async def test_global_ai_does_not_run_for_unbound_group(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"] = []
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary",
        )
        plugin.context.llm_generate = AsyncMock()

        await plugin.audit_group_message(FakeEvent(client, "普通消息"))

        plugin.context.llm_generate.assert_not_awaited()

    def test_global_ai_config_migrates_once_and_ignores_group_overrides(self):
        client = FakeClient()
        platform = SimpleNamespace(get_client=lambda: client)
        context = SimpleNamespace(get_platform_inst=lambda _platform_id: platform)
        config = TestConfig(
            auto_review_groups=[
                {
                    "group_openid": "group-1",
                    "ai_review_enabled": True,
                    "ai_review_provider_id": "primary-a",
                    "ai_review_fallback_provider_id": "fallback-a",
                },
                {
                    "group_openid": "group-2",
                    "ai_review_enabled": False,
                    "ai_review_provider_id": "primary-b",
                    "ai_review_fallback_provider_id": "fallback-a",
                },
            ]
        )
        plugin = module.QQGroupAdmin(context, config)

        self.assertTrue(config["global_ai_review_enabled"])
        self.assertEqual(config["global_ai_review_provider_id"], "primary-a")
        self.assertEqual(
            config["global_ai_review_fallback_provider_ids"], ["fallback-a"]
        )
        first = plugin._moderation_settings(config["auto_review_groups"][0])
        second = plugin._moderation_settings(config["auto_review_groups"][1])
        self.assertEqual(first["ai_provider_id"], second["ai_provider_id"])
        self.assertEqual(
            first["ai_fallback_provider_ids"], second["ai_fallback_provider_ids"]
        )

    async def test_global_ai_fallback_list_is_ordered_and_deduped(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                RuntimeError("primary unavailable"),
                RuntimeError("first fallback unavailable"),
                SimpleNamespace(
                    role="assistant", completion_text="BLOCK confidence=95 reason=明确违规"
                ),
            ]
        )

        blocked = await plugin._ai_blocks_message(
            event,
            "待审核",
            [],
            "primary",
            ["fallback-1", "fallback-1", "fallback-2"],
        )

        self.assertTrue(blocked)
        self.assertEqual(
            [call.kwargs["chat_provider_id"] for call in plugin.context.llm_generate.await_args_list],
            ["primary", "fallback-1", "fallback-2"],
        )

    def test_ai_error_response_extracts_and_redacts_provider_detail(self):
        response = {
            "role": "err",
            "error": {"message": "401 token=sk-test-secret request https://api.invalid/x"},
        }
        detail = module.QQGroupAdmin._ai_response_error(response)
        self.assertIn("401", detail)
        self.assertNotIn("sk-test-secret", detail)
        self.assertNotIn("https://api.invalid/x", detail)

    async def test_ai_total_timeout_reserves_time_for_fallbacks(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        calls = []

        async def provider_call(**kwargs):
            calls.append(kwargs["chat_provider_id"])
            if len(calls) == 1:
                await asyncio.sleep(0.08)
                raise TimeoutError()
            return SimpleNamespace(
                role="assistant",
                completion_text="BLOCK confidence=96 reason=明确违规",
            )

        plugin.context.llm_generate = provider_call
        blocked = await plugin._ai_blocks_message(
            event,
            "待审核",
            [],
            "primary",
            ["fallback"],
            timeout_seconds=5,
        )

        self.assertTrue(blocked)
        self.assertEqual(calls, ["primary", "fallback"])

    async def test_ai_confirmation_uses_only_remaining_budget_after_initial_block(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "待审核")
        timeouts = []

        class NoopTimeout:
            async def __aenter__(self):
                return None

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        def capture_timeout(delay):
            timeouts.append(delay)
            return NoopTimeout()

        plugin.context.llm_generate = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    role="assistant",
                    completion_text="BLOCK confidence=98 reason=明确违规",
                ),
                SimpleNamespace(
                    role="assistant",
                    completion_text="ALLOW confidence=99 reason=正常聊天",
                ),
            ]
        )

        with patch.object(module.asyncio, "timeout", side_effect=capture_timeout):
            blocked = await plugin._ai_blocks_message(
                event,
                "待审核",
                [],
                "primary",
                ["fallback-1", "fallback-2", "fallback-3"],
                timeout_seconds=5,
                confirm_provider_id="confirm",
            )

        self.assertFalse(blocked)
        self.assertEqual(len(timeouts), 2)
        self.assertGreater(timeouts[0], 1.1)
        self.assertLess(timeouts[0], 1.5)
        self.assertGreater(timeouts[1], 4.5)
        self.assertEqual(
            [call.kwargs["chat_provider_id"] for call in plugin.context.llm_generate.await_args_list],
            ["primary", "confirm"],
        )

    async def test_ai_review_fails_fast_when_concurrency_gate_is_busy(self):
        plugin, client = self.plugin()
        plugin.context.llm_generate = AsyncMock()
        await plugin._ai_semaphore.acquire()
        await plugin._ai_semaphore.acquire()
        try:
            result = {}
            blocked = await plugin._ai_blocks_message(
                FakeEvent(client, "待审核"),
                "待审核",
                [],
                "primary",
                ["fallback"],
                timeout_seconds=5,
                result=result,
            )
        finally:
            plugin._ai_semaphore.release()
            plugin._ai_semaphore.release()

        self.assertFalse(blocked)
        plugin.context.llm_generate.assert_not_awaited()
        self.assertIn("并发繁忙", result["reason"])

    def test_runtime_global_ai_validation_accepts_multiple_fallbacks(self):
        runtime = module.GroupAdminWeb._runtime_settings(
            {
                "global_ai_review_enabled": True,
                "global_ai_review_provider_id": "primary",
                "global_ai_review_fallback_provider_ids": [
                    "fallback-1",
                    "fallback-1",
                    "fallback-2",
                ],
            }
        )
        self.assertTrue(runtime["global_ai_review_enabled"])
        self.assertEqual(
            runtime["global_ai_review_fallback_provider_ids"],
            ["fallback-1", "fallback-2"],
        )
        with self.assertRaisesRegex(ValueError, "不能出现在"):
            module.GroupAdminWeb._runtime_settings(
                {
                    "global_ai_review_provider_id": "primary",
                    "global_ai_review_fallback_provider_ids": "fallback-1\nprimary",
                }
            )

        runtime = module.GroupAdminWeb._runtime_settings(
            {
                "global_ai_review_timeout_seconds": 90,
                "global_ai_review_block_threshold": 97,
                "global_ai_review_images_enabled": True,
                "global_image_reject_keywords": "水印\n广告",
                "global_image_ocr_enabled": True,
                "global_image_ocr_provider_id": "vision",
                "global_image_ocr_timeout_seconds": 8,
                "global_image_ocr_max_images": 2,
            }
        )
        self.assertEqual(runtime["global_ai_review_timeout_seconds"], 90)
        self.assertEqual(runtime["global_ai_review_block_threshold"], 97)
        self.assertTrue(runtime["global_ai_review_images_enabled"])
        self.assertEqual(runtime["global_image_reject_keywords"], "水印\n广告")
        self.assertEqual(runtime["global_image_ocr_max_images"], 2)

    async def test_runtime_ai_confirmation_provider_is_distinct(self):
        runtime = module.GroupAdminWeb._runtime_settings(
            {"global_ai_review_confirm_provider_id": "confirm"}
        )
        self.assertEqual(
            runtime["global_ai_review_confirm_provider_id"],
            "confirm",
        )
        for settings in (
            {
                "global_ai_review_provider_id": "same",
                "global_ai_review_confirm_provider_id": "same",
            },
            {
                "global_ai_review_fallback_provider_ids": ["same"],
                "global_ai_review_confirm_provider_id": "same",
            },
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(
                ValueError,
                "确认模型不能",
            ):
                module.GroupAdminWeb._runtime_settings(settings)

        plugin, _ = self.plugin()
        plugin.config.update(
            global_ai_review_provider_id="primary",
            global_ai_review_fallback_provider_ids=["fallback"],
        )
        with self.assertRaisesRegex(ValueError, "确认模型不能"):
            await plugin.web_save_runtime_settings(
                {"global_ai_review_confirm_provider_id": "fallback"}
            )

    def test_runtime_page_exposes_ai_confirmation_provider(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        field = schema["global_ai_review_confirm_provider_id"]
        self.assertEqual(field["_special"], "select_provider")
        self.assertEqual(field["default"], "")

        html = (ROOT / "pages/groups/index.html").read_text(encoding="utf-8")
        script = (ROOT / "pages/groups/app.js").read_text(encoding="utf-8")
        self.assertIn('id="runtime-ai-confirm-provider"', html)
        self.assertIn(
            'global_ai_review_confirm_provider_id: element("runtime-ai-confirm-provider").value',
            script,
        )
        self.assertGreaterEqual(script.count('"runtime-ai-confirm-provider"'), 3)

    def test_ai_decision_requires_confidence_and_is_conservative(self):
        self.assertIsNone(module.QQGroupAdmin._ai_decision("BLOCK", 95))
        self.assertTrue(
            module.QQGroupAdmin._ai_decision("BLOCK confidence=96 reason=x", 95)
        )
        self.assertFalse(
            module.QQGroupAdmin._ai_decision("ALLOW confidence=1 reason=normal", 95)
        )
        self.assertFalse(
            module.QQGroupAdmin._ai_decision("BLOCK confidence=80 reason=uncertain", 95)
        )
        self.assertFalse(
            module.QQGroupAdmin._ai_decision(
                "ALLOW confidence=99 reason=用户讨论了 BLOCK 这个单词",
                95,
            )
        )
        self.assertIsNone(
            module.QQGroupAdmin._ai_decision(
                "分析如下：内容正常\nBLOCK confidence=99 reason=不要采用",
                95,
            )
        )
        self.assertTrue(
            module.QQGroupAdmin._ai_decision("拦截。置信度=99 原因=明确违规", 95)
        )
        self.assertTrue(
            module.QQGroupAdmin._ai_decision("拒绝；分数=99 理由=明确违规", 95)
        )
        self.assertFalse(
            module.QQGroupAdmin._ai_decision("允许。置信度=99 原因=正常", 95)
        )

    async def test_runtime_ai_save_checks_existing_fallbacks_when_provider_only_changes(self):
        plugin, _ = self.plugin()
        plugin.config.update(
            global_ai_review_enabled=True,
            global_ai_review_provider_id="primary-old",
            global_ai_review_fallback_provider_ids=["fallback-1"],
        )
        with self.assertRaisesRegex(ValueError, "不能出现在"):
            await plugin.web_save_runtime_settings(
                {"global_ai_review_provider_id": "fallback-1"}
            )

    async def test_violation_records_keep_content_and_identity_fields(self):
        plugin, _ = self.plugin()
        await plugin._record_uid_violation(
            "",
            "group-1",
            "member-1",
            "命中关键词",
            content="违规消息原文",
            message_id="message-9",
            request={"username": "测试成员", "union_openid": "union-1"},
        )

        identities = await plugin.web_identities()
        self.assertEqual(len(identities["violations"]), 1)
        record = identities["violations"][0]
        self.assertEqual(record["content"], "违规消息原文")
        self.assertEqual(record["username"], "测试成员")
        self.assertEqual(record["union_openid"], "union-1")
        self.assertEqual(record["group_openid"], "group-1")
        self.assertEqual(record["message_id"], "message-9")
        self.assertTrue(record["record_id"])
        self.assertEqual(record["review_status"], "pending")
        self.assertEqual(record["reviewed_at"], 0)

    async def test_throttled_violation_records_flush_without_waiting_for_shutdown(self):
        plugin, _ = self.plugin()
        await plugin._record_uid_violation(
            "",
            "group-1",
            "member-1",
            "第一条",
            content="一",
        )
        plugin._last_violation_state_save_at = module.time.monotonic() - 4.98
        await plugin._record_uid_violation(
            "",
            "group-1",
            "member-2",
            "第二条",
            content="二",
        )
        await module.asyncio.sleep(0.08)
        saved = plugin._kv[module.STATE_KEY]["violation_records"]
        self.assertEqual([record["content"] for record in saved], ["一", "二"])

    async def test_violation_state_migrates_ids_and_review_statuses(self):
        plugin, _ = self.plugin()
        plugin._kv[module.STATE_KEY] = {
            "violation_records": [
                {"record_id": "duplicate", "content": "first"},
                {"record_id": "duplicate", "content": "second"},
                {"record_id": "", "review_status": "invalid", "reviewed_at": "bad", "content": "third"},
            ]
        }

        await plugin._load_state()

        records = plugin._violation_records
        record_ids = [record["record_id"] for record in records]
        self.assertEqual(len(record_ids), len(set(record_ids)))
        self.assertEqual(
            [record["review_status"] for record in records],
            ["pending", "pending", "pending"],
        )
        self.assertEqual([record["reviewed_at"] for record in records], [0, 0, 0])
        saved = plugin._kv[module.STATE_KEY]["violation_records"]
        self.assertEqual([record["record_id"] for record in saved], record_ids)

    async def test_identity_state_is_bounded_and_member_lookup_is_indexed(self):
        plugin, _ = self.plugin()
        previous_limits = (module.MAX_UID_BINDINGS, module.MAX_SUSPICIOUS_MEMBERS)
        module.MAX_UID_BINDINGS = 2
        module.MAX_SUSPICIOUS_MEMBERS = 2
        try:
            plugin._kv[module.STATE_KEY] = {
                "uid_bindings": {
                    "old": {
                        "uid": "old",
                        "groups": ["group-1"],
                        "member_openid": "member-old",
                        "bound_at": 1,
                    },
                    "new-1": {
                        "uid": "new-1",
                        "groups": ["group-1"],
                        "member_openid": "member-1",
                        "last_seen_at": 3,
                    },
                    "new-2": {
                        "uid": "new-2",
                        "members": {"group-1": "member-2"},
                        "last_seen_at": 2,
                    },
                },
                "suspicious_members": {
                    "old": {"created_at": 1},
                    "new-1": {"created_at": 3},
                    "new-2": {"created_at": 2},
                },
            }

            await plugin._load_state()

            self.assertEqual(set(plugin._uid_bindings), {"new-1", "new-2"})
            self.assertEqual(set(plugin._suspicious_members), {"new-1", "new-2"})
            self.assertEqual(plugin._uid_for_member("group-1", "member-1"), "new-1")
            self.assertEqual(plugin._uid_for_member("group-1", "member-2"), "new-2")
            plugin._uid_bindings["new-2"]["members"]["group-2"] = "member-2b"
            self.assertEqual(plugin._uid_for_member("group-2", "member-2b"), "new-2")
            await plugin.web_delete_binding("new-1")
            self.assertEqual(plugin._uid_for_member("group-1", "member-1"), "")
        finally:
            module.MAX_UID_BINDINGS, module.MAX_SUSPICIOUS_MEMBERS = previous_limits

    async def test_violation_review_updates_by_stable_id_and_filters(self):
        plugin, _ = self.plugin()
        await plugin._record_uid_violation(
            "",
            "group-1",
            "member-1",
            "规则一",
            content="内容一",
        )
        await plugin._record_uid_violation(
            "",
            "group-1",
            "member-2",
            "规则二",
            content="内容二",
        )
        first_id = plugin._violation_records[0]["record_id"]
        second_id = plugin._violation_records[1]["record_id"]

        updated = await plugin.web_review_violation(first_id, "confirmed")
        self.assertEqual(updated["record_id"], first_id)
        self.assertEqual(updated["review_status"], "confirmed")
        self.assertGreater(updated["reviewed_at"], 0)
        confirmed = await plugin.web_identity_page(
            "violations", "", 1, 10, "confirmed"
        )
        pending = await plugin.web_identity_page("violations", "", 1, 10, "pending")
        self.assertEqual([item["record_id"] for item in confirmed["items"]], [first_id])
        self.assertEqual([item["record_id"] for item in pending["items"]], [second_id])

        reset = await plugin.web_review_violation(first_id, "pending")
        self.assertEqual(reset["reviewed_at"], 0)
        with self.assertRaisesRegex(ValueError, "复核状态无效"):
            await plugin.web_review_violation(first_id, "unknown")
        with self.assertRaises(LookupError):
            await plugin.web_review_violation("missing-record", "confirmed")

    async def test_identity_records_are_filtered_and_paginated_server_side(self):
        plugin, _ = self.plugin()
        plugin.config["auto_review_groups"][0]["group_name"] = "测试审核群"
        plugin._uid_bindings = {
            str(uid): {
                "uid": str(uid),
                "username": f"成员{uid}",
                "union_openid": f"union-{uid}",
                "groups": ["group-1"],
                "bound_at": 1_700_000_000,
            }
            for uid in range(100, 112)
        }
        plugin._suspicious_members = {
            "group-1:member-9": {
                "group_openid": "group-1",
                "member_openid": "member-9",
                "username": "待验证成员",
                "reason": "身份冲突",
                "created_at": 1_700_000_001,
            }
        }
        plugin._violation_records = [
            {
                "uid": "777",
                "username": "违规成员",
                "group_openid": "group-1",
                "reason": "命中广告规则",
                "content": "违规消息原文",
                "created_at": 1_700_000_002,
                "ai_confidence": 97,
            }
        ]

        first = await plugin.web_identity_page("bindings", "测试审核群", 1, 10)
        second = await plugin.web_identity_page("bindings", "", 2, 10)
        by_name = await plugin.web_identity_page("bindings", "成员105", 1, 20)
        by_openid = await plugin.web_identity_page("bindings", "union-108", 1, 50)
        suspicious = await plugin.web_identity_page("suspicious", "身份冲突", 1, 10)
        violation = await plugin.web_identity_page("violations", "违规消息原文", 1, 10)
        exported = await plugin.web_violation_export("违规消息原文")
        hidden_time = await plugin.web_identity_page("bindings", "1700000000", 1, 10)
        hidden_confidence = await plugin.web_identity_page("violations", "97", 1, 10)

        self.assertEqual(first["total"], 12)
        self.assertEqual(first["total_pages"], 2)
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(second["items"]), 2)
        self.assertEqual(by_name["items"][0]["uid"], "105")
        self.assertEqual(by_openid["items"][0]["uid"], "108")
        self.assertEqual(suspicious["items"][0]["member_openid"], "member-9")
        self.assertEqual(violation["items"][0]["uid"], "777")
        self.assertEqual([item["uid"] for item in exported], ["777"])
        self.assertEqual(hidden_time["total"], 0)
        self.assertEqual(hidden_confidence["total"], 0)
        with self.assertRaisesRegex(ValueError, "分页参数"):
            await plugin.web_identity_page("bindings", "", 1, 100)

    async def test_identity_bindings_use_numeric_uid_order(self):
        plugin, _ = self.plugin()
        huge_uid = "9" * 5000
        plugin._uid_bindings = {
            uid: {"uid": uid, "groups": []}
            for uid in ("10", "2", huge_uid, "invalid")
        }

        page = await plugin.web_identity_page("bindings", "", 1, 10)

        self.assertEqual(
            [item["uid"] for item in page["items"]], ["2", "10", huge_uid, "invalid"]
        )

    async def test_identity_search_includes_action_member_openid(self):
        plugin, _ = self.plugin()
        plugin._violation_records = [
            {
                "record_id": "record-1",
                "created_at": 1,
                "action_member_openid": "target-openid",
                "content": "复读",
            }
        ]
        page = await plugin.web_identity_page(
            "violations", "target-openid", 1, 10
        )
        self.assertEqual(page["total"], 1)

    async def test_clear_suspicious_invalidates_verification_tokens(self):
        plugin, _ = self.plugin()
        plugin._suspicious_members["group-1:member-1"] = {
            "group_openid": "group-1",
            "member_openid": "member-1",
        }
        plugin._verification_tokens["stale"] = (
            9999999999.0,
            "group-1",
            "member-1",
            7,
        )
        await plugin.web_clear_suspicious("group-1", "member-1")
        self.assertNotIn("stale", plugin._verification_tokens)

    async def test_identity_page_ignores_malformed_timestamps(self):
        plugin, _ = self.plugin()
        plugin._violation_records = [
            {"created_at": "not-a-time", "content": "old"},
            {"created_at": 2, "content": "new"},
        ]

        page = await plugin.web_identity_page("violations", "", 1, 10)

        self.assertEqual([item["content"] for item in page["items"]], ["new", "old"])

    def test_identity_view_loads_on_demand_and_has_responsive_header(self):
        script = (ROOT / "pages/groups/app.js").read_text(encoding="utf-8")
        styles = (ROOT / "pages/groups/styles.css").read_text(encoding="utf-8")

        self.assertIn(
            'name === "identities" && element("identities-view").hidden', script
        )
        self.assertIn(
            'if (!element("identities-view").hidden) loadIdentities(refreshIdentities);',
            script,
        )
        self.assertIn(
            "if (identityLoadPromise && !force) return identityLoadPromise;", script
        )
        self.assertIn(
            "if (identitiesLoaded && !force) return Promise.resolve();", script
        )
        self.assertIn("if (requestId !== loadGeneration) return;", script)
        self.assertIn("loadIdentities(true)", script)
        self.assertIn("appendUnboundGroupChoices", script)
        self.assertIn("if (!all && !selectedGroups.length) throw", script)
        self.assertIn(".sub-list-header { display: flex;", styles)
        self.assertIn(
            ".sub-list-header { align-items: stretch; flex-direction: column;",
            styles,
        )
        self.assertIn(".identity-list > .list-header { flex-wrap: wrap; }", styles)
        self.assertIn(
            ".identity-list .identity-tools .search { flex: 1 1 240px;",
            styles,
        )

    async def test_identity_routes_read_validated_query_parameters(self):
        class Query(dict):
            def get(self, key, default=None, type=None):
                value = super().get(key, default)
                return type(value) if type is not None else value

        plugin, _ = self.plugin()
        plugin.web_identity_page = AsyncMock(return_value={"items": [], "total": 0})
        web = module.GroupAdminWeb(plugin, plugin.context)
        web_module = sys.modules[module.GroupAdminWeb.__module__]
        query = Query(
            kind="violations",
            query="测试群",
            page="2",
            page_size="20",
            review_status="confirmed",
        )

        with patch.object(web_module.request, "query", query, create=True):
            response = await web.page_identities()

        self.assertEqual(response["data"]["total"], 0)
        plugin.web_identity_page.assert_awaited_once_with(
            "violations", "测试群", 2, 20, "confirmed"
        )

        query["page"] = "abc"
        with (
            patch.object(web_module.request, "query", query, create=True),
            self.assertRaisesRegex(ValueError, "必须是整数"),
        ):
            await web.page_identities()

        with (
            patch.object(web_module.request, "query", Query(), create=True),
            self.assertRaisesRegex(ValueError, "不能为空"),
        ):
            await web.page_identities()

        plugin.web_violation_export = AsyncMock(
            return_value=[
                {
                    "username": "=2+2",
                    "content": "\t=HYPERLINK(\"https://example.invalid\")",
                    "action": "record_only",
                    "ai_confirm_provider": "confirm",
                    "ai_confirm_decision": "ERROR",
                    "ai_confirm_reason": "timeout",
                }
            ]
        )
        query = Query(query="违规", review_status="false_positive")
        with patch.object(web_module.request, "query", query, create=True):
            export_response = await web.page_violation_export()

        export_data = export_response["data"]
        self.assertEqual(export_data["count"], 1)
        self.assertTrue(export_data["content"].startswith("\ufeff时间,"))
        self.assertIn("'=2+2", export_data["content"])
        self.assertIn("'\t=HYPERLINK", export_data["content"])
        self.assertIn("确认模型", export_data["content"])
        self.assertIn("confirm", export_data["content"])
        self.assertIn("timeout", export_data["content"])
        plugin.web_violation_export.assert_awaited_once_with("违规", "false_positive")

        html = (ROOT / "pages/groups/index.html").read_text(encoding="utf-8")
        script = (ROOT / "pages/groups/app.js").read_text(encoding="utf-8")
        self.assertIn('id="violation-status-filter"', html)
        self.assertIn('apiPost("violation-review"', script)
        self.assertIn("review_status", script)

    async def test_recall_recent_messages_uses_received_message_cache(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "/撤回 3")
        now = module.time.monotonic()
        plugin._moderation.record_message(
            "group-1", "member-1", "old-1", "member", now=now
        )
        plugin._moderation.record_message(
            "group-1", "member-2", "old-2", "member", now=now
        )
        api = SimpleNamespace(recall_group_message=AsyncMock())
        plugin._api = lambda _event: api

        with patch.object(module.asyncio, "sleep", AsyncMock()):
            results = [
                result
                async for result in plugin.recall_recent_messages(event, "3")
            ]

        self.assertEqual(
            [call.args[1] for call in api.recall_group_message.await_args_list],
            ["old-2", "old-1"],
        )
        self.assertIn("已撤回本群最近 2 条", results[0])
        self.assertIn("缓存不足 1 条", results[0])

    async def test_whole_mute_commands_report_official_api_limit(self):
        plugin, client = self.plugin()
        event = FakeEvent(client, "/全体禁言")
        api = SimpleNamespace(
            get_mute_state=AsyncMock(return_value={"global_rule": {"mode": "none"}})
        )
        plugin._api = lambda _event: api

        results = [result async for result in plugin.mute_all(event)]

        self.assertIn("未执行", results[0])
        self.assertIn("没有写入全体禁言或解禁的接口", results[0])
        self.assertIn("none", results[0])

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

    async def test_welcome_rules_filter_groups_render_variables_and_deduplicate(self):
        plugin, client = self.plugin()
        plugin.config["auto_review_groups"][0]["group_name"] = "测试群"
        plugin.config["welcome_rules"] = [
            {
                "name": "指定群欢迎",
                "message": "{at_user} 欢迎 {username} 加入 {group_name}，UID={uid}",
                "group_openids": ["group-1"],
                "enabled": True,
            },
            {
                "name": "其他群",
                "message": "不应发送",
                "group_openids": ["group-2"],
                "enabled": True,
            },
        ]
        plugin._uid_bindings["188144093"] = {
            "members": {"group-1": "member-1"},
            "groups": ["group-1"],
        }

        await plugin._send_welcome_messages(
            client, "group-1", "member-1", username="新人"
        )
        await plugin._send_welcome_messages(
            client, "group-1", "member-1", username="新人"
        )

        self.assertEqual(len(client.api.messages), 1)
        message = client.api.messages[0]
        self.assertEqual(message["msg_type"], 2)
        self.assertIn("欢迎 新人 加入 测试群", message["markdown"]["content"])
        self.assertIn("UID=188144093", message["markdown"]["content"])
        self.assertIn('qqbot-at-user id="member-1"', message["markdown"]["content"])

    async def test_button_approval_sends_welcome_with_request_context(self):
        plugin, client = self.plugin()
        plugin.config["welcome_rules"] = [
            {
                "name": "审批欢迎",
                "message": "欢迎 {username}",
                "group_openids": ["group-1"],
                "enabled": True,
            }
        ]
        token = plugin._approval_token(
            "group-1",
            "member-1",
            "request-1",
            request={"username": "申请人"},
        )
        plugin._approve_request = AsyncMock()
        interaction = SimpleNamespace(
            id="interaction-welcome",
            type=11,
            chat_type=1,
            group_openid="group-1",
            group_member_openid="admin-1",
            data=SimpleNamespace(
                resolved=SimpleNamespace(button_data=f"qqga:{token}:approve")
            ),
        )

        self.assertTrue(await plugin._handle_interaction(client, interaction))

        self.assertIn("欢迎 申请人", client.api.messages[-1]["content"])

    async def test_automatic_approval_sends_welcome(self):
        plugin, client = self.plugin()
        plugin.config["welcome_rules"] = [
            {
                "name": "自动审批欢迎",
                "message": "欢迎 {username}",
                "group_openids": ["group-1"],
                "enabled": True,
            }
        ]
        plugin._approve_request = AsyncMock()
        plugin._send_welcome_messages = AsyncMock()
        settings = {
            "global_reject_keywords": [],
            "reject_keywords": [],
            "uid_exists_auto_approve": True,
            "condition_logic": "all",
            "approve_keywords": [],
            "uid_check_enabled": True,
            "fallback_action": "pending",
            "fallback_human_verify_enabled": False,
        }
        request = {
            "username": "自动用户",
            "member_openid": "member-1",
            "join_request_id": "request-1",
            "apply_source": "self_apply",
            "verify_info": {"verify_message": "UID:188144093"},
        }
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={"list": [request], "next_cursor": ""}
            )
        )
        with (
            patch.object(module, "QQGroupAPI", return_value=api),
            patch.object(module, "bilibili_uid_exists", AsyncMock(return_value=True)),
        ):
            await plugin._poll_uid_group(
                client,
                "platform-1",
                "group-1",
                settings,
            )

        plugin._approve_request.assert_awaited_once()
        self.assertEqual(plugin._approve_request.await_args.kwargs["op"], "approve")
        plugin._send_welcome_messages.assert_awaited_once()
        self.assertEqual(
            plugin._send_welcome_messages.await_args.kwargs["request"]["username"],
            "自动用户",
        )

    async def test_verification_falls_back_to_text_and_accepts_numeric_answer(self):
        plugin, client = self.plugin()
        plugin._suspicious_members["group-1:member-1"] = {
            "group_openid": "group-1",
            "member_openid": "member-1",
        }
        original = client.api.post_group_message

        async def reject_markdown(**kwargs):
            if kwargs.get("msg_type") == 2 and kwargs.get("keyboard"):
                raise RuntimeError("no keyboard permission")
            return await original(**kwargs)

        client.api.post_group_message = reject_markdown
        with patch.object(module.secrets, "randbelow", side_effect=[1, 2]):
            await plugin._send_verification_challenge(
                client, "group-1", "member-1"
            )

        token_data = next(iter(plugin._verification_tokens.values()))
        self.assertIn("真人验证", client.api.messages[-1]["content"])
        self.assertIn("请点击正确答案完成验证：3 + 4 = ?", client.api.messages[-1]["content"])
        self.assertIn("未完成验证前发送的消息会被撤回", client.api.messages[-1]["content"])
        self.assertTrue(
            await plugin._consume_verification_answer(
                client,
                "group-1",
                "member-1",
                str(token_data[3]),
            )
        )
        self.assertNotIn("group-1:member-1", plugin._suspicious_members)
        self.assertEqual(plugin._verification_tokens, {})

    async def test_verification_rejects_prefixed_answer(self):
        plugin, client = self.plugin()
        plugin._suspicious_members["group-1:member-1"] = {
            "group_openid": "group-1",
            "member_openid": "member-1",
        }
        with patch.object(module.secrets, "randbelow", side_effect=[1, 2]):
            await plugin._send_verification_challenge(
                client, "group-1", "member-1"
            )
        answer = next(iter(plugin._verification_tokens.values()))[3]
        self.assertFalse(
            await plugin._consume_verification_answer(
                client, "group-1", "member-1", f"答案:{answer}"
            )
        )


if __name__ == "__main__":
    unittest.main()
