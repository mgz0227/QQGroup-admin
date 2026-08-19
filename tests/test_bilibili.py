import unittest

from bilibili import (
    BilibiliConfigError,
    fetch_space_dynamics,
    live_transition,
    parse_dynamic_items,
    sign_wbi,
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
                }
            ],
        )

    def test_live_transition_seeds_and_detects_changes(self):
        offline = {"live_status": 0, "live_time": 0}
        live = {"live_status": 1, "live_time": 100}
        restarted = {"live_status": 1, "live_time": 200}
        self.assertIsNone(live_transition(None, live))
        self.assertEqual(live_transition(offline, live), "start")
        self.assertEqual(live_transition(live, offline), "stop")
        self.assertEqual(live_transition(live, restarted), "start")
        self.assertIsNone(live_transition(live, dict(live)))


if __name__ == "__main__":
    unittest.main()
