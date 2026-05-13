from fastapi.testclient import TestClient

import main
from auth import COOKIE_NAME, make_session_token


def test_unauth_redirects_to_login():
    with TestClient(main.app) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_login_wrong_password():
    with TestClient(main.app) as client:
        r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert r.status_code == 303
        assert "/login?error=1" in r.headers["location"]


def test_login_right_password_sets_cookie():
    with TestClient(main.app) as client:
        r = client.post("/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303
        assert COOKIE_NAME in r.cookies


def test_healthz_is_public():
    with TestClient(main.app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_authed_automation_loads():
    with TestClient(main.app) as client:
        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.get("/automation")
        assert r.status_code == 200
        assert "Automation" in r.text
