import os
import stat

from test_api import clock, make_client  # noqa: F401 (clock is a fixture)


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
