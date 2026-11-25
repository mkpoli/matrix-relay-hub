"""Text conversion between QQ and Matrix."""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

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
            if href.startswith(("http://", "https://")) and not href.startswith(
                "https://matrix.to/#/@"
            ):
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
