"""Text, mention, quote, and filename formatting between WeCom and Matrix."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote

# WeCom uses both regular spaces and U+2005 (four-per-em space) after @mentions.
_MENTION_SPACE = r"[\s\u2005]"
_LEADING_AT_RE = re.compile(rf"^{_MENTION_SPACE}*@[^\s\u2005@]+(?:{_MENTION_SPACE}+|$)")
_SAFE_LOCALPART_RE = re.compile(r"^[a-z0-9._-]+$")
_LOCALPART_BAD_RE = re.compile(r"[^a-z0-9._-]+")
REPLY_FALLBACK_RE = re.compile(r"> (?:\* )?<[@!#][^\s>]+>")


def ghost_localpart(userid: str) -> str:
    """Turn a WeCom userid into a legal, collision-resistant Matrix localpart."""
    raw = userid.strip()
    if _SAFE_LOCALPART_RE.match(raw) and len(raw) <= 64:
        return f"wecom_{raw}"
    slug = _LOCALPART_BAD_RE.sub("_", raw.lower()).strip("_")[:32] or "user"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"wecom_{slug}_{digest}"


def strip_bot_mention(text: str, bot_name: str = "", *, is_group: bool = True) -> str:
    """Remove the leading @BotName prefix WeCom puts on group @-mentions."""
    value = (text or "").strip()
    if not value or not is_group:
        return value
    if bot_name:
        escaped = re.escape(bot_name.strip())
        pattern = re.compile(rf"^(?:{_MENTION_SPACE}*@{escaped}(?:{_MENTION_SPACE}+|$))+")
        stripped = pattern.sub("", value, count=1).strip()
        if stripped != value:
            return stripped
    return _LEADING_AT_RE.sub("", value, count=1).strip()


def strip_reply_fallback(body: str) -> str:
    """Drop the Matrix `> <@user> ...` fallback block while keeping real quotes."""
    lines = body.replace("\r\n", "\n").split("\n")
    if not lines or not REPLY_FALLBACK_RE.match(lines[0].lstrip()):
        return body
    index = 0
    while index < len(lines) and lines[index].lstrip().startswith(">"):
        index += 1
    if index < len(lines) and lines[index].strip() == "":
        return "\n".join(lines[index + 1 :])
    return body


def chunk_utf8(text: str, max_bytes: int = 4096) -> list[str]:
    """Split text on line or space boundaries so each chunk fits `max_bytes` UTF-8 bytes."""
    if max_bytes < 4:
        raise ValueError("max_bytes must be at least 4")
    remaining = text
    parts: list[str] = []
    while len(remaining.encode("utf-8")) > max_bytes:
        byte_count = 0
        cut = 0
        for index, char in enumerate(remaining):
            size = len(char.encode("utf-8"))
            if byte_count + size > max_bytes:
                break
            byte_count += size
            cut = index + 1
        if cut == 0:
            break
        break_at = remaining.rfind("\n", 0, cut)
        if break_at < cut // 2:
            break_at = remaining.rfind(" ", 0, cut)
        if break_at < cut // 2:
            break_at = cut
        part = remaining[:break_at].rstrip()
        if part:
            parts.append(part)
        remaining = remaining[break_at:].lstrip("\n")
        if not remaining:
            break
    if remaining:
        parts.append(remaining)
    return parts


def _escape_html(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br/>")


def quote_summary(quote: dict | None) -> str:
    """Extract a readable plain-text summary from a WeCom `quote` payload."""
    if not isinstance(quote, dict):
        return ""
    msgtype = quote.get("msgtype")
    if msgtype == "text":
        return str((quote.get("text") or {}).get("content") or "").strip()
    if msgtype == "voice":
        content = str((quote.get("voice") or {}).get("content") or "").strip()
        return f"[voice] {content}" if content else "[voice]"
    if msgtype == "image":
        return "[image]"
    if msgtype == "file":
        return "[file]"
    if msgtype == "video":
        return "[video]"
    if msgtype == "mixed":
        items = (quote.get("mixed") or {}).get("msg_item") or []
        bits: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("msgtype")
            if kind == "text":
                t = str((item.get("text") or {}).get("content") or "").strip()
                if t:
                    bits.append(t)
            elif kind == "image":
                bits.append("[image]")
        return " ".join(bits).strip()
    return ""


def render_matrix_with_quote(body: str, quote: dict | None) -> tuple[str, str | None]:
    """Combine a WeCom message body with an optional quoted block for Matrix."""
    quoted = quote_summary(quote)
    if not quoted:
        return body, None
    quoted_lines = "\n".join(f"> {line}" for line in quoted.splitlines())
    plain = f"{quoted_lines}\n\n{body}" if body else quoted_lines
    formatted = f"<blockquote>{_escape_html(quoted)}</blockquote>"
    if body:
        formatted += _escape_html(body)
    return plain, formatted


def _clean_shortcode(value: object) -> str:
    if not isinstance(value, str):
        return ""
    code = " ".join(value.split()).strip(":")
    return f":{code[:64]}:" if code else ""


def format_reaction_key(
    key: object,
    *,
    content: dict | None = None,
    relates: dict | None = None,
) -> str:
    """Turn a Matrix reaction key into readable text for WeCom."""
    if not isinstance(key, str):
        return ""
    cleaned = " ".join(key.split())
    if not cleaned:
        return ""
    if cleaned.startswith("mxc://"):
        candidates: list[object] = []
        if isinstance(relates, dict):
            candidates.append(relates.get("shortcode"))
        if isinstance(content, dict):
            candidates.append(content.get("shortcode"))
            candidates.append(content.get("com.beeper.reaction.shortcode"))
            discord_reaction = content.get("fi.mau.discord.reaction")
            if isinstance(discord_reaction, dict):
                candidates.append(discord_reaction.get("name"))
        for candidate in candidates:
            formatted = _clean_shortcode(candidate)
            if formatted:
                return formatted
        return "[custom emoji]"
    if len(cleaned) > 100:
        return cleaned[:100] + "..."
    return cleaned


def parse_content_disposition(header: str | None) -> str | None:
    """Extract a safe basename from an HTTP Content-Disposition header."""
    if not header:
        return None
    raw_name: str | None = None
    utf8_match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\s]+)", header)
    if utf8_match:
        raw_name = unquote(utf8_match.group(1))
    else:
        quoted_match = re.search(r'filename\s*=\s*"([^"]+)"', header)
        if quoted_match:
            raw_name = unquote(quoted_match.group(1))
        else:
            bare_match = re.search(r"filename\s*=\s*([^;\s]+)", header)
            if bare_match:
                raw_name = unquote(bare_match.group(1))
    if not raw_name:
        return None
    # Strip both POSIX and Windows directory prefixes to avoid path traversal.
    name = PureWindowsPath(PurePosixPath(raw_name).name).name.strip()
    if not name or name in (".", ".."):
        return None
    return name


def sanitize_filename(name: str | None, fallback: str) -> str:
    if not name:
        return fallback
    cleaned = PureWindowsPath(PurePosixPath(name).name).name.strip()
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned


_WECOM_MD_TAG_RE = re.compile(r"<(@|/?font\b)", re.IGNORECASE)


def sanitize_wecom_markdown(text: str) -> str:
    """Neutralize WeCom Markdown mention (<@userid>) and <font> tags in relayed text."""
    return _WECOM_MD_TAG_RE.sub("<\u200b\\1", text)
