import unittest
from io import BytesIO
from unittest.mock import patch

from bilibili_card import (
    BilibiliCoverUnavailable,
    _download_image,
    _image_url,
    _image_url_candidates,
    build_bilibili_card,
    download_bilibili_image,
    render_bilibili_card,
    split_bilibili_poster,
)


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

    def test_download_bilibili_image_rejects_non_bilibili_url(self):
        self.assertIsNone(download_bilibili_image("https://example.test/a.jpg"))

    def test_image_url_candidates_normalize_cdn_and_restore_original_name(self):
        self.assertEqual(
            _image_url_candidates(
                "http://i0.hdslb.com/bfs/new_dyn/post@672w_1c.webp?sign=ok#fragment"
            ),
            [
                "https://i0.hdslb.com/bfs/new_dyn/post@672w_1c.webp?sign=ok",
                "https://i0.hdslb.com/bfs/new_dyn/post.webp?sign=ok",
            ],
        )
        self.assertEqual(
            _image_url_candidates(
                "https://i0.hdslb.com/bfs/new_dyn/post.png@672w_1c.webp"
            )[-1],
            "https://i0.hdslb.com/bfs/new_dyn/post.png",
        )
        self.assertEqual(
            _image_url("https://user:pass@i0.hdslb.com/bfs/post.jpg"),
            "",
        )

    def test_download_bilibili_image_transcodes_and_bounds_remote_poster(self):
        from PIL import Image

        source = BytesIO()
        Image.new("RGB", (2200, 3200), "#734820").save(source, format="PNG")

        class Response:
            headers = {"Content-Length": str(len(source.getvalue()))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                self.limit = limit
                return source.getvalue()

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch("bilibili_card.build_opener", return_value=opener):
            encoded = download_bilibili_image(
                "https://i0.hdslb.com/bfs/draw-original.png"
            )

        self.assertIsNotNone(encoded)
        rendered = Image.open(BytesIO(encoded))
        self.assertEqual(rendered.format, "JPEG")
        self.assertLessEqual(max(rendered.size), 2400)
        self.assertLessEqual(len(encoded), 8 * 1024 * 1024)
        self.assertEqual(opener.timeout, 4)

    def test_card_download_prefers_suffix_free_source(self):
        from PIL import Image

        source = BytesIO()
        Image.new("RGB", (12, 12), "#734820").save(source, format="PNG")
        requested: list[str] = []

        class Response:
            headers = {"Content-Length": str(len(source.getvalue()))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return source.getvalue()

        class Opener:
            def open(self, request, timeout):
                requested.append(request.full_url)
                return Response()

        with patch("bilibili_card.build_opener", return_value=Opener()):
            self.assertIsNotNone(
                _download_image(
                    "https://i0.hdslb.com/bfs/draw@672w_1c.webp"
                )
            )
        self.assertEqual(requested[0], "https://i0.hdslb.com/bfs/draw.webp")

    def test_native_download_prefers_suffix_free_original_before_thumbnail(self):
        from PIL import Image

        small = BytesIO()
        Image.new("RGB", (672, 1256), "#734820").save(small, format="PNG")
        original = BytesIO()
        Image.new("RGB", (1320, 2468), "#3c2410").save(original, format="PNG")
        requested: list[str] = []

        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload
                self.headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return self.payload

        class Opener:
            def open(self, request, timeout):
                requested.append(request.full_url)
                return Response(
                    original.getvalue()
                    if "@672w_1c" not in request.full_url
                    else small.getvalue()
                )

        with patch("bilibili_card.build_opener", return_value=Opener()):
            encoded = download_bilibili_image(
                "http://i0.hdslb.com/bfs/new_dyn/post@672w_1c.png"
            )

        self.assertIsNotNone(encoded)
        rendered = Image.open(BytesIO(encoded))
        self.assertEqual(requested[0], "https://i0.hdslb.com/bfs/new_dyn/post.png")
        self.assertEqual(rendered.size, (1284, 2400))

    def test_tall_poster_is_split_into_readable_parts(self):
        from PIL import Image

        source = BytesIO()
        Image.new("RGB", (1320, 2468), "#734820").save(source, format="JPEG")
        parts = split_bilibili_poster(source.getvalue())

        self.assertEqual(len(parts), 3)
        self.assertEqual(
            sum(Image.open(BytesIO(part)).height for part in parts),
            2468,
        )
        self.assertTrue(all(Image.open(BytesIO(part)).width == 1320 for part in parts))
        self.assertEqual(len(split_bilibili_poster(b"not-an-image")), 1)

    def test_html_card_gives_small_portrait_covers_a_readable_width(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            cover="https://i0.hdslb.com/bfs/new_dyn/post.png",
            link="https://www.bilibili.com/opus/1",
            cover_width=1320,
            cover_height=2468,
            image_only=True,
        )

        self.assertIn("图文动态 · 正文已包含在海报中", html)
        self.assertIn("width: 560px", html)
        self.assertIn("height: auto", html)

    def test_html_image_only_card_stays_focus_layout_without_dimensions(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            cover="https://i0.hdslb.com/bfs/new-draw.jpg",
            link="https://www.bilibili.com/opus/2",
            image_only=True,
        )

        self.assertIn("focus-content", html)
        self.assertIn("width: 560px", html)
        self.assertNotIn('<div class="portrait-content">', html)

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

    def test_html_card_uses_compact_portrait_layout_when_dimensions_are_known(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            cover="https://i0.hdslb.com/bfs/draw.jpg",
            cover_width=1320,
            cover_height=2468,
            image_only=True,
        )
        self.assertIn("focus-content", html)
        self.assertIn('width:406px;height:760px', html)
        self.assertIn("图文动态 · 正文已包含在海报中", html)

    def test_html_portrait_with_copy_keeps_side_by_side_layout(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            title="海报说明",
            cover="https://i0.hdslb.com/bfs/draw.jpg",
            cover_width=1320,
            cover_height=2468,
        )
        self.assertIn("portrait-content", html)
        self.assertIn('width:251px;height:470px', html)
        self.assertIn("海报说明", html)

    def test_html_focus_fallback_stacks_copy_and_full_poster(self):
        html = build_bilibili_card(
            author="UP",
            kind="图文",
            title="动态标题",
            summary="动态简短说明",
            cover="https://i0.hdslb.com/bfs/draw.jpg",
            cover_width=1320,
            cover_height=2468,
            focus_cover=True,
        )

        self.assertIn("focus-content", html)
        self.assertNotIn('<div class="portrait-content">', html)
        self.assertIn("width: 560px", html)
        self.assertIn("动态简短说明", html)
        self.assertIn("max-height: 760px", html)

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

    def test_required_poster_does_not_render_an_empty_success_card(self):
        with (
            patch("bilibili_card._download_image", return_value=None),
            self.assertRaises(BilibiliCoverUnavailable),
        ):
            render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "cover": "https://i0.hdslb.com/bfs/draw.jpg",
                    "image_only": True,
                    "require_cover": True,
                }
            )

    def test_local_card_expands_portrait_cover_without_cropping(self):
        from PIL import Image

        portrait = Image.new("RGB", (1320, 2468), "#734820")
        with patch("bilibili_card._download_image", return_value=portrait):
            image = render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "cover": "https://i0.hdslb.com/bfs/new_dyn/post.png",
                    "image_only": True,
                    "link": "https://www.bilibili.com/opus/1",
                }
            )

        rendered = Image.open(BytesIO(image))
        self.assertEqual(rendered.width, 560)
        self.assertGreater(rendered.height, 700)
        self.assertLess(rendered.height, 1_200)

    def test_local_image_only_square_cover_uses_focus_width(self):
        from PIL import Image

        square = Image.new("RGB", (1088, 1080), "#734820")
        with patch("bilibili_card._download_image", return_value=square):
            image = render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "cover": "https://i0.hdslb.com/bfs/draw-preview.jpg",
                    "image_only": True,
                    "link": "https://www.bilibili.com/opus/3",
                }
            )

        rendered = Image.open(BytesIO(image))
        self.assertEqual(rendered.width, 560)
        # The square poster should fill the focus card instead of being
        # constrained to the old portrait column.
        self.assertGreaterEqual(rendered.height, 650)

    def test_local_focus_fallback_keeps_portrait_readable_with_copy(self):
        from PIL import Image

        portrait = Image.new("RGB", (1320, 2468), "#734820")
        with patch("bilibili_card._download_image", return_value=portrait):
            image = render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "title": "动态标题",
                    "summary": "动态简短说明",
                    "cover": "https://i0.hdslb.com/bfs/draw.jpg",
                    "focus_cover": True,
                }
            )

        rendered = Image.open(BytesIO(image))
        self.assertEqual(rendered.width, 560)
        self.assertGreater(rendered.height, 1_000)

    def test_focus_fallback_download_keeps_tall_cover_resolution(self):
        from PIL import Image

        portrait = Image.new("RGB", (1320, 2468), "#734820")
        requested_sizes: list[tuple[int, int]] = []

        def download(_value, *, max_size=(1600, 1000)):
            requested_sizes.append(max_size)
            return portrait

        with patch("bilibili_card._download_image", side_effect=download):
            render_bilibili_card(
                {
                    "author": "UP",
                    "kind": "图文",
                    "cover": "https://i0.hdslb.com/bfs/draw.jpg",
                    "focus_cover": True,
                }
            )

        self.assertIn((1600, 2400), requested_sizes)

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
