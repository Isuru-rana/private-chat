"""Full reading of big text attachments: split into parts, notes prompt, open_file tool."""
from dataclasses import dataclass
from typing import Awaitable, Callable

from privatechat.attachments import line_count
from privatechat.web.tools import ToolOutcome

INLINE_TOKENS = 4000
PART_TOKENS = 10000
MAX_PARTS = 5
NOTE_TOKENS = 500
OPEN_FILE_CHARS = 14000
OPEN_FILE_RESERVE = 4000  # history room kept free so one open_file result fits

Count = Callable[[str], Awaitable[int]]


@dataclass
class Piece:
    name: str
    start: int  # 1-based, inclusive
    end: int
    total: int  # line count of the whole file
    text: str


async def text_tokens(files: list[dict], count: Count) -> int:
    return sum([await count(f["text"]) for f in files])


def _cut(text: str, size: int) -> list[str]:
    """Chunks of about `size` chars, cut at the last newline when there is one."""
    out = []
    while len(text) > size:
        i = text.rfind("\n", 0, size)
        i = i + 1 if i > 0 else size
        out.append(text[:i])
        text = text[i:]
    return out + [text] if text else out


async def split_parts(files: list[dict], count: Count) -> list[list[Piece]]:
    parts: list[list[Piece]] = []
    cur: list[Piece] = []
    used = 0
    for f in files:
        text = f["text"]
        total = line_count(text)
        tokens = await count(text)
        if tokens <= PART_TOKENS:
            if cur and used + tokens > PART_TOKENS:
                parts.append(cur)
                cur, used = [], 0
            cur.append(Piece(f["name"], 1, total, total, text))
            used += tokens
            continue
        if cur:
            parts.append(cur)
            cur, used = [], 0
        size = max(1, int(PART_TOKENS * (len(text) / tokens) * 0.95))
        line = 1
        for chunk in _cut(text, size):
            n = line_count(chunk)
            parts.append([Piece(f["name"], line, line + n - 1, total, chunk)])
            line += n
    if cur:
        parts.append(cur)
    return parts


def _label(p: Piece) -> str:
    return f"{p.name} (lines {p.start}-{p.end} of {p.total})"


def notes_prompt(part: list[Piece], k: int, n: int, all_names: list[str]) -> list[dict]:
    """Messages asking the model for notes on one part of the attachments."""
    body = "\n\n".join(f"=== {_label(p)} ===\n{p.text}" for p in part)
    ask = (f"You are reading attached files in full, in {n} parts. This is part {k} of {n}. "
           f"All attached files: {', '.join(all_names)}.\n"
           "Write notes on the text below: what each file or piece does; every top-level function "
           "and class by exact name, each with a one-line purpose (exact names help find the lines "
           "later with open_file); how it connects to the other files (imports, calls, config); and "
           "anything wrong or unfinished. Answer as plain notes, about 300 words.")
    return [{"role": "system", "content": ask}, {"role": "user", "content": body}]


def pinned_text(files: list[dict], notes: list[str]) -> str:
    lines = "\n".join(f"- {f['name']} ({line_count(f['text'])} lines)" for f in files)
    blocks = "\n\n".join(f"Notes, part {i}:\n{t}" for i, t in enumerate(notes, 1))
    return ("Notes from reading the attached files in full (the file text itself is not shown; "
            f"call open_file for exact lines).\n\nFiles:\n{lines}\n\n{blocks}")


def with_system(msgs: list[dict], text: str) -> list[dict]:
    if msgs and msgs[0].get("role") == "system":
        return [{**msgs[0], "content": text + "\n\n" + msgs[0]["content"]}, *msgs[1:]]
    return [{"role": "system", "content": text}, *msgs]


OPEN_FILE_SPEC = {"type": "function", "function": {
    "name": "open_file",
    "description": "Open an attached file and get its exact lines. Use it when the notes are not "
                   "enough. Returns up to about 4000 tokens per call.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "Attached file name"},
        "start_line": {"type": "integer", "description": "First line (1-based), optional"},
        "end_line": {"type": "integer", "description": "Last line (inclusive), optional"}},
        "required": ["name"]}}}


SPEC_NAMES = 50  # names listed in the tool spec; the rest are summed up


class FileTools:
    def __init__(self, files: list[dict]):
        self.files = {f["name"]: f["text"] for f in files}

    @property
    def names(self) -> list[str]:
        return list(self.files)

    @property
    def spec(self) -> dict:
        """OPEN_FILE_SPEC with the valid names, so the model knows them after old turns drop out."""
        names = self.names
        listed = ", ".join(names[:SPEC_NAMES]) + (f" and {len(names) - SPEC_NAMES} more"
                                                  if len(names) > SPEC_NAMES else "")
        fn = OPEN_FILE_SPEC["function"]
        props = {**fn["parameters"]["properties"],
                 "name": {"type": "string", "description": f"Attached file name, one of: {listed}"}}
        return {**OPEN_FILE_SPEC, "function": {**fn, "parameters": {**fn["parameters"], "properties": props}}}

    @classmethod
    def from_history(cls, history: list[dict]) -> "FileTools | None":
        found: dict[str, dict] = {}
        for m in history:
            for a in m.get("attachments") or []:
                if a.get("kind") == "text":
                    found.pop(a["name"], None)
                    found[a["name"]] = a
        return cls(list(found.values())) if found else None

    @staticmethod
    def _out(text: str, name: str, status: str, detail: str) -> ToolOutcome:
        return ToolOutcome(text, None, {"kind": "file", "name": name, "status": status,
                                        "detail": detail}, None)

    def _err(self, name: str, msg: str) -> ToolOutcome:
        return self._out(msg, name, "error", msg)

    async def run(self, args: dict) -> ToolOutcome:
        args = args if isinstance(args, dict) else {}
        name = args.get("name")
        name = name if isinstance(name, str) else ""
        if name not in self.files:
            return self._err(name, f"No attached file named {name!r}. Valid names: "
                                   f"{', '.join(self.names)}")
        text = self.files[name]
        total = line_count(text)
        lines = [ln.removesuffix("\r") for ln in text.split("\n")[:total]]
        start, end = args.get("start_line"), args.get("end_line")
        start, end = 1 if start is None else start, total if end is None else end
        ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end))
        if not ok:
            return self._err(name, "start_line and end_line must be integers.")
        end = min(end, total)
        if start < 1 or start > total or end < start:
            return self._err(name, f"Invalid line range; {name} has {total} lines.")
        out, size, last = [], 0, start - 1
        for i in range(start, end + 1):
            if out and size + len(lines[i - 1]) + 1 > OPEN_FILE_CHARS:
                break
            out.append(lines[i - 1][:OPEN_FILE_CHARS])  # one huge line (minified) is cut
            size += len(out[-1]) + 1
            last = i
        text = f"{name} lines {start}-{last} of {total}:\n" + "\n".join(out)
        if last < end:
            text += f"\n[more lines remain — ask with start_line={last + 1}]"
        return self._out(text, name, "done", f"lines {start}-{last} of {total}")
