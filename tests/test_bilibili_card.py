import unittest
from io import BytesIO
from unittest.mock import patch

from bilibili_card import _image_url, build_bilibili_card, render_bilibili_card


class BilibiliCardTest(unittest.TestCase):
    def test_card_has_compact_sections_and_escapes_remote_content(self):
        html = build_bilibili_card(
            author="UP <script>",
            kind="图文",
            timestamp="08-24 12:12",
            title="标题 & 片段",
            summary="正文 <b>不要执行</b>",
            cover="https://i0.hdslb.com/bfs/archive/cover.jpg?a=1&b=2",
            avatar="https://i1.hdslb.com/bfs/face/avatar.jpg",
            link="https://www.bilibili.com/opus/1?a=1&b=2",
        )

        self.assertIn("B站动态", html)
        self.assertIn("查看原动态", html)
        self.assertIn("UP &lt;script&gt;", html)
        self.assertIn("正文 &lt;b&gt;不要执行&lt;/b&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("cover.jpg?a=1&amp;b=2", html)

    def test_card_rejects_non_bilibili_remote_images(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            cover="https://internal.example.test/cover.jpg",
            avatar="https://internal.example.test/avatar.jpg",
        )

        self.assertNotIn("internal.example.test", html)
        self.assertEqual(_image_url("https://internal.example.test/a.jpg"), "")
        self.assertTrue(_image_url("https://i0.hdslb.com/bfs/a.jpg"))

    def test_empty_content_does_not_leave_empty_blocks(self):
        html = build_bilibili_card(
            author="UP",
            kind="文字",
            link="https://www.bilibili.com/opus/1",
        )

        self.assertNotIn('class="title"></div>', html)
        self.assertNotIn('class="summary"></div>', html)
        self.assertNotIn('class="cover-wrap">', html)
        self.assertNotIn('class="status">', html)

    def test_card_uses_type_specific_brand_and_link_label(self):
        video = build_bilibili_card(
            author="UP",
            kind="视频",
            title="视频标题",
            summary="视频简介",
            link="https://www.bilibili.com/video/BV1xx",
        )
        live = build_bilibili_card(
            author="UP",
            kind="直播",
            title="直播标题",
            link="https://live.bilibili.com/123",
        )

        self.assertIn("B站视频", video)
        self.assertIn("查看视频", video)
        self.assertIn("视频简介", video)
        self.assertIn("B站直播", live)
        self.assertIn("进入直播间", live)

    def test_local_card_renders_useful_empty_dynamic(self):
        with patch("bilibili_card._download_image", return_value=None):
            image = render_bilibili_card(
                {
                    "author": "喵公子啦",
                    "kind": "图文",
                    "timestamp": "08-24 12:12",
                    "link": "https://www.bilibili.com/opus/1",
                }
            )

        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(image), 8 * 1024 * 1024)

    def test_local_card_expands_portrait_cover_without_cropping(self):
        from PIL import Image

        portrait = Image.new("RGB", (1320, 2468), "#734820")
        with patch("bilibili_card._download_image", return_value=portrait):
            image = render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "cover": "https://i0.hdslb.com/bfs/new_dyn/post.png",
                    "link": "https://www.bilibili.com/opus/1",
                }
            )

        rendered = Image.open(BytesIO(image))
        self.assertGreater(rendered.height, 700)
        self.assertLess(rendered.height, 1_000)

    def test_local_card_uses_custom_link_label_and_clips_author(self):
        from PIL import ImageDraw

        calls = []
        original_text = ImageDraw.ImageDraw.text

        def capture_text(draw, xy, text, *args, **kwargs):
            calls.append(str(text))
            return original_text(draw, xy, text, *args, **kwargs)

        with (
            patch("bilibili_card._download_image", return_value=None),
            patch.object(ImageDraw.ImageDraw, "text", capture_text),
        ):
            render_bilibili_card(
                {
                    "author": "超长作者名称-" * 20,
                    "kind": "直播",
                    "status": "直播结束",
                    "link": "https://live.bilibili.com/123",
                    "link_label": "查看直播间",
                }
            )

        self.assertIn("查看直播间  →", calls)
        self.assertTrue(any(value.endswith("…") for value in calls))


if __name__ == "__main__":
    unittest.main()
