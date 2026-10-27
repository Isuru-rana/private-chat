import asyncio

from privatechat.context import (
    IMAGE_TOKENS,
    SUMMARY_RESERVE,
    cut_index,
    message_text,
    plan,
    strip_old_images,
)


def msg(id, role, content, atts=None):
    m = {"id": id, "role": role, "content": content}
    if atts:
        m["attachments"] = atts
    return m


IMG = {"kind": "image", "name": "cat.png", "mime": "image/png", "data": "QUJD"}
FILE = {"kind": "text", "name": "notes.txt", "text": "file body", "truncated": False}


def convo(n):
    """u0 a0 u1 a1 ... with 100-char contents."""
    out = []
    for i in range(n):
        out.append(msg(f"u{i}", "user", f"q{i} " + "x" * 96))
        out.append(msg(f"a{i}", "assistant", f"a{i} " + "y" * 96))
    return out


async def count_chars(m):
    # deterministic fake tokenizer: 1 token per character
    return len(message_text(m)) + IMAGE_TOKENS * sum(a["kind"] == "image" for a in m.get("attachments", []))


def run_plan(history, budget, summaries=None):
    return asyncio.run(plan(history, count=count_chars, budget=budget, summaries=summaries or {}))


def test_cut_index_keeps_newest_that_fit():
    assert cut_index([10, 10, 10, 10], 100) == 0
    assert cut_index([10, 10, 10, 10], 25) == 2
    assert cut_index([10, 10, 10, 50], 25) == 3  # last message is always kept


def test_message_text_includes_file_text():
    assert "file body" in message_text(msg("u", "user", "hi", [FILE]))


def test_old_images_hidden_beyond_last_two_user_turns():
    h = [msg("u0", "user", "a", [IMG]), msg("a0", "assistant", "b"),
         msg("u1", "user", "c", [IMG]), msg("a1", "assistant", "d"),
         msg("u2", "user", "e", [IMG, FILE])]
    out, hidden = strip_old_images(h, keep_turns=2)
    assert hidden == 1
    assert out[0]["attachments"][0]["kind"] == "image_hidden"
    assert out[2]["attachments"][0]["kind"] == "image"
    assert out[4]["attachments"][0]["kind"] == "image"
    assert h[0]["attachments"][0]["kind"] == "image"  # input not modified


def test_small_chat_sends_everything():
    p = run_plan(convo(2), budget=10_000)
    assert p.dropped == [] and p.needs_summary is None
    assert [m["content"] for m in p.final_messages()][0].startswith("q0")


def test_long_chat_drops_oldest_and_starts_with_user():
    h = convo(10)  # 20 messages x ~100 tokens
    p = run_plan(h, budget=SUMMARY_RESERVE + 450)
    kept = p.final_messages()
    assert kept[-1]["content"].startswith("q9") or kept[-1]["content"].startswith("a9")
    assert p.first_kept_id.startswith("u")
    assert len(p.dropped) > 0
    assert p.dropped[-1]["id"] != p.first_kept_id
    # nothing summarized yet -> summary requested for everything dropped
    prev, rest = p.needs_summary
    assert prev is None and rest == p.dropped


def test_summary_goes_first_as_system_message():
    h = convo(10)
    p = run_plan(h, budget=SUMMARY_RESERVE + 450)
    p.summary = "they talked about x and y"
    first = p.final_messages()[0]
    assert first["role"] == "system" and "they talked about x and y" in first["content"]


def test_cached_summary_reused():
    h = convo(10)
    p0 = run_plan(h, budget=SUMMARY_RESERVE + 450)
    last_dropped = p0.dropped[-1]["id"]
    p = run_plan(h, budget=SUMMARY_RESERVE + 450, summaries={last_dropped: "cached"})
    assert p.needs_summary is None and p.summary == "cached"


def test_incremental_summary_from_earlier_one():
    h = convo(10)
    p0 = run_plan(h, budget=SUMMARY_RESERVE + 450)
    ids = [m["id"] for m in p0.dropped]
    p = run_plan(h, budget=SUMMARY_RESERVE + 450, summaries={ids[1]: "older summary"})
    prev, rest = p.needs_summary
    assert prev == "older summary" and [m["id"] for m in rest] == ids[2:]
