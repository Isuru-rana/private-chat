import os
import stat

from test_api import clock, make_client, unlocked  # noqa: F401 (clock is a fixture)


def test_security_headers_on_pages_api_and_refusals(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        for r in (c.get("/"), c.get("/api/vault"), c.get("/static/app.js"),
                  c.post("/api/chats", json={}, headers={"Origin": "http://evil.example"})):
            assert r.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
            assert "script-src 'self'" in r.headers["content-security-policy"]
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"


def test_oversized_body_refused_before_parsing(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        chat = c.post("/api/chats", json={"title": "t"}).json()
        r = c.post(f"/api/chats/{chat['id']}/messages", content=b"{}",
                   headers={"Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)})
        assert r.status_code == 413
        assert c.get(f"/api/chats/{chat['id']}").json()["messages"] == []


def test_chunked_body_refused(tmp_path, clock):
    def gen():
        yield b'{"password": "a strong vault password"}'

    with make_client(tmp_path, clock) as c:
        r = c.post("/api/vault/create", content=gen(), headers={"Content-Type": "application/json"})
        assert r.status_code == 411
        assert c.get("/api/vault").json()["exists"] is False


def test_normal_sized_requests_still_work(tmp_path, clock):
    with make_client(tmp_path, clock) as c:
        assert unlocked(c)
