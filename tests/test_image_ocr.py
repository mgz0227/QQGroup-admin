import base64
import unittest
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from image_ocr import (
    embedded_image_text,
    is_remote_gif_ref,
    normalize_vision_image_ref,
)


class ImageOCRTest(unittest.TestCase):
    def test_inline_gif_is_converted_to_png(self):
        source = BytesIO()
        Image.new("RGB", (2, 2), "red").save(source, format="GIF")
        value = "base64:data:image/gif;base64," + base64.b64encode(
            source.getvalue()
        ).decode("ascii")

        converted = normalize_vision_image_ref(value)

        self.assertTrue(converted.startswith("data:image/png;base64,"))
        with Image.open(BytesIO(base64.b64decode(converted.split(",", 1)[1]))) as image:
            self.assertEqual(image.format, "PNG")

    def test_remote_gif_is_skipped_without_downloading(self):
        url = "https://example.test/media/a.gif?token=secret"

        self.assertTrue(is_remote_gif_ref(url))
        self.assertEqual(normalize_vision_image_ref(url), "")
        self.assertTrue(
            is_remote_gif_ref(
                "https://example.test/media?id=1&filename=sticker.gif"
            )
        )
        self.assertFalse(is_remote_gif_ref("https://example.test/media/a.png"))

    def test_qq_face_label_is_exposed_as_image_text(self):
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                message=[], raw_message=SimpleNamespace(raw_data={})
            ),
            get_message_str=lambda: "[表情:[龙年快乐]] [图片]",
        )

        self.assertEqual(embedded_image_text(event), "龙年快乐")

    def test_plain_caption_is_not_treated_as_image_text(self):
        Plain = type("Plain", (), {})
        ImageComponent = type("Image", (), {})
        plain = Plain()
        plain.text = "普通说明词"
        image = ImageComponent()
        image.alt = "图片元数据"
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                message=[plain, image], raw_message=SimpleNamespace(raw_data={})
            ),
            get_message_str=lambda: "普通说明词 [图片]",
        )

        self.assertEqual(embedded_image_text(event), "图片元数据")


if __name__ == "__main__":
    unittest.main()
