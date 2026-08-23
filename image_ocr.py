from __future__ import annotations

from io import BytesIO
from typing import Any
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000


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
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL comes from QQ
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return ""
        image = Image.open(BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            image.thumbnail((2000, 2000))
        return str(pytesseract.image_to_string(image) or "").strip()[:4000]
    except Exception:
        return ""
