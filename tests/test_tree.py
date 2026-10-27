from privatechat.tree import annotate_path, latest_leaf, path_to, siblings


def m(id, parent, role="user", content=""):
    return {"id": id, "parent": parent, "role": role, "content": content or id}


# a1 -> b1 -> c1          (original)
#    -> b2 -> c2 -> d2    (edited b, newer)
MSGS = [
    m("a1", None),
    m("b1", "a1", "assistant"),
    m("c1", "b1"),
    m("b2", "a1", "assistant"),
    m("c2", "b2"),
    m("d2", "c2", "assistant"),
]


def test_path_to_walks_root_first():
    assert [x["id"] for x in path_to(MSGS, "d2")] == ["a1", "b2", "c2", "d2"]
    assert [x["id"] for x in path_to(MSGS, "c1")] == ["a1", "b1", "c1"]


def test_path_to_none_is_empty():
    assert path_to(MSGS, None) == []


def test_siblings_in_creation_order():
    assert [x["id"] for x in siblings(MSGS, "b2")] == ["b1", "b2"]
    assert [x["id"] for x in siblings(MSGS, "a1")] == ["a1"]


def test_latest_leaf_follows_newest_child():
    assert latest_leaf(MSGS, "a1") == "d2"
    assert latest_leaf(MSGS, "b1") == "c1"
    assert latest_leaf(MSGS, "d2") == "d2"


def test_annotate_path_adds_version_info():
    out = annotate_path(MSGS, "c1")
    assert [x["id"] for x in out] == ["a1", "b1", "c1"]
    b = out[1]
    assert b["versions"] == 2 and b["version"] == 1 and b["sibling_ids"] == ["b1", "b2"]
    assert out[0]["versions"] == 1 and out[0]["version"] == 1
    assert "parent" not in out[0]
