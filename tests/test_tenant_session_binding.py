"""Unit contract for the dormant tenant-session scope binding."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import HTTPException

from wxsearch.api import auth, tenant_session
from wxsearch.db_connector import TenantChoice


NOW = 2_000_000_000
SAFE_SECRET = "qa-tenant-session-secret-" + "a" * 40
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = dict(cookies or {})


class FakeDb:
    def __init__(self, choices=()):
        self.choices = list(choices)
        self.calls = []

    def list_active_tenants(self, *, authenticated_user_id):
        self.calls.append(authenticated_user_id)
        return list(self.choices)


def _configure(monkeypatch, *, max_age=600, binding=True):
    monkeypatch.setattr(auth, "SESSION_SECRET", SAFE_SECRET)
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", 3600)
    monkeypatch.setattr(auth.time, "time", lambda: NOW)
    monkeypatch.setenv("TENANT_SCOPE_MAX_AGE", str(max_age))
    monkeypatch.setenv(
        "TENANT_SESSION_BINDING_ENABLED", "true" if binding else "false"
    )
    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "false")
    monkeypatch.setenv("TENANT_REVIEW_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")


def _auth_token(user_id=17):
    return auth.make_session_token(user_id)


def _scope_token(
    monkeypatch,
    *,
    user_id=17,
    tenant_id=None,
    session_id="a" * 32,
    issued_at=NOW,
    max_age=600,
):
    _configure(monkeypatch, max_age=max_age)
    login_token = _auth_token(user_id)
    scope_token = auth.make_tenant_scope_token(
        login_token,
        user_id,
        tenant_id,
        now=issued_at,
        session_id=session_id,
    )
    return login_token, scope_token


def _choice(tenant_id=None, *, role="sales"):
    return TenantChoice(
        tenant_id=tenant_id or uuid.uuid4(),
        code="tenant-a",
        name="Tenant A",
        membership_id=uuid.uuid4(),
        role=role,
    )


def test_feature_flags_default_false_and_binding_can_be_enabled(monkeypatch):
    monkeypatch.setattr(auth, "SESSION_SECRET", SAFE_SECRET)
    monkeypatch.setenv("TENANT_SCOPE_MAX_AGE", "600")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in (
        "TENANT_SESSION_BINDING_ENABLED",
        "TENANT_SESSION_REQUIRED",
        "TENANT_REVIEW_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    assert tenant_session.validate_tenant_feature_flags() == {
        "binding": False,
        "required": False,
        "review": False,
    }

    monkeypatch.setenv("TENANT_SESSION_BINDING_ENABLED", "true")
    assert tenant_session.validate_tenant_feature_flags() == {
        "binding": True,
        "required": False,
        "review": False,
    }


@pytest.mark.parametrize(
    "flag_name",
    (
        "TENANT_SESSION_BINDING_ENABLED",
        "TENANT_SESSION_REQUIRED",
        "TENANT_REVIEW_ENABLED",
    ),
)
def test_invalid_feature_flag_fails_closed(monkeypatch, flag_name):
    _configure(monkeypatch, binding=False)
    monkeypatch.setenv(flag_name, "sometimes")

    with pytest.raises(RuntimeError, match="explicit boolean"):
        tenant_session.validate_tenant_feature_flags()


def test_dependent_flags_and_unsafe_secret_fail_closed(monkeypatch):
    _configure(monkeypatch, binding=False)
    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "true")
    with pytest.raises(RuntimeError, match="REQUIRED is not available"):
        tenant_session.validate_tenant_feature_flags()

    monkeypatch.setenv("TENANT_SESSION_BINDING_ENABLED", "true")
    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "false")
    monkeypatch.setenv("TENANT_REVIEW_ENABLED", "true")
    with pytest.raises(RuntimeError, match="REVIEW_ENABLED is not available"):
        tenant_session.validate_tenant_feature_flags()

    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "true")
    with pytest.raises(RuntimeError, match="REQUIRED is not available"):
        tenant_session.validate_tenant_feature_flags()

    monkeypatch.setenv("TENANT_SESSION_REQUIRED", "false")
    monkeypatch.setenv("TENANT_REVIEW_ENABLED", "false")
    monkeypatch.setattr(auth, "SESSION_SECRET", "wx-dev-secret-change-me")
    with pytest.raises(RuntimeError, match="non-placeholder SESSION_SECRET"):
        tenant_session.validate_tenant_feature_flags()


def test_scope_token_has_frozen_shape_and_round_trips(monkeypatch):
    tenant_id = uuid.uuid4()
    login_token, scope_token = _scope_token(
        monkeypatch,
        tenant_id=tenant_id,
        session_id="b" * 32,
    )

    assert len(scope_token) <= auth.MAX_SESSION_TOKEN_LENGTH == 768
    parts = scope_token.split(".")
    assert len(parts) == 8
    version, user_id, fingerprint, session_id, tenant, issued, expires, signature = parts
    assert version == "v1"
    assert user_id == "17"
    assert HEX_32.fullmatch(fingerprint)
    assert session_id == "b" * 32
    assert HEX_32.fullmatch(session_id)
    assert tenant == str(tenant_id)
    assert int(issued) == NOW
    assert int(expires) == NOW + 600
    assert HEX_64.fullmatch(signature)

    claims = auth.parse_tenant_scope_token(
        scope_token, login_token, 17, now=NOW
    )
    assert claims == auth.TenantScopeClaims(
        user_id=17,
        session_id="b" * 32,
        tenant_id=tenant_id,
        issued_at=NOW,
        expires_at=NOW + 600,
    )

    unbound = auth.make_tenant_scope_token(
        login_token, 17, now=NOW, session_id="c" * 32
    )
    assert unbound.split(".")[4] == "-"
    assert auth.parse_tenant_scope_token(
        unbound, login_token, 17, now=NOW
    ).tenant_id is None


@pytest.mark.parametrize("part_index", range(8))
def test_tampering_any_scope_token_part_invalidates_signature_or_format(
    monkeypatch, part_index
):
    login_token, scope_token = _scope_token(
        monkeypatch, tenant_id=uuid.uuid4()
    )
    parts = scope_token.split(".")
    original = parts[part_index]
    parts[part_index] = ("e" if original.startswith("f") else "f") + original[1:]
    tampered = ".".join(parts)

    assert auth.parse_tenant_scope_token(
        tampered, login_token, 17, now=NOW
    ) is None


@pytest.mark.parametrize(
    "bad_token",
    ("", "v1", "x" * 769, ".......", "not-a-scope-token"),
)
def test_scope_token_length_and_malformed_input_fail_closed(monkeypatch, bad_token):
    _configure(monkeypatch)
    assert auth.parse_tenant_scope_token(
        bad_token, _auth_token(), 17, now=NOW
    ) is None


@pytest.mark.parametrize("session_id", ("a" * 31, "A" * 32, "z" * 32))
def test_scope_session_id_must_be_32_lowercase_hex(monkeypatch, session_id):
    _configure(monkeypatch)
    with pytest.raises(ValueError, match="32 lowercase hex"):
        auth.make_tenant_scope_token(
            _auth_token(), 17, now=NOW, session_id=session_id
        )


def test_scope_rejects_future_and_expired_claims_at_exact_boundaries(monkeypatch):
    login_token, token = _scope_token(monkeypatch, max_age=120)
    assert auth.parse_tenant_scope_token(token, login_token, 17, now=NOW + 120)
    assert auth.parse_tenant_scope_token(
        token, login_token, 17, now=NOW + 121
    ) is None

    within_skew = auth.make_tenant_scope_token(
        login_token,
        17,
        now=NOW + auth.SESSION_CLOCK_SKEW,
        session_id="d" * 32,
    )
    assert auth.parse_tenant_scope_token(
        within_skew, login_token, 17, now=NOW
    )
    beyond_skew = auth.make_tenant_scope_token(
        login_token,
        17,
        now=NOW + auth.SESSION_CLOCK_SKEW + 1,
        session_id="e" * 32,
    )
    assert auth.parse_tenant_scope_token(
        beyond_skew, login_token, 17, now=NOW
    ) is None


def test_scope_is_bound_to_user_and_exact_login_fingerprint(monkeypatch):
    login_token, scope_token = _scope_token(monkeypatch, user_id=17)

    other_user_token = auth.make_session_token(18)
    assert auth.parse_tenant_scope_token(
        scope_token, other_user_token, 18, now=NOW
    ) is None
    assert auth.parse_tenant_scope_token(
        scope_token, login_token, 18, now=NOW
    ) is None

    monkeypatch.setattr(auth.time, "time", lambda: NOW + 1)
    replacement_login = auth.make_session_token(17)
    assert replacement_login != login_token
    assert auth.parse_tenant_scope_token(
        scope_token, replacement_login, 17, now=NOW + 1
    ) is None


def test_csrf_token_is_64_hex_and_cannot_cross_scope_sessions(monkeypatch):
    login_token, first_scope = _scope_token(
        monkeypatch, session_id="1" * 32
    )
    second_scope = auth.make_tenant_scope_token(
        login_token, 17, now=NOW, session_id="2" * 32
    )
    first_csrf = auth.tenant_scope_csrf_token(first_scope)
    second_csrf = auth.tenant_scope_csrf_token(second_scope)

    assert HEX_64.fullmatch(first_csrf)
    assert HEX_64.fullmatch(second_csrf)
    assert first_csrf != second_csrf


def test_resolve_scope_revalidates_active_choice_and_rejects_stale(monkeypatch):
    tenant_id = uuid.uuid4()
    choice = _choice(tenant_id, role="resource_reviewer")
    login_token, scope_token = _scope_token(
        monkeypatch, tenant_id=tenant_id
    )
    request = FakeRequest(
        {auth.COOKIE_NAME: login_token, auth.TENANT_COOKIE_NAME: scope_token}
    )
    current_user = {"id": 17, "username": "qa", "role": "member"}
    active_db = FakeDb([choice])

    context = tenant_session.resolve_tenant_scope(
        request, current_user, db=active_db
    )
    assert context == tenant_session.TenantSessionContext(
        user_id=17,
        tenant_id=tenant_id,
        membership_id=choice.membership_id,
        role="resource_reviewer",
    )
    assert active_db.calls == [17]

    stale_db = FakeDb([])
    with pytest.raises(HTTPException) as exc_info:
        tenant_session.resolve_tenant_scope(request, current_user, db=stale_db)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "tenant_scope_stale"


def test_resolve_scope_rejects_missing_unbound_and_cross_user(monkeypatch):
    _configure(monkeypatch)
    login_token = _auth_token(17)
    current_user = {"id": 17, "username": "qa", "role": "member"}

    for scope_token in (
        "",
        auth.make_tenant_scope_token(
            login_token, 17, now=NOW, session_id="3" * 32
        ),
        auth.make_tenant_scope_token(
            _auth_token(18),
            18,
            uuid.uuid4(),
            now=NOW,
            session_id="4" * 32,
        ),
    ):
        request = FakeRequest(
            {
                auth.COOKIE_NAME: login_token,
                auth.TENANT_COOKIE_NAME: scope_token,
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            tenant_session.resolve_tenant_scope(
                request, current_user, db=FakeDb([])
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "tenant_scope_required"


def test_session_cookie_options_are_canonical_and_secure_by_environment(
    monkeypatch,
):
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", 3600)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert auth.session_cookie_options() == {
        "max_age": 3600,
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "path": "/",
    }

    monkeypatch.setenv("ENVIRONMENT", "development")
    assert auth.session_cookie_options()["secure"] is False
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    assert auth.session_cookie_options()["secure"] is True
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "invalid")
    with pytest.raises(RuntimeError, match="must be a boolean"):
        auth.session_cookie_options()
