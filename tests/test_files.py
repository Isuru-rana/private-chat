import asyncio
import json

import pytest

from privatechat.files import (FileTools, OPEN_FILE_CHARS, PART_TOKENS, Piece, notes_prompt,
                               pinned_text, split_parts, with_system)


def f(name, text):
    return {"kind": "text", "name": name, "text": text, "truncated": False}


def run(coro):
    return asyncio.run(coro)


async def count(s):
    return len(s) // 3 + 1


def test_small_files_share_one_part():
    parts = run(split_parts([f("a.py", "x\n" * 10), f("b.py", "y\n" * 10)], count))
    assert len(parts) == 1 and [p.name for p in parts[0]] == ["a.py", "b.py"]


def test_files_never_split_when_they_fit():
    a, b = "a\n" * 9000, "b\n" * 9000           # ~6000 tokens each
    parts = run(split_parts([f("a", a), f("b", b)], count))
    assert [[p.name for p in part] for part in parts] == [["a"], ["b"]]


def test_big_file_split_at_lines_with_ranges():
    text = "\n".join(f"line {i}" for i in range(1, 12001))   # ~33k tokens
    parts = run(split_parts([f("big.py", text)], count))
    pieces = [p for part in parts for p in part]
    assert len(parts) >= 4 and pieces[0].start == 1 and pieces[-1].end == 12000
    assert all(nxt.start == cur.end + 1 for cur, nxt in zip(pieces, pieces[1:]))
    assert "".join(p.text for p in pieces).replace("\n", "") == text.replace("\n", "")


def test_minified_file_without_newlines_still_splits():
    parts = run(split_parts([f("app.min.js", "x" * 90_000)], count))   # ~30k tokens
    assert len(parts) >= 3 and all(run(count(p.text)) <= PART_TOKENS for part in parts for p in part)


def test_notes_prompt_names_part_and_all_files():
    part = [Piece("a.py", 1, 2, 2, "x\ny")]
    text = json.dumps(notes_prompt(part, 2, 4, ["a.py", "b.py"]))
    assert "part 2 of 4" in text and "b.py" in text and "a.py (lines 1-2 of 2)" in text


def test_pinned_text_lists_files_and_notes():
    out = pinned_text([f("a.py", "x\ny")], ["first", "second"])
    assert "a.py (2 lines)" in out and "Notes, part 2:" in out and "second" in out
    assert out.startswith("Notes from reading the attached files in full")


def test_with_system_merges_into_existing():
    out = with_system([{"role": "system", "content": "S"}, {"role": "user", "content": "u"}], "N")
    assert [m["role"] for m in out] == ["system", "user"] and out[0]["content"].startswith("N")
    assert out[0]["content"].endswith("S")


def test_with_system_inserts_when_absent():
    out = with_system([{"role": "user", "content": "u"}], "N")
    assert [m["role"] for m in out] == ["system", "user"] and out[0]["content"] == "N"


def test_open_file_whole_and_range():
    t = FileTools([f("a.py", "l1\nl2\nl3")])
    assert run(t.run({"name": "a.py"})).text.endswith("l1\nl2\nl3")
    r = run(t.run({"name": "a.py", "start_line": 2, "end_line": 2}))
    assert "l2" in r.text and "l1" not in r.text and r.step["status"] == "done"


def test_open_file_caps_and_says_more_remain():
    t = FileTools([f("big", "\n".join("y" * 100 for _ in range(1000)))])
    r = run(t.run({"name": "big"}))
    assert len(r.text) <= OPEN_FILE_CHARS + 200 and "more lines" in r.text


@pytest.mark.parametrize("args", [{"name": "nope.py"}, {"name": "a.py", "start_line": "x"},
                                  {"name": "a.py", "start_line": 3, "end_line": 1},
                                  {"name": "a.py", "start_line": 99}, {}, None])
def test_open_file_bad_args_never_raise(args):
    r = run(FileTools([f("a.py", "l1\nl2")]).run(args))
    assert r.step["status"] == "error" and r.text


def test_unknown_name_lists_valid_names():
    assert "a.py" in run(FileTools([f("a.py", "x")]).run({"name": "zz"})).text


def test_from_history_newest_same_name_wins_and_none_without_text():
    h = [{"role": "user", "content": "", "attachments": [f("a.py", "old")]},
         {"role": "user", "content": "", "attachments": [f("a.py", "new")]}]
    assert run(FileTools.from_history(h).run({"name": "a.py"})).text.endswith("new")
    assert FileTools.from_history([{"role": "user", "content": "hi"}]) is None


def test_open_file_null_lines_mean_defaults():
    r = run(FileTools([f("a.py", "l1\nl2\nl3")]).run({"name": "a.py", "start_line": None, "end_line": None}))
    assert r.step["status"] == "done" and r.text.endswith("l1\nl2\nl3")
    r = run(FileTools([f("a.py", "l1\nl2\nl3")]).run({"name": "a.py", "start_line": 2, "end_line": None}))
    assert r.text.endswith("l2\nl3") and "l1" not in r.text


def test_line_counts_agree_for_trailing_newline():
    from privatechat.attachments import line_count, to_llm
    text = "l1\nl2\nl3\n"
    assert line_count(text) == 3
    assert "a.py (3 lines)" in pinned_text([f("a.py", text)], ["n"])
    assert "lines 1-3 of 3" in run(FileTools([f("a.py", text)]).run({"name": "a.py"})).text
    piece = run(split_parts([f("a.py", text)], count))[0][0]
    assert (piece.start, piece.end, piece.total) == (1, 3, 3)
    msg = to_llm({"role": "user", "content": "", "attachments": [{**f("a.py", text), "read": "parts"}]})
    assert "(3 lines)" in msg["content"][0]["text"]


def test_line_counts_split_on_newline_only():
    text = "a\x0cb c\nd"  # form feed / line separator are not line breaks here
    from privatechat.attachments import line_count
    assert line_count(text) == 2
    assert "lines 1-2 of 2" in run(FileTools([f("x", text)]).run({"name": "x"})).text
    assert run(split_parts([f("x", text)], count))[0][0].total == 2


def test_spec_lists_valid_names_capped():
    from privatechat.files import OPEN_FILE_SPEC, SPEC_NAMES
    spec = FileTools([f("a.py", "x"), f("b.md", "y")]).spec
    desc = spec["function"]["parameters"]["properties"]["name"]["description"]
    assert "a.py" in desc and "b.md" in desc and spec["function"]["name"] == "open_file"
    assert "a.py" not in json.dumps(OPEN_FILE_SPEC)  # the shared base spec is left untouched
    many = FileTools([f(f"f{i}", "x") for i in range(SPEC_NAMES + 7)]).spec
    desc = many["function"]["parameters"]["properties"]["name"]["description"]
    assert f"f{SPEC_NAMES}" not in desc and "and 7 more" in desc


def test_notes_prompt_asks_for_function_and_class_names():
    ask = notes_prompt([Piece("a.py", 1, 1, 1, "x")], 1, 1, ["a.py"])[0]["content"]
    assert "every top-level function" in ask and "class" in ask and "about 300 words" in ask
