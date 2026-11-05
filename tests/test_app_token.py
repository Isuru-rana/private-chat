import pytest
from fastapi.testclient import TestClient

from privatechat.app import create_app
from privatechat.crypto import FAST_KDF
from test_api import APP_TOKEN, PW, clock, fake_llm, make_client, slots_ok, unlocked  # noqa: F401 (clock is a fixture)


def bare(c):
    """Same app, but a client that sends no X-App-Token by default."""
    return TestClient(c.app)


@pytest.mark.parametrize("headers", [{}, {"X-App-Token": ""}, {"X-App-Token": "wrong"},
                                     {"X-App-Token": APP_TOKEN + "x"}])
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/chats", None), ("POST", "/api/chats", {"title": "t"}), ("GET", "/api/vault", None),
    ("POST", "/api/vault/create", {"password": PW}), ("POST", "/api/vault/unlock", {"password": PW}),
    ("POST", "/api/vault/lock", None), ("POST", "/api/vault/wipe", {"confirm": "WIPE"}),
    ("POST", "/api/web/approve", {"id": "x", "allow": True}), ("GET", "/api/secret/chats", None),
])
def test_api_refused_without_the_right_token(tmp_path, clock, headers, method, path, body):
    with make_client(tmp_path, clock) as c:
        r = bare(c).request(method, path, json=body, headers=headers)
        assert r.status_code == 401 and r.json() == {"error": "app_token"}
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/api/vault").json() == {"exists": False, "unlocked": False}
        assert c.get("/api/chats").json() == []


def test_api_works_with_the_token(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        assert c.post("/api/chats", json={"title": "t"}).status_code == 200
        assert unlocked(c)


def test_page_and_static_files_need_no_token(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        for path in ("/", "/static/app.js", "/static/style.css"):
            assert bare(c).get(path).status_code == 200


def test_lock_with_token_and_keepalive_style_request(tmp_path, clock):
    # what the page's pagehide handler sends: a same-origin POST with the token and no body
    with make_client(tmp_path, clock) as c:
        unlocked(c)
        assert c.get("/api/vault").json()["unlocked"] is True
        r = c.post("/api/vault/lock", headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200
        assert c.get("/api/vault").json()["unlocked"] is False


def test_lock_without_token_leaves_state_alone(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        unlocked(c)
        assert bare(c).post("/api/vault/lock").status_code == 401
        assert c.get("/api/vault").json()["unlocked"] is True


def test_random_token_when_none_given(tmp_path, clock):
    a = create_app(tmp_path / "a", kdf=FAST_KDF, llm=fake_llm, clock=clock, slots_check=slots_ok,
                   allowed_hosts=["testserver"])
    b = create_app(tmp_path / "b", kdf=FAST_KDF, llm=fake_llm, clock=clock, slots_check=slots_ok,
                   allowed_hosts=["testserver"])
    assert len(a.state.app_token) >= 32 and a.state.app_token != b.state.app_token
    with TestClient(a) as c:
        assert c.get("/api/chats").status_code == 401
        assert c.get("/api/chats", headers={"X-App-Token": a.state.app_token}).status_code == 200
