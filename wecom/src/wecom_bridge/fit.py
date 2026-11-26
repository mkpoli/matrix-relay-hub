"""Fit Matrix media to WeCom AI Bot and Group Robot Webhook limits.

WeCom limits:
- AI Bot WebSocket upload: image <= 10 MiB (JPG/PNG), video <= 10 MiB (MP4),
  file <= 20 MiB (> 5 bytes).
- Group Robot Webhook: image <= 2 MiB (JPG/PNG, base64 + MD5),
  file <= 20 MiB (> 5 bytes, uploaded via webhook/upload_media).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

AIBOT_IMAGE_LIMIT = 10 * 1024 * 1024 - 128 * 1024
AIBOT_VIDEO_LIMIT = 10 * 1024 * 1024 - 128 * 1024
WEBHOOK_IMAGE_LIMIT = 2 * 1024 * 1024 - 64 * 1024
FILE_LIMIT = 20 * 1024 * 1024
MIN_FILE_BYTES = 6

MAX_PIXELS = 32_000_000
IMAGE_SIDES = (2560, 2048, 1600, 1280, 960)
IMAGE_QUALITIES = (88, 80, 70, 60)

AUDIO_KBPS = 64
MIN_VIDEO_KBPS = 120
MAX_FPS = 30
TEMP_PREFIX = "wecom-fit-"

TOO_LARGE = "too large for WeCom"
TOO_SMALL = "too small for WeCom"
TOO_LONG = "too long for WeCom"
UNREADABLE = "not accepted by WeCom"

WECOM_MEDIA_MSGTYPES = frozenset({"m.image", "m.video", "m.audio", "m.file"})


class Unfit(Exception):
    """The media cannot be made into something WeCom accepts."""


@dataclass(frozen=True)
class Prepared:
    data: bytes
    filename: str
    media_type: str  # "image" | "video" | "file"


def _renamed(filename: str, suffix: str) -> str:
    stem = PurePath(filename).stem or "file"
    return f"{stem}{suffix}"


def sweep_temp() -> None:
    """Remove work directories left behind by an interrupted transcode."""
    for path in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*"):
        shutil.rmtree(path, ignore_errors=True)


def sniff_mime(data: bytes, fallback: str = "application/octet-stream") -> tuple[str, str]:
    """Detect common media MIME types and default extensions from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    if data.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    return fallback, ""


def _is_jpg_or_png(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")


def fit_image(data: bytes, limit: int = AIBOT_IMAGE_LIMIT) -> bytes:
    """Re-encode an image (or an animation's first frame) as JPEG under `limit`."""
    try:
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        image.draft("RGB", (IMAGE_SIDES[0], IMAGE_SIDES[0]))
        if image.width * image.height > MAX_PIXELS:
            raise Unfit(TOO_LARGE)
        image.thumbnail((IMAGE_SIDES[0], IMAGE_SIDES[0]), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        log.info("cannot decode image: %s", exc)
        raise Unfit(UNREADABLE) from exc
    if exif:
        image.info["exif"] = exif.tobytes()
        image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
    else:
        image = image.convert("RGB")
    for side in IMAGE_SIDES:
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        for quality in IMAGE_QUALITIES:
            out = io.BytesIO()
            image.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            if out.tell() <= limit:
                return out.getvalue()
    raise Unfit(TOO_LARGE)


async def _run(*args: str, timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        if isinstance(exc, TimeoutError):
            log.info("%s timed out after %.0f s", args[0], timeout)
            return -1, b""
        raise
    if process.returncode:
        log.info("%s failed: %s", args[0], err.decode(errors="replace")[-500:])
    return process.returncode or 0, out


async def _probe(ffprobe: str, path: Path) -> tuple[float, bool]:
    code, out = await _run(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "default=noprint_wrappers=1",
        str(path),
        timeout=120,
    )
    if code:
        return 0.0, False
    duration = 0.0
    audio = False
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key == "duration":
            with contextlib.suppress(ValueError):
                duration = float(value)
        elif key == "codec_type" and value == "audio":
            audio = True
    return duration, audio


def _video_height(kbps: float) -> int:
    if kbps >= 1200:
        return 720
    if kbps >= 500:
        return 480
    return 360


def _scale_filter(height: int) -> str:
    short = f"trunc(min(%s,{height})/2)*2"
    return (
        f"scale='if(gte(iw,ih),-2,{short % 'iw'})'"
        f":'if(gte(iw,ih),{short % 'ih'},-2)'"
    )


async def fit_video(data: bytes, limit: int = AIBOT_VIDEO_LIMIT) -> bytes:
    """Transcode a video into an H.264 MP4 under `limit`."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        log.warning("ffmpeg is not installed; cannot transcode video")
        raise Unfit(UNREADABLE)
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as tmp:
        source = Path(tmp, "source")
        target = Path(tmp, "out.mp4")
        await asyncio.to_thread(source.write_bytes, data)
        duration, audio = await _probe(ffprobe, source)
        if duration <= 0:
            raise Unfit(UNREADABLE)
        audio_kbps = AUDIO_KBPS if audio else 0
        kbps = limit * 8 / 1000 / duration * 0.94 - audio_kbps
        timeout = 300 + 6 * duration
        for _ in range(3):
            if kbps < MIN_VIDEO_KBPS:
                raise Unfit(TOO_LONG)
            args = [
                "nice",
                "-n",
                "10",
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-threads",
                "1",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                _scale_filter(_video_height(kbps)),
                "-fpsmax",
                str(MAX_FPS),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                f"{kbps:.0f}k",
                "-maxrate",
                f"{kbps * 1.3:.0f}k",
                "-bufsize",
                f"{kbps * 2:.0f}k",
                "-threads",
                "1",
            ]
            if audio:
                args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"]
            else:
                args += ["-an"]
            args += ["-movflags", "+faststart", str(target)]
            code, _ = await _run(*args, timeout=timeout)
            if code:
                raise Unfit(UNREADABLE)
            size = target.stat().st_size
            if size <= limit:
                return await asyncio.to_thread(target.read_bytes)
            kbps *= limit / size * 0.9
    raise Unfit(TOO_LARGE)


async def prepare(
    data: bytes,
    msgtype: str,
    mimetype: str,
    filename: str,
    *,
    mode: str = "aibot",
) -> Prepared:
    """Prepare Matrix media for WeCom AI Bot or Webhook delivery."""
    if msgtype == "m.image":
        limit = WEBHOOK_IMAGE_LIMIT if mode == "webhook" else AIBOT_IMAGE_LIMIT
        if len(data) <= limit and _is_jpg_or_png(data):
            suffix = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
            out_name = filename if Path(filename).suffix.lower() in (".jpg", ".jpeg", ".png") else _renamed(filename, suffix)
            return Prepared(data, out_name, "image")
        converted = await asyncio.to_thread(fit_image, data, limit)
        return Prepared(converted, _renamed(filename, ".jpg"), "image")

    if msgtype == "m.video" and mode == "aibot":
        if len(data) <= AIBOT_VIDEO_LIMIT and mimetype == "video/mp4":
            return Prepared(data, _renamed(filename, ".mp4"), "video")
        try:
            transcoded = await fit_video(data, AIBOT_VIDEO_LIMIT)
            return Prepared(transcoded, _renamed(filename, ".mp4"), "video")
        except Unfit:
            # Fall back to file upload when transcoding fails but raw file fits 20 MiB.
            if MIN_FILE_BYTES <= len(data) <= FILE_LIMIT:
                return Prepared(data, filename, "file")
            raise

    # m.file, m.audio, or webhook m.video -> WeCom file upload (>5 B and <=20 MiB).
    if len(data) < MIN_FILE_BYTES:
        raise Unfit(TOO_SMALL)
    if len(data) > FILE_LIMIT:
        raise Unfit(TOO_LARGE)
    return Prepared(data, filename, "file")
