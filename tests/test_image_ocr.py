import base64
import unittest
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from image_ocr import embedded_image_text, normalize_vision_image_ref


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

    def test_qq_face_label_is_exposed_as_image_text(self):
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                message=[], raw_message=SimpleNamespace(raw_data={})
            ),
            get_message_str=lambda: "[表情:[龙年快乐]] [图片]",
        )

        self.assertEqual(embedded_image_text(event), "龙年快乐")


if __name__ == "__main__":
    unittest.main()
