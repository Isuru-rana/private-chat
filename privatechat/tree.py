"""Chats as a tree of messages, so edits and regenerated replies keep old versions.

Each message has an `id` and a `parent` (None for the first). A chat's `head` is
the message the conversation currently ends at; the visible conversation is the
path from the root to the head. Messages sharing a parent are versions of each
other, kept in creation order (list order).
"""


def path_to(messages: list[dict], head: str | None) -> list[dict]:
    by_id = {m["id"]: m for m in messages}
    out = []
    node = by_id.get(head)
    while node is not None:
        out.append(node)
        node = by_id.get(node["parent"])
    return out[::-1]


def siblings(messages: list[dict], msg_id: str) -> list[dict]:
    parent = next(m["parent"] for m in messages if m["id"] == msg_id)
    return [m for m in messages if m["parent"] == parent]


def latest_leaf(messages: list[dict], msg_id: str) -> str:
    """Follow the newest child at each step down to the end of that branch."""
    while True:
        children = [m for m in messages if m["parent"] == msg_id]
        if not children:
            return msg_id
        msg_id = children[-1]["id"]


def annotate_path(messages: list[dict], head: str | None) -> list[dict]:
    """The visible conversation, each message tagged with its version info."""
    out = []
    for m in path_to(messages, head):
        sibs = [s["id"] for s in siblings(messages, m["id"])]
        item = {k: v for k, v in m.items() if k != "parent"}
        item.update(sibling_ids=sibs, versions=len(sibs), version=sibs.index(m["id"]) + 1)
        out.append(item)
    return out
