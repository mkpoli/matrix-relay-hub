"""Shrink Matrix media until QQ's group upload accepts it.

QQ takes at most 10 MiB per direct upload: on 2026-11-13 a 10,215,531-byte JPEG
was accepted and a 10,507,538-byte one refused with 40093011. It refuses WebM
(40034002) and accepts JPEG, PNG, GIF, WebP, MP4 and MOV.
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

QQ_UPLOAD_LIMIT = 10 * 1024 * 1024
# Headroom so a file that lands just under the limit is not refused for rounding.
TARGET = QQ_UPLOAD_LIMIT - 128 * 1024

QQ_IMAGE, QQ_VIDEO, QQ_AUDIO, QQ_FILE = 1, 2, 3, 4
QQ_FILE_TYPES = {"m.image": QQ_IMAGE, "m.video": QQ_VIDEO, "m.audio": QQ_AUDIO, "m.file": QQ_FILE}
QQ_VIDEO_TYPES = {"video/mp4", "video/quicktime"}

# Pixels decoded at once (after JPEG's reduced-scale decode): 32 MP of RGBA is
# 128 MB, which a 1 GB host can spare beside the homeserver.
MAX_PIXELS = 32_000_000
IMAGE_SIDES = (2560, 2048, 1600, 1280)
IMAGE_QUALITIES = (88, 80, 70)

AUDIO_KBPS = 64
MIN_VIDEO_KBPS = 120
MAX_FPS = 30
TEMP_PREFIX = "qq-fit-"

TOO_LARGE = "too large for QQ"
TOO_LONG = "too long for QQ"
UNREADABLE = "not accepted by QQ"


class Unfit(Exception):
    """The media cannot be made into something QQ accepts; str() is the reason."""


@dataclass(frozen=True)
class Prepared:
    data: bytes
    filename: str
    file_type: int


def _renamed(filename: str, suffix: str) -> str:
    stem = PurePath(filename).stem or "file"
    return f"{stem}{suffix}"


def sweep_temp() -> None:
    """Remove work directories left by a process that was killed mid-transcode."""
    for path in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*"):
        shutil.rmtree(path, ignore_errors=True)


def _is_animated(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return bool(getattr(image, "is_animated", False))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return False


def fit_image(data: bytes, limit: int = TARGET) -> bytes:
    """Re-encode a still image (an animation's first frame) as a JPEG under `limit`."""
    try:
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        # JPEG decodes straight to a smaller scale; other formats shrink in place.
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
    """Run a command; a timeout kills it and counts as a failure."""
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
        ffprobe, "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "default=noprint_wrappers=1",
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
    if duration <= 0:
        # Browser recordings often carry no duration; read the last timestamp.
        code, out = await _run(
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0",
            str(path),
            timeout=300,
        )
        times = []
        for line in out.decode(errors="replace").split():
            with contextlib.suppress(ValueError):
                times.append(float(line.strip(",")))
        duration = max(times, default=0.0)
    return duration, audio


def _video_height(kbps: float) -> int:
    if kbps >= 1200:
        return 720
    if kbps >= 500:
        return 480
    return 360


def _scale_filter(height: int) -> str:
    # Cap the short side (so portrait clips shrink like landscape ones) and keep
    # both sides even, which yuv420p requires.
    short = f"trunc(min(%s,{height})/2)*2"
    return (
        f"scale='if(gte(iw,ih),-2,{short % 'iw'})'"
        f":'if(gte(iw,ih),{short % 'ih'},-2)'"
    )


async def fit_video(data: bytes, limit: int = TARGET) -> bytes:
    """Transcode a video (or an animated image) to an H.264 MP4 under `limit`."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        log.warning("ffmpeg is not installed; cannot shrink video")
        raise Unfit(UNREADABLE)
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as tmp:
        source = Path(tmp, "source")
        target = Path(tmp, "out.mp4")
        await asyncio.to_thread(source.write_bytes, data)
        duration, audio = await _probe(ffprobe, source)
        if duration <= 0:
            log.info("cannot read the video's duration")
            raise Unfit(UNREADABLE)
        audio_kbps = AUDIO_KBPS if audio else 0
        kbps = limit * 8 / 1000 / duration * 0.94 - audio_kbps
        # A shared single core encodes 720p at roughly real time; allow slack.
        timeout = 300 + 6 * duration
        for _ in range(3):
            if kbps < MIN_VIDEO_KBPS:
                log.info("video of %.0f s is too long to fit QQ", duration)
                raise Unfit(TOO_LONG)
            args = [
                "nice", "-n", "10", ffmpeg, "-nostdin", "-v", "error", "-y",
                "-threads", "1", "-i", str(source),
                "-map", "0:v:0", "-vf", _scale_filter(_video_height(kbps)),
                "-fpsmax", str(MAX_FPS),
                "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                "-b:v", f"{kbps:.0f}k",
                "-maxrate", f"{kbps * 1.3:.0f}k",
                "-bufsize", f"{kbps * 2:.0f}k",
                "-threads", "1",
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
            log.info("transcode came out at %d bytes; retrying smaller", size)
            kbps *= limit / size * 0.9
    raise Unfit(TOO_LARGE)


async def _fit_animation(data: bytes, filename: str, limit: int) -> Prepared:
    try:
        video = await fit_video(data, limit)
        return Prepared(video, _renamed(filename, ".mp4"), QQ_VIDEO)
    except Unfit:
        # ffmpeg cannot read every animation (animated WebP before 8.0).
        log.info("sending the first frame of %s instead", filename)
        image = await asyncio.to_thread(fit_image, data, limit)
        return Prepared(image, _renamed(filename, ".jpg"), QQ_IMAGE)


async def prepare(
    data: bytes,
    msgtype: str,
    mimetype: str,
    filename: str,
    *,
    force: bool = False,
    limit: int = TARGET,
) -> Prepared:
    """Return media QQ will take, or raise Unfit with the reason it cannot be.

    `force` re-encodes a file that is small enough but which QQ refused.
    """
    file_type = QQ_FILE_TYPES.get(msgtype, QQ_FILE)
    if file_type == QQ_IMAGE:
        if len(data) <= limit and not force:
            return Prepared(data, filename, QQ_IMAGE)
        if await asyncio.to_thread(_is_animated, data):
            return await _fit_animation(data, filename, limit)
        image = await asyncio.to_thread(fit_image, data, limit)
        return Prepared(image, _renamed(filename, ".jpg"), QQ_IMAGE)
    if file_type == QQ_VIDEO:
        if len(data) <= limit and mimetype in QQ_VIDEO_TYPES and not force:
            return Prepared(data, filename, QQ_VIDEO)
        video = await fit_video(data, limit)
        return Prepared(video, _renamed(filename, ".mp4"), QQ_VIDEO)
    if force:
        raise Unfit(UNREADABLE)
    if len(data) > limit:
        raise Unfit(TOO_LARGE)
    return Prepared(data, filename, file_type)
