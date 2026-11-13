import asyncio
import io
import os
import shutil
import subprocess

import pytest
from PIL import Image

from qq_bridge.fit import QQ_IMAGE, QQ_VIDEO, Unfit, fit_image, prepare

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing"
)


def _noise_png(width, height, mode="RGB"):
    image = Image.frombytes(mode, (width, height), os.urandom(width * height * len(mode)))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _ffmpeg(*args):
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args, "-f", "mp4", "-"],
        capture_output=True,
        check=True,
    )
    return result.stdout


def test_small_image_is_sent_untouched():
    data = _noise_png(64, 64)
    prepared = asyncio.run(prepare(data, "m.image", "image/png", "a.png"))
    assert prepared.data == data
    assert prepared.filename == "a.png"
    assert prepared.file_type == QQ_IMAGE


def test_large_image_becomes_a_jpeg_under_the_limit():
    data = _noise_png(1200, 900)
    limit = len(data) // 4
    prepared = asyncio.run(prepare(data, "m.image", "image/png", "scan.png", limit=limit))
    assert len(prepared.data) <= limit
    assert prepared.filename == "scan.jpg"
    assert Image.open(io.BytesIO(prepared.data)).format == "JPEG"


def test_transparency_is_flattened_onto_white():
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    out = io.BytesIO()
    image.save(out, "PNG")
    jpeg = fit_image(out.getvalue(), limit=10**6)
    assert Image.open(io.BytesIO(jpeg)).getpixel((20, 20)) == (255, 255, 255)


def test_undecodable_image_does_not_fit():
    with pytest.raises(Unfit, match="not accepted"):
        asyncio.run(prepare(b"x" * 100, "m.image", "", "a.png", limit=10))


def test_refused_image_is_reencoded_even_when_small():
    data = _noise_png(32, 32)
    prepared = asyncio.run(prepare(data, "m.image", "image/png", "a.png", force=True))
    assert prepared.filename == "a.jpg"


def test_oversized_file_does_not_fit():
    with pytest.raises(Unfit, match="too large"):
        asyncio.run(prepare(b"x" * 100, "m.file", "", "a.zip", limit=10))


def test_huge_image_is_refused_before_decoding():
    image = Image.new("1", (8000, 6000))
    out = io.BytesIO()
    image.save(out, "PNG")
    with pytest.raises(Unfit, match="too large"):
        fit_image(out.getvalue())


def test_large_jpeg_decodes_at_reduced_scale():
    image = Image.new("RGB", (9000, 6000), "gray")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=50)
    jpeg = fit_image(out.getvalue(), limit=10**6)
    assert max(Image.open(io.BytesIO(jpeg)).size) == 2560


def test_exif_rotation_is_applied():
    image = Image.new("RGB", (60, 20), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes())
    jpeg = fit_image(out.getvalue(), limit=10**6)
    assert Image.open(io.BytesIO(jpeg)).size == (20, 60)


@needs_ffmpeg
@pytest.mark.parametrize("size", ["499x281", "355x640"])
def test_odd_sized_video_transcodes(size):
    data = _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=s={size}:d=1", "-c:v", "libx264",
        "-pix_fmt", "yuv444p", "-movflags", "frag_keyframe+empty_moov",
    )
    prepared = asyncio.run(prepare(data, "m.video", "video/webm", "v.webm"))
    assert prepared.file_type == QQ_VIDEO


def test_animation_ffmpeg_cannot_read_falls_back_to_a_frame(monkeypatch):
    async def unreadable(data, limit):
        raise Unfit("not accepted by QQ")

    monkeypatch.setattr("qq_bridge.fit.fit_video", unreadable)
    frames = [Image.frombytes("RGB", (200, 200), os.urandom(120_000)) for _ in range(4)]
    out = io.BytesIO()
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=100)
    data = out.getvalue()
    prepared = asyncio.run(prepare(data, "m.image", "image/webp", "a.webp", limit=len(data) // 2))
    assert prepared.file_type == QQ_IMAGE and prepared.filename == "a.jpg"


@needs_ffmpeg
def test_webm_becomes_mp4():
    webm = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:d=2",
         "-c:v", "libvpx", "-f", "webm", "-"],
        capture_output=True, check=True,
    ).stdout
    prepared = asyncio.run(prepare(webm, "m.video", "video/webm", "clip.webm"))
    assert prepared.file_type == QQ_VIDEO
    assert prepared.filename == "clip.mp4"
    assert prepared.data[4:8] == b"ftyp"


@needs_ffmpeg
def test_large_video_is_transcoded_under_the_limit():
    data = _ffmpeg(
        "-f", "lavfi", "-i", "nullsrc=s=640x360:d=6,geq=random(1)*255:128:128",
        "-f", "lavfi", "-i", "sine=d=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "4000k",
        "-c:a", "aac", "-shortest", "-movflags", "frag_keyframe+empty_moov",
    )
    limit = len(data) // 3
    prepared = asyncio.run(prepare(data, "m.video", "video/mp4", "v.mp4", limit=limit))
    assert len(prepared.data) <= limit


@needs_ffmpeg
def test_video_too_long_for_the_limit_does_not_fit():
    data = _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=20:r=5", "-c:v", "libx264",
        "-movflags", "frag_keyframe+empty_moov",
    )
    # 20 s in 100 kB leaves 40 kb/s, below the floor.
    with pytest.raises(Unfit, match="too long"):
        asyncio.run(prepare(data, "m.video", "video/webm", "v.webm", limit=100_000))


@needs_ffmpeg
def test_large_animated_gif_becomes_a_video():
    frames = [Image.frombytes("RGB", (200, 200), os.urandom(120_000)) for _ in range(10)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100)
    data = out.getvalue()
    prepared = asyncio.run(prepare(data, "m.image", "image/gif", "cat.gif", limit=len(data) // 2))
    assert prepared.file_type == QQ_VIDEO
    assert prepared.filename == "cat.mp4"
