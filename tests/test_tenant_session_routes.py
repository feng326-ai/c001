"""HTTP-route and cookie contract for dormant tenant-session binding."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import Response

from wxsearch.api import auth, main as api_main, tenant_session
from wxsearch.db_connector import (
    TenantAccessDenied,
    TenantChoice,
    TenantPrincipal,
)


NOW = 2_000_000_000
SAFE_SECRET = "qa-tenant-route-secret-" + "b" * 40


class FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = dict(cookies or {})


class FakeTenantDb:
    def __init__(self, choices=(), *, denied=False, principal=None):
        self.choices = list(choices)
        self.denied = denied
        self.principal = principal
        self.list_calls = []
        self.transaction_calls = []

    def list_active_tenants(self, *, authenticated_user_id):
        self.list_calls.append(authenticated_user_id)
        return list(self.choices)

    @contextmanager
    def tenant_transaction(self, *, authenticated_user_id, requested_tenant_id):
        self.transaction_calls.append(
            (authenticated_user_id, uuid.UUID(str(requested_tenant_id)))
        )
        if self.denied:
            raise TenantAccessDenied("tenant access denied")
        yield SimpleNamespace(principal=self.principal)


def _configure(monkeypatch, *, binding=True, secure=False):
    monkeypatch.setattr(auth, "SESSION_SECRET", SAFE_SECRET)
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", 3600)
    monkeypatch.setattr(auth.time, "time", lambda: NOW)
    monkeypatch.setenv("TENANT_SCOPE_MAX_AGE", "600")
    monkeypatch.setenv(
        "TENANT_SESSION_BINDING_ENABLED", "true" if binding else "false"
    )
    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "false")
    monkeypatch.setenv("TENANT_REVIEW_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true" if secure else "false")


def _current_user(user_id=17):
    return {
        "id": user_id,
        "username": f"qa-user-{user_id}",
        "role": "member",
        "team_id": None,
        "team_name": "",
    }


def _choice(tenant_id=None, *, role="sales"):
    return TenantChoice(
        tenant_id=tenant_id or uuid.uuid4(),
        code="tenant-a",
        name="Tenant A",
        membership_id=uuid.uuid4(),
        role=role,
    )


def _login_and_scope(
    *, user_id=17, tenant_id=None, session_id="a" * 32
):
    login_token = auth.make_session_token(user_id)
    scope_token = auth.make_tenant_scope_token(
        login_token,
        user_id,
        tenant_id,
        now=NOW,
        session_id=session_id,
    )
    return login_token, scope_token


def _cookie_headers(response):
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


def _cookie_value(response, cookie_name):
    for header in _cookie_headers(response):
        parsed = SimpleCookie()
        parsed.load(header)
        if cookie_name in parsed:
            return parsed[cookie_name].value
    return None


def test_v2_session_routes_have_only_frozen_methods_and_are_registered_once():
    expected = {
        "/api/v2/session": {"GET"},
        "/api/v2/session/tenants": {"GET"},
        "/api/v2/session/tenant": {"PUT", "DELETE"},
    }
    router_methods = {}
    for route in tenant_session.router.routes:
        router_methods.setdefault(route.path, set()).update(route.methods)
    assert router_methods == expected

    openapi_paths = api_main.app.openapi()["paths"]
    app_methods = {
        path: {method.upper() for method in openapi_paths[path]}
        for path in expected
    }
    assert app_methods == expected
    assert sum(
        getattr(route, "original_router", None) is tenant_session.router
        for route in api_main.app.routes
    ) == 1
    assert not any(
        "review" in getattr(route, "path", "").lower()
        for route in api_main.app.routes
        if getattr(route, "path", "").startswith("/api/v2/")
    )


@pytest.mark.parametrize(
    "endpoint",
    (
        "get_tenant_session",
        "list_tenant_choices",
        "bind_tenant_scope",
        "unbind_tenant_scope",
    ),
)
def test_all_v2_session_routes_are_404_when_binding_flag_is_off(
    monkeypatch, endpoint
):
    _configure(monkeypatch, binding=False)
    request = FakeRequest()
    response = Response()
    user = _current_user()

    with pytest.raises(HTTPException) as exc_info:
        if endpoint == "get_tenant_session":
            tenant_session.get_tenant_session(request, response, user)
        elif endpoint == "list_tenant_choices":
            tenant_session.list_tenant_choices(request, response, user)
        elif endpoint == "bind_tenant_scope":
            tenant_session.bind_tenant_scope(
                tenant_session.TenantSelectionRequest(tenant_id=uuid.uuid4()),
                request,
                response,
                None,
                user,
            )
        else:
            tenant_session.unbind_tenant_scope(
                request, response, None, user
            )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "not_found"
    assert _cookie_headers(response) == []


def test_selection_payload_rejects_extra_redirect_or_identity_fields():
    for extra in (
        {"next": "https://attacker.example"},
        {"return_url": "//attacker.example"},
        {"user_id": 99},
        {"role": "tenant_admin"},
    ):
        with pytest.raises(ValidationError):
            tenant_session.TenantSelectionRequest(
                tenant_id=uuid.uuid4(), **extra
            )


def test_get_missing_scope_bootstraps_unbound_cookie_and_lists_active_choices(
    monkeypatch,
):
    _configure(monkeypatch)
    choice = _choice()
    fake_db = FakeTenantDb([choice])
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    login_token = auth.make_session_token(17)
    request = FakeRequest({auth.COOKIE_NAME: login_token})
    response = Response()

    state = tenant_session.get_tenant_session(
        request, response, _current_user()
    )

    assert state["status"] == "unbound"
    assert state["tenant"] is None
    assert state["tenants"] == [
        {
            "tenant_id": str(choice.tenant_id),
            "code": choice.code,
            "name": choice.name,
            "role": choice.role,
        }
    ]
    assert fake_db.list_calls == [17]
    scope_token = _cookie_value(response, auth.TENANT_COOKIE_NAME)
    claims = auth.parse_tenant_scope_token(
        scope_token, login_token, 17, now=NOW
    )
    assert claims is not None and claims.tenant_id is None
    assert state["csrf_token"] == auth.tenant_scope_csrf_token(scope_token)
    header = next(
        value
        for value in _cookie_headers(response)
        if value.startswith(f"{auth.TENANT_COOKIE_NAME}=")
    )
    assert "HttpOnly" in header
    assert "Path=/" in header
    assert "SameSite=lax" in header
    assert "Max-Age=3600" in header


def test_get_reports_unmapped_without_breaking_legacy_login(monkeypatch):
    _configure(monkeypatch)
    fake_db = FakeTenantDb([])
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    login_token = auth.make_session_token(17)
    response = Response()

    state = tenant_session.get_tenant_session(
        FakeRequest({auth.COOKIE_NAME: login_token}),
        response,
        _current_user(),
    )

    assert state["status"] == "unmapped"
    assert state["tenant"] is None
    assert state["tenants"] == []
    assert _cookie_value(response, auth.TENANT_COOKIE_NAME)
    assert _cookie_value(response, auth.COOKIE_NAME) is None


def test_get_active_scope_uses_fresh_choice_and_stale_scope_is_unbound(
    monkeypatch,
):
    _configure(monkeypatch)
    selected = _choice(role="resource_reviewer")
    login_token, active_scope = _login_and_scope(
        tenant_id=selected.tenant_id
    )
    fake_db = FakeTenantDb([selected])
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    response = Response()

    state = tenant_session.get_tenant_session(
        FakeRequest(
            {
                auth.COOKIE_NAME: login_token,
                auth.TENANT_COOKIE_NAME: active_scope,
            }
        ),
        response,
        _current_user(),
    )
    assert state["status"] == "active"
    assert state["tenant"]["role"] == "resource_reviewer"
    assert _cookie_headers(response) == []

    stale_db = FakeTenantDb([])
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: stale_db)
    stale_response = Response()
    stale_state = tenant_session.get_tenant_session(
        FakeRequest(
            {
                auth.COOKIE_NAME: login_token,
                auth.TENANT_COOKIE_NAME: active_scope,
            }
        ),
        stale_response,
        _current_user(),
    )
    assert stale_state["status"] == "stale"
    replacement = _cookie_value(stale_response, auth.TENANT_COOKIE_NAME)
    replacement_claims = auth.parse_tenant_scope_token(
        replacement, login_token, 17, now=NOW
    )
    assert replacement_claims is not None
    assert replacement_claims.tenant_id is None


def test_bind_requires_same_scope_csrf_and_uses_tenant_transaction(monkeypatch):
    _configure(monkeypatch)
    tenant_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    login_token, bootstrap_scope = _login_and_scope(session_id="1" * 32)
    principal = TenantPrincipal(
        user_id=17,
        user_public_id=uuid.uuid4(),
        tenant_id=tenant_id,
        membership_id=membership_id,
        role="resource_reviewer",
    )
    fake_db = FakeTenantDb(principal=principal)
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    request = FakeRequest(
        {
            auth.COOKIE_NAME: login_token,
            auth.TENANT_COOKIE_NAME: bootstrap_scope,
        }
    )
    response = Response()

    result = tenant_session.bind_tenant_scope(
        tenant_session.TenantSelectionRequest(tenant_id=tenant_id),
        request,
        response,
        auth.tenant_scope_csrf_token(bootstrap_scope),
        _current_user(),
    )

    assert fake_db.transaction_calls == [(17, tenant_id)]
    assert result["status"] == "active"
    assert result["tenant"] == {
        "tenant_id": str(tenant_id),
        "role": "resource_reviewer",
    }
    bound_scope = _cookie_value(response, auth.TENANT_COOKIE_NAME)
    claims = auth.parse_tenant_scope_token(
        bound_scope, login_token, 17, now=NOW
    )
    assert claims is not None and claims.tenant_id == tenant_id
    assert result["csrf_token"] == auth.tenant_scope_csrf_token(bound_scope)
    assert membership_id.hex not in bound_scope
    assert "resource_reviewer" not in bound_scope


def test_bind_rejects_cross_session_csrf_before_database_call(monkeypatch):
    _configure(monkeypatch)
    login_token, request_scope = _login_and_scope(session_id="1" * 32)
    _login_token, other_scope = _login_and_scope(session_id="2" * 32)
    fake_db = FakeTenantDb()
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        tenant_session.bind_tenant_scope(
            tenant_session.TenantSelectionRequest(tenant_id=uuid.uuid4()),
            FakeRequest(
                {
                    auth.COOKIE_NAME: login_token,
                    auth.TENANT_COOKIE_NAME: request_scope,
                }
            ),
            response,
            auth.tenant_scope_csrf_token(other_scope),
            _current_user(),
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "csrf_invalid"
    assert fake_db.transaction_calls == []
    assert _cookie_headers(response) == []


def test_bind_denial_is_uniform_404_and_does_not_replace_scope(monkeypatch):
    _configure(monkeypatch)
    login_token, bootstrap_scope = _login_and_scope()
    fake_db = FakeTenantDb(denied=True)
    monkeypatch.setattr(tenant_session, "DatabaseConnector", lambda: fake_db)
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        tenant_session.bind_tenant_scope(
            tenant_session.TenantSelectionRequest(tenant_id=uuid.uuid4()),
            FakeRequest(
                {
                    auth.COOKIE_NAME: login_token,
                    auth.TENANT_COOKIE_NAME: bootstrap_scope,
                }
            ),
            response,
            auth.tenant_scope_csrf_token(bootstrap_scope),
            _current_user(),
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "tenant_not_available"
    assert _cookie_headers(response) == []


def test_unbind_requires_csrf_and_rotates_to_new_unbound_scope(monkeypatch):
    _configure(monkeypatch)
    tenant_id = uuid.uuid4()
    login_token, active_scope = _login_and_scope(
        tenant_id=tenant_id, session_id="3" * 32
    )
    request = FakeRequest(
        {
            auth.COOKIE_NAME: login_token,
            auth.TENANT_COOKIE_NAME: active_scope,
        }
    )
    response = Response()

    result = tenant_session.unbind_tenant_scope(
        request,
        response,
        auth.tenant_scope_csrf_token(active_scope),
        _current_user(),
    )

    replacement = _cookie_value(response, auth.TENANT_COOKIE_NAME)
    assert replacement and replacement != active_scope
    claims = auth.parse_tenant_scope_token(
        replacement, login_token, 17, now=NOW
    )
    assert claims is not None and claims.tenant_id is None
    assert claims.session_id != "3" * 32
    assert result == {
        "status": "unbound",
        "csrf_token": auth.tenant_scope_csrf_token(replacement),
    }


def test_login_rotates_or_clears_scope_and_logout_clears_both_cookies(
    monkeypatch,
):
    _configure(monkeypatch, binding=True, secure=True)
    monkeypatch.setattr(
        api_main,
        "authenticate",
        lambda _username, _password: _current_user(),
    )

    login_response = asyncio.run(
        api_main.login_submit(
            api_main.LoginReq(username="qa-user", password="password")
        )
    )
    login_token = _cookie_value(login_response, auth.COOKIE_NAME)
    scope_token = _cookie_value(login_response, auth.TENANT_COOKIE_NAME)
    assert auth.parse_session_token(login_token) == 17
    claims = auth.parse_tenant_scope_token(
        scope_token, login_token, 17, now=NOW
    )
    assert claims is not None and claims.tenant_id is None
    for header in _cookie_headers(login_response):
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=lax" in header
        assert "Path=/" in header

    logout_response = asyncio.run(api_main.logout())
    logout_headers = _cookie_headers(logout_response)
    assert any(header.startswith(f"{auth.COOKIE_NAME}=") for header in logout_headers)
    assert any(
        header.startswith(f"{auth.TENANT_COOKIE_NAME}=")
        for header in logout_headers
    )
    assert all("Max-Age=0" in header for header in logout_headers)
    assert all("Secure" in header and "Path=/" in header for header in logout_headers)

    _configure(monkeypatch, binding=False, secure=False)
    disabled_response = asyncio.run(
        api_main.login_submit(
            api_main.LoginReq(username="qa-user", password="password")
        )
    )
    disabled_scope_header = next(
        header
        for header in _cookie_headers(disabled_response)
        if header.startswith(f"{auth.TENANT_COOKIE_NAME}=")
    )
    assert "Max-Age=0" in disabled_scope_header
