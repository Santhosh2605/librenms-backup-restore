"""Live API contract tests against a running LibreNMS.

These are skipped unless a reachable instance is configured, so the suite stays
green on a bare checkout:

    export BR_TEST_BASE_URL=https://librenms.example.test
    export BR_TEST_USER=admin
    export BR_TEST_PASS='...'
    pytest tests/integration -v

They assert the *contract* the GUI depends on: envelope shape, resource keys,
status codes and the security posture (no secrets in payloads, CSRF enforced,
authentication required). Route bugs of the kind that produced the 404s in
development are exactly what these catch.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

BASE = os.environ.get("BR_TEST_BASE_URL", "").rstrip("/")
USER = os.environ.get("BR_TEST_USER", "")
PASSWORD = os.environ.get("BR_TEST_PASS", "")

pytestmark = pytest.mark.skipif(
    not (BASE and USER and PASSWORD),
    reason="set BR_TEST_BASE_URL, BR_TEST_USER, BR_TEST_PASS to run integration tests",
)

API = "/plugin-backuprestore.php"
PLUGIN = "/plugin/BackupRestore"


class Session:
    """Minimal cookie-keeping client (no third-party dependency)."""

    def __init__(self, base: str):
        self.base = base
        self.cookies: dict[str, str] = {}

    def _request(self, method: str, path: str, data: dict | None = None,
                 headers: dict | None = None):
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if cookie:
            req.add_header("Cookie", cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._store(resp)
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self._store(exc)
            return exc.code, exc.read().decode("utf-8", "replace")

    def _store(self, response) -> None:
        for raw in response.headers.get_all("Set-Cookie") or []:
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.cookies[name.strip()] = value.strip()

    def login(self) -> None:
        _status, html = self._request("GET", "/login")
        match = re.search(r'name="_token"\s+value="([^"]+)"', html)
        assert match, "login page did not contain a CSRF token"
        status, _ = self._request("POST", "/login", {
            "_token": match.group(1), "username": USER, "password": PASSWORD,
        })
        assert status in (200, 302), f"login failed with {status}"

    def api(self, resource: str = "", method: str = "GET", payload: dict | None = None):
        query = f"?r={urllib.parse.quote(resource)}" if resource else ""
        data = payload
        headers = {}
        if method != "GET":
            headers["X-CSRF-TOKEN"] = self.csrf
        status, body = self._request(method, API + query, data, headers)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body

    @property
    def csrf(self) -> str:
        return urllib.parse.unquote(self.cookies.get("XSRF-TOKEN", ""))

    def page(self) -> tuple[int, str]:
        return self._request("GET", PLUGIN)


@pytest.fixture(scope="module")
def session():
    client = Session(BASE)
    client.login()
    return client


# -- transport & auth -------------------------------------------------------

def test_plugin_page_renders(session):
    status, html = session.page()
    assert status == 200
    assert "BackupRestore" in html or "backup-restore" in html


def test_assets_are_served(session):
    for asset, mime in (("backup-restore.js", "javascript"),
                        ("backup-restore.css", "css")):
        status, body = session._request("GET", f"{API}?r=assets/{asset}")
        assert status == 200, f"{asset} -> {status}"
        assert len(body) > 500, f"{asset} looks empty"


def test_unauthenticated_access_is_refused():
    anonymous = Session(BASE)
    status, _body = anonymous.api("status")
    assert status in (401, 403, 302), status


# -- every documented resource ---------------------------------------------

RESOURCES = ["status", "backups", "jobs", "restores", "schedules",
             "settings", "audit", "worker/health"]


@pytest.mark.parametrize("resource", RESOURCES)
def test_resource_returns_ok_envelope(session, resource):
    status, payload = session.api(resource)
    assert status == 200, f"{resource} -> {status}"
    assert isinstance(payload, dict), resource
    assert payload.get("status") == "ok", payload
    assert "data" in payload


def test_status_totals_have_expected_shape(session):
    _status, payload = session.api("status")
    totals = payload["data"]["totals"]
    for key in ("backups", "verified", "failed", "storage_bytes"):
        assert key in totals, key
        assert isinstance(totals[key], int)


def test_backups_items_are_fully_populated(session):
    _status, payload = session.api("backups")
    for item in payload["data"]["items"]:
        for key in ("backup_ref", "type", "status", "verification",
                    "size", "size_human", "created_at", "components"):
            assert key in item, (item.get("backup_ref"), key)


def test_worker_health_reports_helper_and_checks(session):
    _status, payload = session.api("worker/health")
    worker = payload["data"]["worker"]
    assert "helper" in worker
    assert isinstance(worker.get("checks", {}), dict)


def test_settings_never_leak_secrets(session):
    _status, payload = session.api("settings")
    blob = json.dumps(payload).lower()
    for needle in ("password", "secret", "passphrase", "private key", "age_secret"):
        assert needle not in blob, f"settings payload contains {needle!r}"


def test_audit_entries_are_structured(session):
    _status, payload = session.api("audit")
    for entry in payload["data"]["items"][:50]:
        assert "event" in entry or "action" in entry, entry


# -- input validation -------------------------------------------------------

def test_traversal_in_resource_is_rejected(session):
    for hostile in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "assets/../../x"):
        status, _payload = session.api(hostile)
        assert status in (400, 404), f"{hostile} -> {status}"


def test_unknown_resource_is_404_not_500(session):
    status, _payload = session.api("does-not-exist")
    assert status == 404


def test_unknown_backup_reference_is_404(session):
    status, payload = session.api("backups/br-19700101T000000Z-zzzzzz")
    assert status == 404
    assert payload.get("status") == "error"


def test_restore_requires_typed_confirmation(session):
    status, payload = session.api(
        "restores", method="POST",
        payload={"backup_ref": "br-19700101T000000Z-zzzzzz", "confirm": "yes"},
    )
    # Wrong phrase is rejected before the reference is even looked up.
    assert status in (403, 419, 422), status
    assert payload.get("status") == "error"


def test_state_change_without_csrf_is_rejected(session):
    client = Session(BASE)
    client.login()
    client.cookies.pop("XSRF-TOKEN", None)
    status, _body = client._request("POST", API + "?r=schedules", {"name": "x"})
    assert status in (419, 403), status
