"""Text conversion between QQ and Matrix."""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlparse

SPACE_RE = re.compile(r"[ \t]*")
FACE_RE = re.compile(r'<faceType=(\d+),faceId="([^"]*)"?,ext="([^"]*)"\s*/?>')
TOKEN_RE = re.compile(
    r'<faceType=(?P<ftype>\d+),faceId="(?P<face_id>[^"]*)"?,ext="(?P<ext>[^"]*)"\s*/?>'
    r"|<@(?P<mention>[^>]+)>"
)

# faceType 1 and 3 are the small yellow faces. faceType 6 is a sticker: its
# faceId is always 0 and the picture arrives in the message's attachments.
STICKER_FACE_TYPE = "6"

# QQ face ids name the system emoji; qzoneCode is 100 + the id, except that
# 微笑 and 惊讶 swapped places in the two lists. Map the common ones to emoji.
FACE_CODE_EMOJI = {
    100: "😊", 101: "😒", 102: "😍", 103: "😳", 104: "😎", 105: "😭",
    106: "😊", 107: "🤐", 108: "😴", 109: "😭", 110: "😅", 111: "😡",
    112: "😜", 113: "😁", 114: "😲", 115: "😞", 116: "😎", 117: "😓",
    118: "😫", 119: "🤮", 120: "🤭", 121: "😊", 122: "🙄", 123: "😤",
    124: "😋", 125: "😪", 126: "😱", 127: "😓", 128: "😄", 129: "😌",
    130: "💪", 131: "🤬", 132: "❓", 133: "🤫", 134: "😵", 135: "😖",
    136: "😔", 137: "💀", 138: "🔨", 139: "👋", 140: "😅",
}

FACE_ID_ALIAS = {0: 114, 14: 100}


def _face_code(face_id: int) -> int | None:
    if face_id in FACE_ID_ALIAS:
        return FACE_ID_ALIAS[face_id]
    code = 100 + face_id
    return code if 100 <= code <= 140 else None


def _face_label(ext: str) -> str:
    if not ext:
        return ""
    try:
        padded = ext + "=" * (-len(ext) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        return ""
    text = data.get("text") if isinstance(data, dict) else None
    return str(text) if text else ""


def _face_plain(face_type: str, face_id: str, ext: str) -> str:
    label = _face_label(ext)
    if label:
        # QQ sometimes supplies the name already bracketed; keep one pair.
        return label if label.startswith("[") and label.endswith("]") else f"[{label}]"
    if face_type == STICKER_FACE_TYPE:
        return "[表情]"
    try:
        number = int(face_id)
    except (TypeError, ValueError):
        return "[表情]"
    code = _face_code(number)
    return FACE_CODE_EMOJI.get(code, "[表情]") if code else "[表情]"


def extract_face_ids(content: str) -> list[int]:
    """Ids of the small faces that have a picture in the face mirror.

    Sticker ids are left out: faceType 6 always reports 0, so the mirror's
    face 0 would replace every sticker with the same wrong picture.
    """
    ids: list[int] = []
    for match in FACE_RE.finditer(content or ""):
        if match.group(1) == STICKER_FACE_TYPE:
            continue
        try:
            ids.append(int(match.group(2)))
        except (TypeError, ValueError):
            continue
    return ids


def sticker_labels(content: str) -> str:
    """Readable labels for the faceType 6 stickers in a message."""
    labels: list[str] = []
    for match in FACE_RE.finditer(content or ""):
        if match.group(1) != STICKER_FACE_TYPE:
            continue
        label = _face_plain(
            match.group(1), match.group(2) or "", match.group(3) or ""
        )
        if label:
            labels.append(label)
    return " ".join(labels)


def replace_faces(content: str) -> str:
    """Turn QQ face markup into an emoji or a readable label."""
    return FACE_RE.sub(
        lambda match: _face_plain(
            match.group(1), match.group(2) or "", match.group(3) or ""
        ),
        content or "",
    )


def _escape(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br/>")


def _mention_names(mentions: list[dict] | None) -> tuple[dict[str, str], set[str]]:
    """Names by openid, and the openids of bots. A QQ reply to a relayed message @-mentions the
    bridge's own bot; QQ marks it with `is_you` or `bot`
    (https://github.com/tencent-connect/openclaw-qqbot src/types.ts, read 2026-11-24). No bot can
    be reached from Matrix, so their mentions are left out."""
    names: dict[str, str] = {}
    bots: set[str] = set()
    for mention in mentions or []:
        name = mention.get("username") or mention.get("nickname") or ""
        for key in ("id", "member_openid", "user_openid"):
            value = mention.get(key)
            if value:
                names[str(value).lower()] = name
                if mention.get("is_you") or mention.get("bot"):
                    bots.add(str(value).lower())
    return names, bots


_FORWARD_HEADER_RE = re.compile(r"^\[([^\[\]\n]{1,60}聊天记录)\]$")
_FORWARD_ITEM_RE = re.compile(r"^===\s*消息\s*(\d+)\s*===$")
_FORWARD_FIELD_RE = re.compile(r"^\[(消息内容|发送者|时间)\]\s?(.*)$")
_FORWARD_ATTACHMENT_RE = re.compile(r"^\[附件\d+\]\s*(.+)$")
_FORWARD_ATT_URL_RE = re.compile(r"(?:^|\s)URL:(https://\S+)", re.IGNORECASE)
_FORWARD_ATT_KIND_RE = re.compile(r"(?:^|\s)类型:(\S+)")
_FORWARD_ATT_NAME_RE = re.compile(r"(?:^|\s)文件名:(.+?)(?=\s+(?:尺寸|大小|URL):|$)", re.IGNORECASE)
_FORWARD_ATT_DIM_RE = re.compile(r"(?:^|\s)尺寸:(\d+)x(\d+)", re.IGNORECASE)

_TRUSTED_QQ_MEDIA_DOMAINS = (
    "nt.qq.com.cn",
    "nt.qq.com",
    "qpic.cn",
)
_VOLATILE_URL_PARAMS = frozenset({"rkey", "sig", "token", "auth_token", "t", "ts"})
MAX_FORWARDED_ATTACHMENTS = 16


@dataclass(frozen=True)
class ForwardedAttachment:
    kind: str
    filename: str
    url: str
    content_type: str
    width: int | None = None
    height: int | None = None


def is_trusted_qq_media_url(url: str) -> bool:
    """Return True only for HTTPS URLs hosted on Tencent QQ's media domains."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in _TRUSTED_QQ_MEDIA_DOMAINS
    )


def normalize_media_url_key(url: str) -> str:
    """Return a stable comparison key for a QQ media URL, ignoring volatile tokens like `rkey`."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    stable_params = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _VOLATILE_URL_PARAMS
    )
    query = "&".join(f"{k}={v}" for k, v in stable_params)
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{parsed.path}?{query}"


def _guess_forwarded_mime(kind: str, filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if kind == "图片":
        return guessed if guessed and guessed.startswith("image/") else "image/jpeg"
    if kind == "视频":
        return guessed if guessed and guessed.startswith("video/") else "video/mp4"
    if kind == "语音":
        return guessed if guessed and guessed.startswith("audio/") else "audio/mpeg"
    return guessed or "application/octet-stream"


def _parse_forwarded_attachment(body: str) -> ForwardedAttachment | None:
    url_match = _FORWARD_ATT_URL_RE.search(body)
    if not url_match:
        return None
    url = url_match.group(1).strip()
    kind_match = _FORWARD_ATT_KIND_RE.search(body)
    kind = kind_match.group(1).strip() if kind_match else "文件"
    name_match = _FORWARD_ATT_NAME_RE.search(body)
    filename = name_match.group(1).strip() if name_match else "file"
    dim_match = _FORWARD_ATT_DIM_RE.search(body)
    width = int(dim_match.group(1)) if dim_match else None
    height = int(dim_match.group(2)) if dim_match else None
    return ForwardedAttachment(
        kind=kind,
        filename=filename,
        url=url,
        content_type=_guess_forwarded_mime(kind, filename),
        width=width,
        height=height,
    )


def _attachment_dedupe_key(att: ForwardedAttachment) -> tuple:
    return (
        att.kind,
        att.filename,
        att.width,
        att.height,
        normalize_media_url_key(att.url),
    )


def _parse_forwarded_record(
    text: str,
) -> tuple[str, list[tuple[str, str, tuple[ForwardedAttachment, ...]]]] | None:
    """Parse QQ Bot API's flattened merged-forward text into `(header, entries)`.

    Each entry is `(sender, content, attachments)`. Returns None if `text` does
    not match QQ's `[...聊天记录]` / `=== 消息 N ===` layout.
    """
    lines = text.replace("\r\n", "\n").strip().split("\n")
    if len(lines) < 3:
        return None
    header_match = _FORWARD_HEADER_RE.match(lines[0].strip())
    if not header_match:
        return None
    header = header_match.group(0)

    blocks: list[tuple[int, list[str]]] = []
    current_num: int | None = None
    current: list[str] | None = None
    for line in lines[1:]:
        stripped = line.strip()
        item_match = _FORWARD_ITEM_RE.match(stripped)
        if item_match:
            if current is not None and current_num is not None:
                blocks.append((current_num, current))
            current_num = int(item_match.group(1))
            current = []
        elif current is None:
            if stripped:
                return None
        else:
            current.append(line)
    if current is not None and current_num is not None:
        blocks.append((current_num, current))
    if not blocks:
        return None

    # Require strict 1..N numbering so nested forwards or literal markers in content
    # bail out safely instead of splitting or misattributing messages.
    if [num for num, _ in blocks] != list(range(1, len(blocks) + 1)):
        return None

    entries: list[tuple[str, str, str, tuple[ForwardedAttachment, ...]]] = []
    for _, raw_block in blocks:
        block = list(raw_block)
        while block and not block[-1].strip():
            block.pop()
        while block and not block[0].strip():
            block.pop(0)
        if not block:
            return None

        sender = ""
        timestamp = ""
        tail_attachments: list[ForwardedAttachment] = []
        # Peel trailing metadata lines ([附件N], [发送者], [时间]) from the bottom
        # first so literal `[发送者] ...` lines inside multiline `[消息内容]` stay intact.
        while block:
            att_match = _FORWARD_ATTACHMENT_RE.match(block[-1])
            if att_match:
                parsed_att = _parse_forwarded_attachment(att_match.group(1))
                if parsed_att is None:
                    break
                tail_attachments.append(parsed_att)
                block.pop()
                continue
            tail_match = _FORWARD_FIELD_RE.match(block[-1])
            if not tail_match:
                break
            tag, value = tail_match.group(1), tail_match.group(2).strip()
            if tag == "发送者" and not sender:
                sender = value
                block.pop()
            elif tag == "时间" and not timestamp:
                timestamp = value
                block.pop()
            else:
                break
        tail_attachments.reverse()

        # Also support leading [发送者]/[时间] before [消息内容].
        while block:
            head_match = _FORWARD_FIELD_RE.match(block[0])
            if not head_match:
                break
            tag, value = head_match.group(1), head_match.group(2).strip()
            if tag == "发送者" and not sender:
                sender = value
                block.pop(0)
            elif tag == "时间" and not timestamp:
                timestamp = value
                block.pop(0)
            else:
                break

        if not sender:
            return None

        if block:
            content_match = _FORWARD_FIELD_RE.match(block[0])
            if not content_match or content_match.group(1) != "消息内容":
                return None
            content_lines = [content_match.group(2), *block[1:]]
            content = "\n".join(content_lines).strip()
        else:
            content = ""

        entries.append((sender, content, timestamp, tuple(tail_attachments)))

    # QQ Bot API sometimes flattens a merged forward twice ([1..k, 1..k]),
    # issuing fresh volatile `rkey` tokens on the second copy's attachment URLs.
    if len(entries) >= 2 and len(entries) % 2 == 0:
        half = len(entries) // 2
        keys = [
            (sender, content, timestamp, tuple(_attachment_dedupe_key(a) for a in atts))
            for sender, content, timestamp, atts in entries
        ]
        if keys[:half] == keys[half:]:
            entries = entries[:half]
    return header, [(sender, content, atts) for sender, content, _, atts in entries]


def extract_forwarded_attachments(
    content: str, mentions: list[dict] | None = None
) -> list[dict]:
    """Extract deduplicated, trusted QQ media attachments from a forwarded chat record."""
    _, bots = _mention_names(mentions)
    forwarded = _parse_forwarded_record(_strip_leading_bot_mentions(content or "", bots))
    if forwarded is None:
        return []
    _, entries = forwarded
    result: list[dict] = []
    seen: set[str] = set()
    for _, _, atts in entries:
        for att in atts:
            if not is_trusted_qq_media_url(att.url):
                continue
            key = normalize_media_url_key(att.url)
            if key in seen:
                continue
            seen.add(key)
            item: dict = {
                "url": att.url,
                "filename": att.filename,
                "content_type": att.content_type,
                "trusted_qq_media": True,
            }
            if att.width:
                item["width"] = att.width
            if att.height:
                item["height"] = att.height
            result.append(item)
            if len(result) >= MAX_FORWARDED_ATTACHMENTS:
                return result
    return result


def _render_qq_inline(
    text: str,
    names: dict[str, str],
    bots: set[str],
    domain: str,
    face_images: dict[int, str] | None = None,
    sticker_image: bool = False,
) -> tuple[str, str]:
    body: list[str] = []
    markup: list[str] = []
    position = 0
    for match in TOKEN_RE.finditer(text):
        body.append(text[position : match.start()])
        markup.append(_escape(text[position : match.start()]))
        if match.group("mention") is not None:
            openid = match.group("mention")
            if openid.lower() in bots:
                position = SPACE_RE.match(text, match.end()).end()
                continue
            if openid.lower() == "all":
                body.append("@全体成员")
                markup.append("<strong>@全体成员</strong>")
            else:
                name = names.get(openid.lower()) or openid[:8]
                body.append(f"@{name}")
                ghost = f"@qq_{openid.lower()}:{domain}"
                markup.append(
                    f'<a href="https://matrix.to/#/{html.escape(ghost)}">@{_escape(name)}</a>'
                )
        else:
            face_type = match.group("ftype") or ""
            face_id = match.group("face_id") or ""
            sticker = face_type == STICKER_FACE_TYPE
            plain = (
                ""
                if sticker and sticker_image
                else _face_plain(face_type, face_id, match.group("ext") or "")
            )
            if plain:
                body.append(plain)
                mxc = None
                if face_images and not sticker:
                    try:
                        mxc = face_images.get(int(face_id))
                    except (TypeError, ValueError):
                        mxc = None
                if mxc:
                    escaped = html.escape(plain, quote=True)
                    markup.append(
                        f'<img data-mx-emoticon src="{html.escape(mxc, quote=True)}"'
                        f' alt="{escaped}" title="{escaped}" height="32" />'
                        f"{_escape(plain)}"
                    )
                else:
                    markup.append(_escape(plain))
        position = match.end()
    body.append(text[position:])
    markup.append(_escape(text[position:]))
    return "".join(body).strip(), "".join(markup).strip()


def _strip_leading_bot_mentions(text: str, bots: set[str]) -> str:
    if not bots:
        return text
    pos = 0
    length = len(text)
    while True:
        while pos < length and text[pos].isspace():
            pos += 1
        if not text.startswith("<@", pos):
            break
        end = text.find(">", pos + 2)
        if end == -1:
            break
        openid = text[pos + 2 : end]
        if openid.lower() not in bots:
            break
        pos = end + 1
    return text[pos:].lstrip()


def render_qq_text(
    content: str,
    mentions: list[dict] | None,
    domain: str,
    face_images: dict[int, str] | None = None,
    sticker_image: bool = False,
) -> tuple[str, str]:
    """Return the plain body and the HTML body for a QQ message.

    `sticker_image` says the message carries an image attachment, which is
    how a faceType 6 sticker arrives; its markup is then dropped from the
    text because the attachment is the sticker.
    """
    text = content or ""
    names, bots = _mention_names(mentions)
    forwarded = _parse_forwarded_record(_strip_leading_bot_mentions(text, bots))
    if forwarded is not None:
        header, entries = forwarded
        groups: list[tuple[str, list[tuple[str, str]]]] = []
        for sender, raw_item, atts in entries:
            item_plain, item_html = _render_qq_inline(
                raw_item, names, bots, domain, face_images, sticker_image=sticker_image
            )
            untrusted_labels = [
                f"[{att.kind}: {att.filename}]"
                for att in atts
                if not is_trusted_qq_media_url(att.url)
            ]
            if untrusted_labels:
                suffix = " ".join(untrusted_labels)
                item_plain = f"{item_plain}\n{suffix}" if item_plain else suffix
                item_html = (
                    f"{item_html}<br/>{_escape(suffix)}"
                    if item_html
                    else _escape(suffix)
                )
            if not item_plain and not item_html:
                if atts:
                    kinds = [f"[{att.kind}]" for att in atts]
                    placeholder = " ".join(kinds)
                else:
                    placeholder = "[图片]" if sticker_image else "[消息]"
                item_plain = placeholder
                item_html = _escape(placeholder)
            if groups and groups[-1][0] == sender:
                groups[-1][1].append((item_plain, item_html))
            else:
                groups.append((sender, [(item_plain, item_html)]))

        plain_blocks: list[str] = []
        html_blocks: list[str] = []
        for sender, items in groups:
            if len(items) == 1 and "\n" not in items[0][0]:
                item_plain, item_html = items[0]
                if sender:
                    plain_blocks.append(f"> {sender}: {item_plain}")
                    html_blocks.append(f"<p><strong>{_escape(sender)}</strong>: {item_html}</p>")
                else:
                    plain_blocks.append(f"> {item_plain}")
                    html_blocks.append(f"<p>{item_html}</p>")
            else:
                plain_lines: list[str] = []
                html_lines: list[str] = []
                if sender:
                    plain_lines.append(f"> {sender}:")
                    html_lines.append(f"<strong>{_escape(sender)}</strong>:")
                for item_plain, item_html in items:
                    for subline in item_plain.split("\n"):
                        plain_lines.append(f"> {subline}" if subline else ">")
                    html_lines.append(item_html)
                plain_blocks.append("\n".join(plain_lines))
                html_blocks.append(f"<p>{'<br/>'.join(html_lines)}</p>")

        plain_body = header + "\n" + "\n>\n".join(plain_blocks)
        html_body = (
            f"<p>{_escape(header)}</p>"
            f"<blockquote>{''.join(html_blocks)}</blockquote>"
        )
        return plain_body, html_body

    return _render_qq_inline(text, names, bots, domain, face_images, sticker_image)


REPLY_FALLBACK = re.compile(r"> (?:\* )?<[@!#][^\s>]+>")


def strip_reply_fallback(body: str) -> str:
    """Drop the `> <@user> ...` block Matrix clients put in front of replies. Only a block that
    opens the way the spec writes a fallback is dropped: a message that is itself a quote (a
    Markdown blockquote ends up as `> ...` lines) is kept whole."""
    lines = body.replace("\r\n", "\n").split("\n")
    if not lines or not REPLY_FALLBACK.match(lines[0].lstrip()):
        return body
    index = 0
    while index < len(lines) and lines[index].lstrip().startswith(">"):
        index += 1
    if index < len(lines) and lines[index].strip() == "":
        return "\n".join(lines[index + 1 :])
    return body


def utf16_length(text: str) -> int:
    """The text's length in UTF-16 code units, the unit Matrix bridges and Discord count."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _prefix_within(text: str, limit: int) -> int:
    """The length of the longest prefix of `text` that is at most `limit` code units."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return index
    return len(text)


def chunk(text: str, limit: int = 1800) -> list[str]:
    """Split a long message on paragraph or line boundaries, each part at most `limit` code units."""
    chunks: list[str] = []
    while utf16_length(text) > limit:
        cut = _prefix_within(text, limit)
        break_at = text.rfind("\n", 0, cut)
        if break_at < cut // 2:
            break_at = text.rfind(" ", 0, cut)
        if break_at < cut // 2:
            break_at = cut
        chunks.append(text[:break_at].rstrip())
        text = text[break_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def strip_leading_mentions(text: str) -> str:
    """Drop the <@bot> prefix QQ puts in front of an @-message."""
    return re.sub(r"^(?:\s*(?:<@[^>]+>|@[^\s@]+))+\s*", "", text).strip()


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
    """Turn a Matrix reaction key into safe, readable text for QQ."""
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


@dataclass(frozen=True)
class CustomEmoji:
    mxc: str
    shortcode: str
    animated: bool = False


_BLOCK_TAGS = frozenset(
    {"p", "div", "blockquote", "pre", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
)


class _EmoticonHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.non_emoji_parts: list[str] = []
        self.emojis: list[CustomEmoji] = []
        self._seen_mxc: set[str] = set()
        self._mx_reply_depth = 0
        self._link_stack: list[tuple[str, int]] = []
        self._last_emoji_alt: str | None = None

    def _append_break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        if self.non_emoji_parts and not self.non_emoji_parts[-1].endswith("\n"):
            self.non_emoji_parts.append("\n")
        self._last_emoji_alt = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "mx-reply":
            self._mx_reply_depth += 1
            return
        if self._mx_reply_depth:
            return
        attr_map = {k.lower(): v for k, v in attrs}
        if tag == "br":
            self.parts.append("\n")
            self.non_emoji_parts.append("\n")
            self._last_emoji_alt = None
            return
        if tag in _BLOCK_TAGS:
            self._append_break()
        if tag == "a":
            href = (attr_map.get("href") or "").strip()
            self._link_stack.append((href, len(self.parts)))
        elif tag == "img":
            self._handle_img(attr_map)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag == "mx-reply" or self._mx_reply_depth:
            return
        if tag == "br":
            self.parts.append("\n")
            self.non_emoji_parts.append("\n")
            self._last_emoji_alt = None
            return
        if tag == "img":
            self._handle_img({k.lower(): v for k, v in attrs})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "mx-reply":
            self._mx_reply_depth = max(0, self._mx_reply_depth - 1)
            return
        if self._mx_reply_depth:
            return
        if tag == "a" and self._link_stack:
            href, start_index = self._link_stack.pop()
            if href.startswith(("http://", "https://")):
                is_user_pill = href.startswith("https://matrix.to/#/") and unquote(
                    href[len("https://matrix.to/#/") :]
                ).startswith("@")
                if not is_user_pill:
                    inner = "".join(self.parts[start_index:]).strip()
                    if inner and href not in inner:
                        suffix = f" ({href})"
                        self.parts.append(suffix)
                        self.non_emoji_parts.append(suffix)
            self._last_emoji_alt = None
        elif tag in _BLOCK_TAGS:
            self._append_break()

    def handle_data(self, data: str) -> None:
        if self._mx_reply_depth or not data:
            return
        text = data
        if self._last_emoji_alt and text.startswith(self._last_emoji_alt):
            # Avoid duplicating the shortcode when HTML already repeats it right after <img>.
            text = text[len(self._last_emoji_alt) :]
        self._last_emoji_alt = None
        if text:
            self.parts.append(text)
            self.non_emoji_parts.append(text)

    def _handle_img(self, attrs: dict[str, str | None]) -> None:
        src = (attrs.get("src") or "").strip()
        alt = (attrs.get("alt") or "").strip()
        is_emoticon = "data-mx-emoticon" in attrs
        if is_emoticon and src.startswith("mxc://") and alt:
            animated = "data-mau-animated-emoji" in attrs
            if src not in self._seen_mxc:
                self._seen_mxc.add(src)
                self.emojis.append(
                    CustomEmoji(mxc=src, shortcode=alt, animated=animated)
                )
            self.parts.append(alt)
            self._last_emoji_alt = alt
            return
        if alt:
            self.parts.append(alt)
            self.non_emoji_parts.append(alt)
            self._last_emoji_alt = None


def extract_custom_emojis(formatted_body: str) -> tuple[str, list[CustomEmoji], bool]:
    """Extract Matrix custom emoticons (`<img data-mx-emoticon>`) from HTML.

    Returns `(plain_text, unique_emojis, emoji_only)` while ignoring `<mx-reply>`
    fallback blocks and empty-alt decorative icons such as Discord embed author
    avatars.
    """
    if not formatted_body or "data-mx-emoticon" not in formatted_body.lower():
        return "", [], False
    parser = _EmoticonHTMLParser()
    parser.feed(formatted_body)
    parser.close()
    if not parser.emojis:
        return "", [], False
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
    non_emoji = "".join(parser.non_emoji_parts).strip()
    return text, parser.emojis, not non_emoji
