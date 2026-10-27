"""Fit a chat into the model's context window.

Gemma reads at most ~16k tokens per request, including its thinking and answer.
Before each reply we:
  1. stop re-sending images older than the last few user turns,
  2. keep the newest messages that fit the budget (the latest one always),
  3. replace the dropped older part with a short summary, made once and stored
     with the chat (summaries build on the previous one as the chat grows).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .attachments import to_llm

IMAGE_TOKENS = 300  # rough cost of one image for Gemma's vision encoder
MSG_OVERHEAD = 8  # chat-template tokens around each message
SUMMARY_RESERVE = 600  # room kept for the summary once trimming starts
KEEP_IMAGE_TURNS = 2


def message_text(m: dict) -> str:
    """The text of a stored message as the model sees it (files included)."""
    llm = to_llm({**m, "attachments": [a for a in m.get("attachments", []) if a["kind"] != "image"]})
    content = llm["content"]
    return content if isinstance(content, str) else content[0]["text"]


def strip_old_images(history: list[dict], keep_turns: int = KEEP_IMAGE_TURNS) -> tuple[list[dict], int]:
    """Copies of `history` where images older than the last `keep_turns` user
    turns are replaced by a short note. Returns (messages, images hidden)."""
    user_idx = [i for i, m in enumerate(history) if m["role"] == "user"]
    recent = set(user_idx[-keep_turns:]) if keep_turns else set()
    out, hidden = [], 0
    for i, m in enumerate(history):
        atts = m.get("attachments")
        if atts and i not in recent and any(a["kind"] == "image" for a in atts):
            new = []
            for a in atts:
                if a["kind"] == "image":
                    new.append({"kind": "image_hidden", "name": a["name"]})
                    hidden += 1
                else:
                    new.append(a)
            m = {**m, "attachments": new}
        out.append(m)
    return out, hidden


def cut_index(costs: list[int], budget: int) -> int:
    """Index of the first message to keep so the kept tail fits `budget`.
    The last message is always kept, even if it alone is over budget."""
    total = 0
    for i in range(len(costs) - 1, -1, -1):
        total += costs[i]
        if total > budget:
            return min(i + 1, len(costs) - 1)
    return 0


@dataclass
class Plan:
    kept: list[dict]
    dropped: list[dict]
    images_hidden: int
    summary: str | None = None
    # (previous summary or None, messages it doesn't cover yet) when a new one is needed
    needs_summary: tuple[str | None, list[dict]] | None = None
    first_kept_id: str | None = field(default=None)

    def final_messages(self) -> list[dict]:
        msgs = [to_llm(m) for m in self.kept]
        if self.dropped and self.summary:
            msgs.insert(0, {
                "role": "system",
                "content": "Summary of the earlier part of this conversation. Those older messages "
                "are no longer shown to you:\n" + self.summary,
            })
        return msgs


async def plan(
    history: list[dict],
    *,
    count: Callable[[dict], Awaitable[int]],
    budget: int,
    summaries: dict[str, str],
) -> Plan:
    msgs, hidden = strip_old_images(history)
    costs = [await count(m) + MSG_OVERHEAD for m in msgs]
    i = cut_index(costs, budget)
    if i > 0:
        i = cut_index(costs, budget - SUMMARY_RESERVE)
        # the kept part must start with a user turn (chat templates need user first)
        while i < len(msgs) - 1 and msgs[i]["role"] != "user":
            i += 1
    p = Plan(kept=msgs[i:], dropped=msgs[:i], images_hidden=hidden, first_kept_id=msgs[i].get("id") if msgs else None)
    if not p.dropped:
        return p
    # newest stored summary that covers a prefix of what we're dropping
    covered = max((j for j, m in enumerate(p.dropped) if m.get("id") in summaries), default=None)
    if covered == len(p.dropped) - 1:
        p.summary = summaries[p.dropped[-1]["id"]]
    elif covered is None:
        p.needs_summary = (None, p.dropped)
    else:
        p.needs_summary = (summaries[p.dropped[covered]["id"]], p.dropped[covered + 1 :])
    return p


TRANSCRIPT_MSG_CHARS = 2_000
TRANSCRIPT_CHARS = 30_000


def summary_prompt(previous: str | None, messages: list[dict]) -> list[dict]:
    lines = []
    for m in messages:
        who = "User" if m["role"] == "user" else "Assistant"
        text = message_text(m)
        if len(text) > TRANSCRIPT_MSG_CHARS:
            text = text[:TRANSCRIPT_MSG_CHARS] + " […]"
        lines.append(f"{who}: {text}")
    transcript = "\n\n".join(lines)[-TRANSCRIPT_CHARS:]
    task = (
        "Write a short summary (under 200 words) of the conversation below, so it can replace "
        "these messages later. Keep names, numbers, facts, decisions and open questions. "
        "Write it as plain notes, no preamble."
    )
    if previous:
        task += "\n\nIt continues from this earlier summary; merge both into one:\n" + previous
    return [{"role": "user", "content": task + "\n\n---\n" + transcript}]
