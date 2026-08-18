import unittest

from qq_api import (
    QQAPIError,
    QQGroupAPI,
    future_rfc3339,
    parse_duration,
    parse_group_ids,
    parse_qq_number_text,
    parse_qq_numbers,
    select_group_strategy,
    validate_rfc3339,
    whitelist_diff,
)


class FakeResponse:
    def __init__(self, status, data=None, headers=None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def text(self):
        import json

        return "" if self.data is None else json.dumps(self.data, ensure_ascii=False)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class FakeHTTP:
    def __init__(self, response):
        self._session = FakeSession(response)
        self._headers = {"Authorization": "QQBot test-token"}
        self.timeout = 5

    async def check_session(self):
        return None


class FakeClient:
    def __init__(self, response):
        self.http = FakeHTTP(response)


class QQGroupAPITest(unittest.IsolatedAsyncioTestCase):
    async def test_all_twelve_management_routes(self):
        client = FakeClient(FakeResponse(200, {}))
        api = QQGroupAPI(client)

        await api.get_group_info("group")
        await api.get_bot_state("group")
        await api.list_join_requests("group")
        await api.approve_join_request(
            "group",
            "member",
            op="approve",
            join_request_id="request",
        )
        await api.get_mute_state("group")
        await api.set_member_mutes(
            "group",
            [
                {
                    "op": "add",
                    "member_openid": "member",
                    "mute_expire_at": future_rfc3339(parse_duration("1d")),
                }
            ],
        )
        await api.list_strategies()
        await api.create_strategy(group_openids=["group"], remark="自动 审批")
        await api.update_strategy("strategy", {"is_enable": "off"})
        await api.delete_strategy("strategy")
        await api.update_whitelist("strategy", op="add", users=["123"])
        await api.execute_strategy("strategy")

        base = "https://api.bot.qq.com"
        self.assertEqual(
            [(method, url) for method, url, _ in client.http._session.calls],
            [
                ("GET", f"{base}/v2/groups/group/info"),
                ("GET", f"{base}/v2/groups/group/bot_state"),
                ("GET", f"{base}/v2/groups/group/join_request_list"),
                (
                    "POST",
                    f"{base}/v2/groups/group/approval_join_request/member",
                ),
                ("GET", f"{base}/v2/groups/group/restrict_chat_setting"),
                ("POST", f"{base}/v2/groups/group/restrict_chat_setting"),
                ("GET", f"{base}/v2/groups/join_approval_strategy"),
                ("POST", f"{base}/v2/groups/join_approval_strategy"),
                ("PATCH", f"{base}/v2/groups/join_approval_strategy/strategy"),
                ("DELETE", f"{base}/v2/groups/join_approval_strategy/strategy"),
                (
                    "POST",
                    f"{base}/v2/groups/join_approval_strategy/strategy/whitelist_users",
                ),
                (
                    "POST",
                    f"{base}/v2/groups/join_approval_strategy/strategy/execute",
                ),
            ],
        )
        self.assertEqual(
            client.http._session.calls[7][2]["json"]["remark"],
            "自动 审批",
        )
        with self.assertRaises(ValueError):
            await api.create_strategy(
                group_openids=["group"],
                remark="x" * 256,
            )

    async def test_paginated_get_uses_documented_json_body(self):
        client = FakeClient(FakeResponse(200, {"list": [], "next_cursor": ""}))

        result = await QQGroupAPI(client).list_join_requests(
            "group/openid",
            limit=100,
            cursor="next",
        )

        self.assertEqual(result["list"], [])
        method, url, kwargs = client.http._session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "https://api.bot.qq.com/v2/groups/group%2Fopenid/join_request_list",
        )
        self.assertEqual(kwargs["json"], {"limit": 100, "cursor": "next"})

    async def test_recall_group_message(self):
        client = FakeClient(FakeResponse(200))

        await QQGroupAPI(client).recall_group_message("group/openid", "message/id")

        method, url, kwargs = client.http._session.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(
            url,
            "https://api.bot.qq.com/v2/groups/group%2Fopenid/messages/message%2Fid",
        )
        self.assertNotIn("json", kwargs)

    async def test_error_keeps_code_and_trace_id(self):
        client = FakeClient(
            FakeResponse(
                403,
                {"err_code": 11253, "message": "forbidden"},
                {"X-Tps-trace-ID": "trace-123"},
            )
        )

        with self.assertRaises(QQAPIError) as caught:
            await QQGroupAPI(client).get_group_info("group")

        self.assertIn("11253", str(caught.exception))
        self.assertIn("trace-123", str(caught.exception))

    async def test_nonzero_error_code_fails_on_async_http_status(self):
        client = FakeClient(
            FakeResponse(202, {"err_code": 11253, "message": "forbidden"})
        )

        with self.assertRaises(QQAPIError):
            await QQGroupAPI(client).get_group_info("group")

    async def test_empty_204_is_success(self):
        client = FakeClient(FakeResponse(204))

        result = await QQGroupAPI(client).delete_strategy("strategy")

        self.assertIsNone(result)

    def test_input_limits(self):
        self.assertEqual(parse_duration("2h").total_seconds(), 7200)
        self.assertEqual(parse_duration("45").total_seconds(), 45)
        self.assertEqual(parse_group_ids("123,456"), [123, 456])
        self.assertEqual(parse_qq_numbers("123,123,456"), ["123", "456"])
        with self.assertRaises(ValueError):
            parse_duration("31d")
        with self.assertRaises(ValueError):
            parse_group_ids("not-a-group")
        self.assertEqual(
            parse_qq_number_text("123\n456，789;123, "),
            ["123", "456", "789"],
        )
        self.assertEqual(
            whitelist_diff(["123", "789"], ["123", "456"]),
            (["789"], ["456"]),
        )

    def test_rfc3339_validation_is_strict(self):
        self.assertEqual(
            validate_rfc3339("2026-08-18T12:34:56+08:00"),
            "2026-08-18T12:34:56+08:00",
        )
        for invalid in (
            "2026-W34-2T12:34:56Z",
            "2026-08-18 12:34:56Z",
            "2026-08-18T12:34:56+08",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_rfc3339(invalid)

    def test_select_group_strategy_is_scoped_to_one_group(self):
        strategy = {
            "strategy_id": "strategy",
            "group_openids": ["group"],
        }
        self.assertIs(select_group_strategy([strategy], "group"), strategy)
        self.assertIsNone(select_group_strategy([strategy], "other"))

        with self.assertRaises(ValueError):
            select_group_strategy(
                [{**strategy, "group_openids": ["group", "other"]}],
                "group",
            )
        with self.assertRaises(ValueError):
            select_group_strategy([strategy, strategy.copy()], "group")
        with self.assertRaises(ValueError):
            select_group_strategy(
                [{"strategy_id": "numeric", "group_ids": [123]}],
                "group",
            )


if __name__ == "__main__":
    unittest.main()
