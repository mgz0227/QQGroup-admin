import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bilibili
from bilibili import (
    BilibiliConfigError,
    fetch_space_dynamics,
    live_transition,
    parse_dynamic_items,
    poll_qr_login,
    sign_wbi,
    start_qr_login,
)


class BilibiliTest(unittest.TestCase):
    def test_wbi_signature_matches_documented_example(self):
        signed = sign_wbi(
            {"foo": "114", "bar": "514", "zab": 1919810},
            "7cd084941338484aae1ad9425b84077c",
            "4932caff0ff746eab6f01bf08b70ac45",
            timestamp=1702204169,
        )
        self.assertEqual(signed["wts"], 1702204169)
        self.assertEqual(signed["w_rid"], "8f6f2b5b3d485fe1886cec6a0be8c5d4")
        with self.assertRaises(BilibiliConfigError):
            fetch_space_dynamics("188144093", "")

    def test_dynamic_parser_returns_stable_fields(self):
        payload = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "id_str": "123",
                        "type": "DYNAMIC_TYPE_AV",
                        "visible": True,
                        "basic": {"jump_url": "//www.bilibili.com/opus/123"},
                        "modules": {
                            "module_author": {
                                "mid": 188144093,
                                "name": "喵公子啦",
                                "pub_ts": 100,
                            },
                            "module_dynamic": {
                                "desc": {"text": "新动态"},
                                "major": {
                                    "archive": {
                                        "title": "新视频",
                                        "cover": "//i0.hdslb.com/bfs/archive/cover.jpg",
                                        "jump_url": "//www.bilibili.com/video/BV1xx",
                                    }
                                },
                            },
                        },
                    },
                    {"id_str": "hidden", "visible": False},
                ]
            },
        }
        self.assertEqual(
            parse_dynamic_items(payload),
            [
                {
                    "id": "123",
                    "type": "DYNAMIC_TYPE_AV",
                    "uid": "188144093",
                    "author": "喵公子啦",
                    "pub_ts": 100,
                    "title": "新视频",
                    "text": "新动态",
                    "url": "https://www.bilibili.com/opus/123",
                    "cover": "https://i0.hdslb.com/bfs/archive/cover.jpg",
                }
            ],
        )

    def test_dynamic_parser_cleans_placeholders_and_reads_nested_cover(self):
        payload = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "id_str": "draw-1",
                        "type": "DYNAMIC_TYPE_DRAW",
                        "modules": {
                            "module_author": {"name": "UP"},
                            "module_dynamic": {
                                "desc": {"text": "-"},
                                "major": {
                                    "draw": {
                                        "items": [
                                            {
                                                "src": "//i0.hdslb.com/bfs/draw.jpg",
                                                "width": 1320,
                                                "height": 2468,
                                            }
                                        ]
                                    }
                                },
                            },
                        },
                    }
                ]
            }
        }

        self.assertEqual(
            parse_dynamic_items(payload)[0]["cover"],
            "https://i0.hdslb.com/bfs/draw.jpg",
        )
        self.assertEqual(parse_dynamic_items(payload)[0]["cover_width"], 1320)
        self.assertEqual(parse_dynamic_items(payload)[0]["cover_height"], 2468)
        self.assertEqual(parse_dynamic_items(payload)[0]["title"], "")
        self.assertEqual(parse_dynamic_items(payload)[0]["text"], "")

    def test_video_parser_prefers_video_description_over_generic_dynamic_text(self):
        payload = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "id_str": "video-1",
                        "type": "DYNAMIC_TYPE_AV",
                        "modules": {
                            "module_author": {"name": "UP"},
                            "module_dynamic": {
                                "desc": {"text": "发布了新动态"},
                                "major": {
                                    "archive": {
                                        "title": "视频标题",
                                        "desc": "这是视频简介",
                                    }
                                },
                            },
                        },
                    }
                ]
            },
        }

        item = parse_dynamic_items(payload)[0]
        self.assertEqual(item["title"], "视频标题")
        self.assertEqual(item["text"], "这是视频简介")

    def test_dynamic_parser_uses_rich_text_nodes_when_plain_text_is_empty(self):
        payload = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "id_str": "rich-1",
                        "type": "DYNAMIC_TYPE_DRAW",
                        "modules": {
                            "module_dynamic": {
                                "desc": {
                                    "text": "",
                                    "rich_text_nodes": [
                                        {"text": "第一段"},
                                        {"text": " 第二段"},
                                    ],
                                }
                            }
                        },
                    }
                ]
            },
        }
        self.assertEqual(parse_dynamic_items(payload)[0]["text"], "第一段 第二段")

    def test_live_transition_seeds_and_detects_changes(self):
        offline = {"live_status": 0, "live_time": 0}
        live = {"live_status": 1, "live_time": 100}
        restarted = {"live_status": 1, "live_time": 200}
        self.assertIsNone(live_transition(None, live))
        self.assertEqual(live_transition(offline, live), "start")
        self.assertEqual(live_transition(live, offline), "stop")
        self.assertEqual(live_transition(live, restarted), "start")
        self.assertIsNone(live_transition(live, dict(live)))

    def test_qr_login_tracks_status_and_collects_cookie(self):
        key = "a" * 32
        with patch.object(
            bilibili,
            "_qr_json",
            side_effect=[
                {
                    "url": f"https://passport.bilibili.com/scan?key={key}",
                    "qrcode_key": key,
                },
                {"code": 86101},
                {"code": 86090},
                {"code": 0},
            ],
        ):
            login = start_qr_login()
            self.assertEqual(poll_qr_login(login), ("waiting", ""))
            self.assertEqual(poll_qr_login(login), ("scanned", ""))
            login.cookies = [
                SimpleNamespace(name="SESSDATA", value="session"),
                SimpleNamespace(name="bili_jct", value="csrf"),
                SimpleNamespace(name="ignored", value="secret"),
            ]
            status, cookie = poll_qr_login(login)
        self.assertEqual(status, "confirmed")
        self.assertEqual(cookie, "SESSDATA=session; bili_jct=csrf")

        login.expires_at = 0
        self.assertEqual(poll_qr_login(login), ("expired", ""))


if __name__ == "__main__":
    unittest.main()
