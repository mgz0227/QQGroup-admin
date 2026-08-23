import unittest

from moderation import ModerationWindows, normalize_message


class ModerationWindowsTest(unittest.TestCase):
    def test_recent_messages_are_newest_first_and_filter_protected_members(self):
        state = ModerationWindows()
        state.record_message("g", "u1", "m1", "member", now=1)
        state.record_message("g", "u2", "m2", "admin", now=2)
        state.record_message("g", "u1", "m3", "member", now=3)
        state.record_message("g", "u3", "m4", "owner", now=4)
        state.record_message("g", "u2", "command", "member", now=5)

        self.assertEqual(
            state.newest_message_ids("g", exclude_message_id="command", now=5),
            ["m3", "m1"],
        )
        self.assertEqual(
            state.newest_message_ids("g", member_openid="u1", now=5),
            ["m3", "m1"],
        )

    def test_recent_messages_expire_and_are_bounded_per_group(self):
        state = ModerationWindows()
        state.record_message("old", "u", "expired", "member", now=0)
        for index in range(205):
            state.record_message("g", "u", f"m{index}", "member", now=100)

        self.assertEqual(state.newest_message_ids("old", now=121), [])
        ids = state.newest_message_ids("g", limit=500, now=100)
        self.assertEqual(len(ids), 50)
        self.assertEqual(ids[0], "m204")
        self.assertEqual(ids[-1], "m155")
        self.assertEqual(len(state.recent_messages["g"]), 200)

    def test_group_image_chain_spans_multiple_members(self):
        state = ModerationWindows()
        self.assertEqual(
            state.add_group_images(
                "g", "u1", "m1", 2, threshold=4, min_members=2, window=10, now=1
            ),
            [],
        )
        self.assertEqual(
            state.add_group_images(
                "g", "u2", "m2", 1, threshold=4, min_members=2, window=10, now=2
            ),
            [],
        )
        self.assertEqual(
            state.add_group_images(
                "g", "u1", "m3", 1, threshold=4, min_members=2, window=10, now=3
            ),
            ["m1", "m2", "m3"],
        )

    def test_non_image_breaks_only_the_group_image_chain(self):
        state = ModerationWindows()
        state.add_group_images(
            "g", "u1", "m1", 1, threshold=3, min_members=2, window=10, now=1
        )
        state.add_images("g", "u1", "m1", 1, threshold=2, window=10, now=1)

        state.add_group_images(
            "g", "u2", "text", 0, threshold=3, min_members=2, window=10, now=2
        )

        self.assertEqual(
            state.add_group_images(
                "g", "u2", "m2", 1, threshold=3, min_members=2, window=10, now=3
            ),
            [],
        )
        self.assertEqual(
            state.add_images("g", "u1", "m3", 1, threshold=2, window=10, now=3),
            ["m1", "m3"],
        )

    def test_image_repeat_and_duplicate_windows(self):
        state = ModerationWindows()
        self.assertEqual(normalize_message("  复读\n内容 "), "复读 内容")
        self.assertEqual(
            state.add_images("g", "u", "m1", 2, threshold=3, window=10, now=1),
            [],
        )
        self.assertEqual(
            state.add_images("g", "u", "m2", 1, threshold=3, window=10, now=2),
            ["m1", "m2"],
        )
        self.assertEqual(
            state.add_repeat(
                "g", "x", "u1", "member", "m1", threshold=3, window=10, now=1
            ),
            [],
        )
        self.assertEqual(
            state.add_repeat(
                "g", "x", "u2", "member", "m2", threshold=3, window=10, now=2
            ),
            [],
        )
        self.assertEqual(
            state.add_repeat(
                "g", "x", "u1", "member", "m3", threshold=3, window=10, now=3
            ),
            ["u1", "u2"],
        )
        key = ("p", "g", "m", "1")
        self.assertIsNone(state.duplicate(key, now=1))
        state.remember(key, True, now=1)
        self.assertTrue(state.duplicate(key, now=2))

        state.repeats.update(
            {("g", str(index)): [(1, "u", "member", "m")] for index in range(2000)}
        )
        state.add_repeat("g", "new", "u", "member", "m", threshold=3, window=10, now=2)
        self.assertLessEqual(len(state.repeats), 2000)

    def test_image_recall_limit_keeps_recent_messages_only(self):
        state = ModerationWindows()
        for index in range(1, 4):
            self.assertEqual(
                state.add_images(
                    "g",
                    "u",
                    f"m{index}",
                    1,
                    threshold=4,
                    window=10,
                    recall_limit=2,
                    now=index,
                ),
                [],
            )
        self.assertEqual(
            state.add_images(
                "g", "u", "m4", 1, threshold=4, window=10, recall_limit=2, now=4
            ),
            ["m3", "m4"],
        )
        self.assertEqual(
            state.add_group_images(
                "g",
                "u1",
                "m2",
                1,
                threshold=2,
                min_members=2,
                window=10,
                recall_limit=1,
                now=2,
            ),
            [],
        )
        self.assertEqual(
            state.add_group_images(
                "g",
                "u2",
                "m3",
                1,
                threshold=2,
                min_members=2,
                window=10,
                recall_limit=1,
                now=3,
            ),
            ["m3"],
        )


if __name__ == "__main__":
    unittest.main()
