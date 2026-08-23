from __future__ import annotations

import base64
import binascii
import re
from io import BytesIO
from typing import Any
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000


def normalize_vision_image_ref(value: str) -> str:
    """Convert inline GIFs to a provider-compatible PNG data URI."""

    original = str(value or "").strip()
    if not original:
        return ""
    candidate = original[7:] if original.lower().startswith("base64:data:") else original
    payload = ""
    lowered = candidate.lower()
    if lowered.startswith("data:image/gif;base64,"):
        payload = candidate.split(",", 1)[1]
    elif candidate.startswith("base64://"):
        payload = candidate.removeprefix("base64://")
    else:
        return original
    if len(payload) > (MAX_IMAGE_BYTES * 4 // 3) + 16:
        return ""
    try:
        data = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError):
        return ""
    if not data.startswith((b"GIF87a", b"GIF89a")):
        return original
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return ""
            image.seek(0)
            frame = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = BytesIO()
            frame.save(output, format="PNG")
        png = output.getvalue()
    except (ImportError, OSError, ValueError):
        return ""
    if len(png) > MAX_IMAGE_BYTES:
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def embedded_image_text(event: Any) -> str:
    """Read OCR-like text exposed by an adapter without downloading media."""

    values: list[str] = []
    components = getattr(getattr(event, "message_obj", None), "message", None) or []
    for component in components:
        for key in ("ocr_text", "text", "alt", "description", "caption", "name"):
            value = getattr(component, key, None)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())

    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    raw_data = getattr(raw, "raw_data", None)
    if isinstance(raw_data, dict):
        for item in raw_data.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            for key in ("ocr_text", "text", "alt", "description", "caption", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    get_message_str = getattr(event, "get_message_str", None)
    if callable(get_message_str):
        message_text = str(get_message_str() or "")
        values.extend(
            match.strip()
            for match in re.findall(
                r"\[(?:表情|Face):\[?([^\]\r\n]+)\]?\]",
                message_text,
                flags=re.IGNORECASE,
            )
            if match.strip()
        )
    return "\n".join(dict.fromkeys(values))[:4000]


def ocr_image_url(url: str, timeout: float = 4.0) -> str:
    """Best-effort local OCR; missing pytesseract or its binary fails open."""

    if not str(url or "").startswith(("http://", "https://")):
        return ""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        request = Request(
            str(url),
            headers={"User-Agent": "Mozilla/5.0 QQGroup-admin/2.2"},
        )
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return ""
        image = Image.open(BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            image.thumbnail((2000, 2000))
        return str(pytesseract.image_to_string(image) or "").strip()[:4000]
    except (OSError, RuntimeError, ValueError):
        return ""
