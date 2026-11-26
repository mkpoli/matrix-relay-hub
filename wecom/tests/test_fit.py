import asyncio
import io

import pytest
from PIL import Image

from wecom_bridge.fit import FILE_LIMIT, TOO_LARGE, TOO_SMALL, Unfit, prepare, sniff_mime


def _make_image(fmt: str, size: tuple[int, int] = (64, 64), mode: str = "RGB") -> bytes:
    img = Image.new(mode, size, color=(20, 120, 220, 128) if mode == "RGBA" else (20, 120, 220))
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


def test_prepare_images_for_aibot_and_webhook():
    png = _make_image("PNG")
    res = asyncio.run(prepare(png, "m.image", "image/png", "photo.png", mode="aibot"))
    assert res.media_type == "image"
    assert res.data == png

    webp = _make_image("WEBP", mode="RGBA")
    res_webp = asyncio.run(prepare(webp, "m.image", "image/webp", "sticker.webp", mode="webhook"))
    assert res_webp.media_type == "image"
    assert res_webp.filename == "sticker.jpg"
    assert res_webp.data.startswith(b"\xff\xd8\xff")


def test_prepare_files_size_bounds():
    with pytest.raises(Unfit, match=TOO_SMALL):
        asyncio.run(prepare(b"12345", "m.file", "text/plain", "tiny.txt", mode="aibot"))
    with pytest.raises(Unfit, match=TOO_LARGE):
        asyncio.run(prepare(b"x" * (FILE_LIMIT + 1), "m.file", "application/octet-stream", "big.bin"))
    ok = asyncio.run(prepare(b"hello world", "m.audio", "audio/ogg", "voice.ogg", mode="aibot"))
    assert ok.media_type == "file"
    assert ok.filename == "voice.ogg"


def test_sniff_mime():
    assert sniff_mime(_make_image("PNG")) == ("image/png", ".png")
    assert sniff_mime(_make_image("JPEG")) == ("image/jpeg", ".jpg")
    assert sniff_mime(b"%PDF-1.7 test") == ("application/pdf", ".pdf")
