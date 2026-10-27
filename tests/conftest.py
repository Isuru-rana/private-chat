def plain(messages):
    """Strip version metadata so tests can compare conversation content."""
    keep = ("role", "content", "attachments")
    return [{k: m[k] for k in keep if k in m} for m in messages]
