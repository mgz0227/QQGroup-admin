import unittest

from review import (
    BilibiliLookupError,
    matched_keyword,
    parse_bilibili_response,
    parse_bilibili_uid,
    parse_keywords,
    parse_request_bilibili_uid,
    verification_text,
)


class ReviewRulesTest(unittest.TestCase):
    def test_verification_text_supports_message_and_answers(self):
        self.assertEqual(
            verification_text({"verify_info": {"verify_message": "UID:188144093"}}),
            "UID:188144093",
        )
        self.assertEqual(
            verification_text(
                {
                    "verify_info": {
                        "review_qa_list": [
                            {"question": "UID", "answer": "188144093"},
                            {"question": "来源", "answer": "主页"},
                        ]
                    }
                }
            ),
            "188144093\n主页",
        )

    def test_uid_parser_accepts_labeled_or_pure_uid(self):
        self.assertEqual(parse_bilibili_uid("UID：188144093"), "188144093")
        self.assertEqual(
            parse_bilibili_uid("我的 B站 uid: 188144093，谢谢"),
            "188144093",
        )
        self.assertEqual(parse_bilibili_uid("188144093"), "188144093")
        self.assertEqual(parse_bilibili_uid("bili 188144093"), "188144093")
        self.assertIsNone(parse_bilibili_uid("主页有 188144093 个赞"))
        self.assertIsNone(parse_bilibili_uid("UID: 0"))
        self.assertEqual(
            parse_request_bilibili_uid(
                {
                    "verify_info": {
                        "review_qa_list": [
                            {"answer": "188144093"},
                            {"answer": "从主页看到的"},
                        ]
                    }
                }
            ),
            "188144093",
        )

    def test_keywords_are_bounded_and_case_insensitive(self):
        keywords = parse_keywords("广告，spam\n拉人;广告")
        self.assertEqual(keywords, ["广告", "spam", "拉人"])
        self.assertEqual(matched_keyword("This is SPAM", keywords), "spam")
        with self.assertRaises(ValueError):
            parse_keywords("x" * 65)

    def test_bilibili_response_distinguishes_invalid_and_transient(self):
        self.assertTrue(
            parse_bilibili_response(
                "188144093",
                {"code": 0, "data": {"card": {"mid": "188144093"}}},
            )
        )
        self.assertFalse(parse_bilibili_response("1", {"code": -404}))
        with self.assertRaises(BilibiliLookupError):
            parse_bilibili_response("1", {"code": -799, "message": "频繁"})
        with self.assertRaises(BilibiliLookupError):
            parse_bilibili_response("1", {"code": 0, "data": []})


if __name__ == "__main__":
    unittest.main()
