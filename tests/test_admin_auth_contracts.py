"""Admin authentication and CSRF contracts through the actual Quart routes."""

import hashlib
import hmac
import json
import re
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

ADMIN = 71001
BOT_TOKEN = "71001:synthetic-admin-auth-test"
NOW = 1_800_000_000
POLICY = {"models": ["gemini-test"], "strategy": "sequential", "inherit_user_model": False}
CONTROL_BODY = {"section": "process", "id": "chat", "value": POLICY, "expected_revision": 0}


@pytest.fixture
def admin_client(monkeypatch):
    from app import web, web_controls, web_miniapp
    from app.runtime_settings.store import SettingsSnapshot

    monkeypatch.setattr(
        web,
        "settings",
        web.settings.model_copy(
            update={"ADMIN_SECRET": "synthetic-admin-secret", "ADMIN_ID": ADMIN, "TELEGRAM_BOT_TOKEN": BOT_TOKEN}
        ),
    )
    monkeypatch.setitem(web.quart_app.config, "TESTING", True)
    for limiter in (web._login_limiter, web._api_limiter):
        monkeypatch.setattr(limiter, "_requests", defaultdict(deque))
        monkeypatch.setattr(limiter, "_call_count", 0)
    monkeypatch.setattr(web_miniapp, "time", SimpleNamespace(time=lambda: NOW))
    effects = {}
    for owner, names in (
        (web_controls, ("set_value", "reset_value", "restore_revision", "save_aliases", "reset_aliases")),
        (web_controls.prompts, ("save_prompt", "reset_prompt", "refresh_prompts")),
        (web_controls.models, ("save_catalog", "reset_catalog", "save_limit", "reset_limit", "refresh_catalogs")),
    ):
        for name in names:
            spy = AsyncMock(return_value=SettingsSnapshot(1, {}))
            monkeypatch.setattr(owner, name, spy)
            effects[name] = spy
    return web.quart_app.test_client(), effects


def assert_no_mutation(effects):
    for spy in effects.values():
        spy.assert_not_awaited()


async def login_csrf(client):
    response = await client.get("/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([a-f0-9]+)"', (await response.get_data()).decode())
    assert match
    return match.group(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/admin/controls", "/api/admin/controls/restore", "/api/admin/controls/preview"])
@pytest.mark.parametrize("token", [None, "wrong-secret", "é"])
async def test_denied_admin_mutation_never_dispatches_writer(admin_client, path, token):
    client, effects = admin_client
    async with client.session_transaction() as session:
        session["controls_csrf"] = "test-csrf"
    headers = {"X-CSRF-Token": "test-csrf"}
    if token is not None:
        headers["X-Auth-Token"] = token
    body = {"revision": 0, "expected_revision": 0} if path.endswith("restore") else CONTROL_BODY
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 401
    assert await response.get_json() == {"error": "Unauthorized"}
    assert_no_mutation(effects)


@pytest.mark.asyncio
async def test_unicode_header_denies_controls_page_without_rendering(admin_client):
    client, effects = admin_client
    response = await client.get("/controls", headers={"X-Auth-Token": "é"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert_no_mutation(effects)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_field", ["password", "csrf_token"])
async def test_unicode_login_rejects_without_authenticated_session(admin_client, malformed_field):
    client, effects = admin_client
    form = {"password": "synthetic-admin-secret", "csrf_token": await login_csrf(client)}
    form[malformed_field] = "невірний-é"
    response = await client.post("/login", form=form)
    assert response.status_code == 200
    assert "synthetic-admin-secret" not in (await response.get_data()).decode()
    async with client.session_transaction() as session:
        assert not session.get("authenticated")
    response = await client.post("/api/admin/controls", json=CONTROL_BODY)
    assert response.status_code == 401
    assert_no_mutation(effects)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/admin/controls", "/api/admin/controls/restore", "/api/admin/controls/preview"])
@pytest.mark.parametrize("csrf", [None, "wrong-csrf", "é"])
async def test_authenticated_controls_csrf_denial_has_no_side_effect(admin_client, path, csrf):
    client, effects = admin_client
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    body = {"revision": 0, "expected_revision": 0} if path.endswith("restore") else CONTROL_BODY
    headers = {} if csrf is None else {"X-CSRF-Token": csrf}
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 403
    assert_no_mutation(effects)


def signed_init_data(user_id, auth_date):
    # Independent signer: no production validator/helper constructs the fixture.
    fields = {
        "auth_date": str(auth_date),
        "query_id": "synthetic-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity,age,tamper,status",
    [(ADMIN, 0, False, 200), (71002, 0, False, 401), (ADMIN, 86401, False, 401), (ADMIN, 0, True, 401)],
)
async def test_telegram_admin_role_and_signature_gate_real_controls_write(admin_client, identity, age, tamper, status):
    client, effects = admin_client
    async with client.session_transaction() as session:
        session["controls_csrf"] = "test-csrf"
    init_data = signed_init_data(identity, NOW - age)
    if tamper:
        init_data = init_data.replace("synthetic-query", "tampered-query")
    response = await client.post(
        "/api/admin/controls",
        json=CONTROL_BODY,
        headers={"Authorization": "tma " + init_data, "X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == status
    if status == 200:
        assert await response.get_json() == {"revision": 1}
        effects["set_value"].assert_awaited_once_with("process:chat", POLICY, expected_revision=0, actor="admin")
    else:
        assert await response.get_json() == {"error": "Unauthorized"}
        assert_no_mutation(effects)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,secret",
    [
        ("header", "synthetic-admin-secret"),
        ("header", "é-test-secret"),
        ("password", "synthetic-admin-secret"),
        ("password", "é-синтетичний"),
        ("session", "synthetic-admin-secret"),
    ],
)
async def test_valid_admin_auth_methods_still_allow_controls_write(admin_client, monkeypatch, secret, method):
    from app import web

    client, effects = admin_client
    monkeypatch.setattr(web, "settings", web.settings.model_copy(update={"ADMIN_SECRET": secret}))
    headers = {"X-CSRF-Token": "test-csrf"}
    if method == "password":
        response = await client.post("/login", form={"password": secret, "csrf_token": await login_csrf(client)})
        assert response.status_code == 302
        async with client.session_transaction() as session:
            assert session["authenticated"] is True
    elif method == "header":
        headers["X-Auth-Token"] = secret
    async with client.session_transaction() as session:
        session["controls_csrf"] = "test-csrf"
        if method == "session":
            session["authenticated"] = True
    # Quart's test builder UTF-8 encodes headers while its ASGI reader decodes
    # Latin-1. Supply raw Latin-1 wire bytes for the non-ASCII header positive.
    scope_base = None
    if method == "header" and secret == "é-test-secret":
        assert client.cookie_jar is not None
        cookie = "; ".join(f"{item.name}={item.value}" for item in client.cookie_jar)
        scope_base = {
            "headers": [
                (b"host", b"localhost"),
                (b"content-type", b"application/json"),
                (b"cookie", cookie.encode("ascii")),
                (b"x-csrf-token", b"test-csrf"),
                (b"x-auth-token", secret.encode("latin-1")),
            ]
        }
    response = await client.post("/api/admin/controls", json=CONTROL_BODY, headers=headers, scope_base=scope_base)
    assert response.status_code == 200
    assert await response.get_json() == {"revision": 1}
    effects["set_value"].assert_awaited_once_with("process:chat", POLICY, expected_revision=0, actor="admin")
