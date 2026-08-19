import unittest

from moderation import ModerationWindows, normalize_message


class ModerationWindowsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
