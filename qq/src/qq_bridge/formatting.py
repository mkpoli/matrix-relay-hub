"""Text conversion between QQ and Matrix."""

from __future__ import annotations

import base64
import html
import json
import re

FACE_RE = re.compile(r'<faceType=(\d+),faceId="([^"]*)"?,ext="([^"]*)"\s*/?>')
TOKEN_RE = re.compile(
    r'<faceType=(?P<ftype>\d+),faceId="(?P<face_id>[^"]*)"?,ext="(?P<ext>[^"]*)"\s*/?>'
    r"|<@(?P<mention>[^>]+)>"
)

# QQ face ids are the classic yellow faces with a shifted order; the API gives
# only the id, so map the common ones to the emoji they represent.
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


def _face_plain(face_id: str, ext: str) -> str:
    label = _face_label(ext)
    if label:
        return label
    try:
        number = int(face_id)
    except (TypeError, ValueError):
        return "[表情]"
    code = _face_code(number)
    return FACE_CODE_EMOJI.get(code, "[表情]") if code else "[表情]"


def extract_face_ids(content: str) -> list[int]:
    ids: list[int] = []
    for match in FACE_RE.finditer(content or ""):
        try:
            ids.append(int(match.group(2)))
        except (TypeError, ValueError):
            continue
    return ids


def replace_faces(content: str) -> str:
    """Turn QQ face markup into an emoji or a readable label."""
    return FACE_RE.sub(
        lambda match: _face_plain(match.group(2) or "", match.group(3) or ""),
        content or "",
    )


def _escape(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br/>")


def _mention_names(mentions: list[dict] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for mention in mentions or []:
        name = mention.get("username") or ""
        for key in ("id", "member_openid", "user_openid"):
            value = mention.get(key)
            if value:
                names[str(value).lower()] = name
    return names


def render_qq_text(
    content: str,
    mentions: list[dict] | None,
    domain: str,
    face_images: dict[int, str] | None = None,
) -> tuple[str, str]:
    """Return the plain body and the HTML body for a QQ message."""
    text = content or ""
    names = _mention_names(mentions)
    body: list[str] = []
    markup: list[str] = []
    position = 0
    for match in TOKEN_RE.finditer(text):
        body.append(text[position : match.start()])
        markup.append(_escape(text[position : match.start()]))
        if match.group("mention") is not None:
            openid = match.group("mention")
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
            face_id = match.group("face_id") or ""
            plain = _face_plain(face_id, match.group("ext") or "")
            body.append(plain)
            mxc = None
            if face_images:
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


def strip_reply_fallback(body: str) -> str:
    """Drop the `> <@user> ...` block Matrix clients put in front of replies."""
    lines = body.split("\n")
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    if index and index < len(lines) and lines[index] == "":
        return "\n".join(lines[index + 1 :])
    return body


def chunk(text: str, limit: int = 1800) -> list[str]:
    """Split a long message on paragraph or line boundaries."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def strip_leading_mentions(text: str) -> str:
    """Drop the <@bot> prefix QQ puts in front of an @-message."""
    return re.sub(r"^(?:\s*(?:<@[^>]+>|@[^\s@]+))+\s*", "", text).strip()
