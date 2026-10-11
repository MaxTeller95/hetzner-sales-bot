"""Smoke tests for the admin dashboard: auth, CSRF and every page rendering."""
import re

import pytest
from fastapi.testclient import TestClient

from app.web.main import app

PAGES = ["/", "/users", "/servers", "/servers?status=all", "/topups", "/plans", "/catalog", "/providers",
         "/pool", "/abuse", "/settings", "/jobs", "/transactions", "/ips", "/api/stats"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(client) -> str:
    r = client.post("/login", data={"username": "admin", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/settings").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_password(client):
    r = client.post("/login", data={"username": "admin", "password": "nope"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_all_pages_render(client):
    login(client)
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:500])


def test_csrf_required_and_settings_save(client):
    csrf = login(client)
    assert client.post("/settings", data={"exchange_rate_eur": "123"}).status_code == 403
    r = client.post("/settings", data={"csrf": csrf, "exchange_rate_eur": "1200000", "profit_margin_percent": "35"}, follow_redirects=False)
    assert r.status_code == 303
    assert 'value="1200000"' in client.get("/settings").text


def test_plan_update_and_detail_pages(client):
    csrf = login(client)
    plans = client.get("/plans").text
    plan_id = re.search(r'action="/plans/(\d+)"', plans).group(1)
    r = client.post(f"/plans/{plan_id}", data={"csrf": csrf, "is_active": "1", "override_hourly": "0.005", "pool_target": "0", "sort_order": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "saved" in r.text
