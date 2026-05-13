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
        assert "Active rules" in r.text


def test_schedule_with_filters_in_url():
    with TestClient(main.app) as client:
        client.cookies.set(COOKIE_NAME, make_session_token())
        r = client.get("/schedule?locations=Havenweg+6&categories=Classic+CrossFit")
        assert r.status_code == 200
        assert "Schedule" in r.text


def test_split_csv_helper():
    assert main._split_csv(None) == []
    assert main._split_csv("") == []
    assert main._split_csv("a, b,c") == ["a", "b", "c"]


def test_monday_of_helper():
    from datetime import date
    assert main._monday_of(date(2026, 5, 13)).weekday() == 0  # Wed -> previous Mon
    assert main._monday_of(date(2026, 5, 11)) == date(2026, 5, 11)  # already Mon
