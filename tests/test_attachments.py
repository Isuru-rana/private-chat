import base64

import pytest

from privatechat.attachments import (
    AttachmentError,
    process,
    to_llm,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def raw(name, data, mime="application/octet-stream"):
    return {"name": name, "mime": mime, "data": b64(data)}


def txt(name, s):
    return raw(name, s.encode())


def png(name):
    return raw(name, PNG)


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_image_detected_by_content_not_mime():
    (a,) = process([raw("x.txt", PNG, "text/plain")])
    assert a == {"kind": "image", "name": "x.txt", "mime": "image/png", "data": b64(PNG)}


def test_jpeg_detected():
    assert process([raw("p.jpg", JPEG)])[0]["mime"] == "image/jpeg"


def test_text_file_decoded():
    (a,) = process([raw("notes.md", "héllo wörld\n".encode())])
    assert a == {"kind": "text", "name": "notes.md", "text": "héllo wörld\n", "truncated": False}


def test_svg_is_text_not_image():
    (a,) = process([raw("x.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")])
    assert a["kind"] == "text"


def test_pdf_text_extracted():
    (a,) = process([raw("doc.pdf", make_pdf("HELLO PDF CANARY"), "application/pdf")])
    assert a["kind"] == "text" and "HELLO PDF CANARY" in a["text"]


def test_binary_rejected():
    with pytest.raises(AttachmentError):
        process([raw("blob.bin", b"\x00\x01\x02\xff\xfe" * 20)])


def test_bad_base64_rejected():
    with pytest.raises(AttachmentError):
        process([{"name": "a.txt", "mime": "text/plain", "data": "!!!not base64"}])


def test_image_too_big():
    with pytest.raises(AttachmentError, match="10 MB"):
        process([raw("big.png", PNG + b"\x00" * (10 * 1024 * 1024))])


def test_file_too_big():
    with pytest.raises(AttachmentError, match="5 MB"):
        process([raw("big.txt", b"a" * (5 * 1024 * 1024 + 1))])


def test_fifty_files_ok_fifty_one_rejected():
    assert len(process([txt(f"f{i}.py", "x") for i in range(50)])) == 50
    with pytest.raises(AttachmentError, match="at most 50 files"):
        process([txt(f"f{i}.py", "x") for i in range(51)])


def test_at_most_five_images():
    with pytest.raises(AttachmentError, match="at most 5 images"):
        process([png(f"p{i}.png") for i in range(6)])


def test_total_size_capped():
    big = "a" * (4 * 1024 * 1024)
    with pytest.raises(AttachmentError, match="20 MB"):
        process([txt(f"f{i}.txt", big) for i in range(6)])  # 24 MB


def test_long_text_is_not_cut():
    out = process([txt("big.py", "x" * 100_000)])
    assert len(out[0]["text"]) == 100_000 and out[0]["truncated"] is False


def test_duplicate_names_renamed():
    names = [a["name"] for a in process([txt("__init__.py", "a"), txt("__init__.py", "b"), txt("__init__.py", "c")])]
    assert names == ["__init__.py", "__init__.py (2)", "__init__.py (3)"]


def test_name_is_basename_and_capped():
    (a,) = process([raw("../../etc/" + "n" * 300 + ".txt", b"x")])
    assert "/" not in a["name"] and len(a["name"]) <= 120


def test_to_llm_plain_message_unchanged():
    assert to_llm({"role": "assistant", "content": "hi"}) == {"role": "assistant", "content": "hi"}


def test_to_llm_with_text_and_image():
    msg = {
        "role": "user",
        "content": "look",
        "attachments": [
            {"kind": "text", "name": "a.py", "text": "print(1)", "truncated": True},
            {"kind": "image", "name": "p.png", "mime": "image/png", "data": "QUJD"},
        ],
    }
    out = to_llm(msg)
    assert out["role"] == "user"
    text, image = out["content"]
    assert text["type"] == "text"
    assert "look" in text["text"] and "a.py" in text["text"] and "print(1)" in text["text"]
    assert "cut short" in text["text"]
    assert image == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


def test_to_llm_hidden_image_becomes_note():
    out = to_llm({"role": "user", "content": "old", "attachments": [{"kind": "image_hidden", "name": "cat.png"}]})
    (text,) = out["content"]
    assert "cat.png" in text["text"] and "no longer" in text["text"]


def test_to_llm_parts_file_is_marker_not_text():
    m = {"role": "user", "content": "q", "attachments": [
        {"kind": "text", "name": "a.py", "text": "l1\nl2\nl3", "truncated": False, "read": "parts"}]}
    text = to_llm(m)["content"][0]["text"]
    assert "l1" not in text and "`a.py` (3 lines) was read in parts" in text and "open_file" in text


def test_old_truncated_attachment_still_noted():
    m = {"role": "user", "content": "q", "attachments": [{"kind": "text", "name": "a", "text": "t", "truncated": True}]}
    assert "cut short" in to_llm(m)["content"][0]["text"]
