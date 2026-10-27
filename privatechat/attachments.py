"""Attachments: validate uploads and turn them into model input.

Images go to the model as pictures (vision). Text files and PDFs are turned into
text and added to the message. The file type is decided from the bytes, never
from the name or the browser's MIME type.
"""

import base64
import binascii
import io
import os

MAX_FILES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 24_000  # all files in one message together; the model only sees ~16k tokens
MAX_NAME = 120

_IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


class AttachmentError(ValueError):
    pass


def _image_mime(data: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pdf_text(data: bytes, name: str) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise AttachmentError(f"{name}: could not read this PDF ({type(e).__name__})") from None


def _clean_name(name: str) -> str:
    name = os.path.basename(str(name).replace("\\", "/")) or "file"
    return name[:MAX_NAME]


def process(raw: list[dict]) -> list[dict]:
    """Validate uploads ({name, mime, data: base64}) into stored attachments."""
    if len(raw) > MAX_FILES:
        raise AttachmentError(f"attach at most {MAX_FILES} files per message")
    out = []
    text_left = MAX_TEXT_CHARS
    for item in raw:
        name = _clean_name(item.get("name", "file"))
        try:
            data = base64.b64decode(item.get("data", ""), validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentError(f"{name}: upload was corrupted") from None

        mime = _image_mime(data)
        if mime:
            if len(data) > MAX_IMAGE_BYTES:
                raise AttachmentError(f"{name}: images can be at most 10 MB")
            out.append({"kind": "image", "name": name, "mime": mime, "data": base64.b64encode(data).decode()})
            continue

        if len(data) > MAX_FILE_BYTES:
            raise AttachmentError(f"{name}: files can be at most 5 MB")
        if data.startswith(b"%PDF-"):
            text = _pdf_text(data, name)
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise AttachmentError(f"{name}: only images, PDFs and text files are supported") from None
            if "\x00" in text:
                raise AttachmentError(f"{name}: only images, PDFs and text files are supported")
        truncated = len(text) > text_left
        text = text[:text_left]
        text_left -= len(text)
        out.append({"kind": "text", "name": name, "text": text, "truncated": truncated})
    return out


def to_llm(message: dict) -> dict:
    """Stored message -> OpenAI-style message for llama-server."""
    attachments = message.get("attachments") or []
    if not attachments:
        return {"role": message["role"], "content": message["content"]}
    text = message["content"]
    images = []
    for a in attachments:
        if a["kind"] == "image":
            images.append({"type": "image_url", "image_url": {"url": f"data:{a['mime']};base64,{a['data']}"}})
        elif a["kind"] == "image_hidden":
            text += f"\n\n[image `{a['name']}` was shared here earlier; it is no longer shown to you]"
        else:
            note = "\n[this file was cut short: it is longer than the model can read at once]" if a["truncated"] else ""
            body = a["text"] or "[no readable text in this file]"
            text += f"\n\nAttached file `{a['name']}`:\n```\n{body}\n```{note}"
    return {"role": message["role"], "content": [{"type": "text", "text": text.strip()}, *images]}
