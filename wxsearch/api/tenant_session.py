"""Dormant v2 tenant-session discovery and binding endpoints.

The legacy login cookie remains the authentication authority.  ``wxscope`` is
only a signed tenant candidate; every tenant business service must still enter
``DatabaseConnector.tenant_transaction`` before touching tenant data.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from ..db_connector import DatabaseConnector, TenantAccessDenied, TenantChoice
from . import auth as auth_module
from .auth import get_current_user


router = APIRouter(prefix="/api/v2/session", tags=["tenant-session"])


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class TenantSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID


@dataclass(frozen=True)
class TenantSessionContext:
    user_id: int
    tenant_id: UUID
    membership_id: UUID
    role: str


def _strict_flag(name: str) -> bool:
    raw = os.getenv(name, "false").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be an explicit boolean")


def tenant_session_binding_enabled() -> bool:
    return _strict_flag("TENANT_SESSION_BINDING_ENABLED")


def _session_secret_is_safe() -> bool:
    value = auth_module.SESSION_SECRET.strip().lower()
    unsafe_markers = (
        "change-me",
        "changeme",
        "placeholder",
        "replace-with",
        "wx-dev-secret",
    )
    return len(value) >= 32 and not any(marker in value for marker in unsafe_markers)


def validate_tenant_feature_flags() -> dict[str, bool]:
    """Validate the three rollout switches as one atomic contract."""

    binding = tenant_session_binding_enabled()
    required = _strict_flag("TENANT_SESSION_REQUIRED")
    review = _strict_flag("TENANT_REVIEW_ENABLED")
    if required:
        raise RuntimeError("TENANT_SESSION_REQUIRED is not available in this release")
    if review:
        raise RuntimeError("TENANT_REVIEW_ENABLED is not available in this release")
    if binding and not _session_secret_is_safe():
        raise RuntimeError(
            "tenant session binding requires a non-placeholder SESSION_SECRET "
            "with at least 32 characters"
        )
    # Also validate cookie/TTL configuration during application startup.
    auth_module.session_cookie_options()
    auth_module._scope_max_age()
    return {"binding": binding, "required": required, "review": review}


def _require_binding_enabled() -> None:
    flags = validate_tenant_feature_flags()
    if not flags["binding"]:
        raise HTTPException(status_code=404, detail="not_found")


def _choice_payload(choice: TenantChoice) -> dict:
    return {
        "tenant_id": str(choice.tenant_id),
        "code": choice.code,
        "name": choice.name,
        "role": choice.role,
    }


def _set_scope_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth_module.TENANT_COOKIE_NAME,
        token,
        **auth_module.session_cookie_options(),
    )


def _read_scope(request: Request, current_user: dict):
    auth_token = request.cookies.get(auth_module.COOKIE_NAME, "")
    scope_token = request.cookies.get(auth_module.TENANT_COOKIE_NAME, "")
    claims = auth_module.parse_tenant_scope_token(
        scope_token,
        auth_token,
        current_user["id"],
    )
    return auth_token, scope_token, claims


def _new_unbound_scope(auth_token: str, user_id: int) -> str:
    return auth_module.make_tenant_scope_token(auth_token, user_id)


def _load_session_state(
    request: Request,
    response: Response,
    current_user: dict,
    db: DatabaseConnector,
) -> dict:
    auth_token, old_scope, claims = _read_scope(request, current_user)
    choices = db.list_active_tenants(
        authenticated_user_id=current_user["id"]
    )
    selected = None
    status = "unbound"
    scope_token = old_scope

    if claims is not None and claims.tenant_id is not None:
        selected = next(
            (
                choice
                for choice in choices
                if choice.tenant_id == claims.tenant_id
            ),
            None,
        )
        if selected is not None:
            status = "active"
        else:
            status = "stale"
    elif old_scope and claims is None:
        status = "stale"

    if not choices:
        status = (
            "stale"
            if claims is not None and claims.tenant_id is not None
            else "unmapped"
        )
        selected = None

    if claims is None or status == "stale":
        scope_token = _new_unbound_scope(auth_token, current_user["id"])
        _set_scope_cookie(response, scope_token)

    return {
        "status": status,
        "user": {
            "id": current_user["id"],
            "username": current_user["username"],
            "role": current_user["role"],
        },
        "tenant": _choice_payload(selected) if selected is not None else None,
        "tenants": [_choice_payload(choice) for choice in choices],
        "csrf_token": auth_module.tenant_scope_csrf_token(scope_token),
    }


def _verify_scope_csrf(
    request: Request,
    current_user: dict,
    submitted_token: str | None,
):
    auth_token, scope_token, claims = _read_scope(request, current_user)
    if claims is None:
        raise HTTPException(
            status_code=409,
            detail="tenant_scope_bootstrap_required",
        )
    expected = auth_module.tenant_scope_csrf_token(scope_token)
    if not submitted_token or not hmac.compare_digest(
        submitted_token, expected
    ):
        raise HTTPException(status_code=403, detail="csrf_invalid")
    return auth_token, claims


def resolve_tenant_scope(
    request: Request,
    current_user: dict,
    db: DatabaseConnector | None = None,
) -> TenantSessionContext:
    """Resolve current scope and re-check membership without opening work TX.

    A future business service must still pass the returned candidate into its
    own synchronous ``tenant_transaction``; this helper is not authorization to
    execute tenant SQL by itself.
    """

    _require_binding_enabled()
    _auth_token, _scope_token, claims = _read_scope(request, current_user)
    if claims is None or claims.tenant_id is None:
        raise HTTPException(status_code=409, detail="tenant_scope_required")
    connector = db or DatabaseConnector()
    choice = next(
        (
            item
            for item in connector.list_active_tenants(
                authenticated_user_id=current_user["id"]
            )
            if item.tenant_id == claims.tenant_id
        ),
        None,
    )
    if choice is None:
        raise HTTPException(status_code=409, detail="tenant_scope_stale")
    return TenantSessionContext(
        user_id=current_user["id"],
        tenant_id=choice.tenant_id,
        membership_id=choice.membership_id,
        role=choice.role,
    )


@router.get("")
def get_tenant_session(
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    _require_binding_enabled()
    return _load_session_state(
        request, response, current_user, DatabaseConnector()
    )


@router.get("/tenants")
def list_tenant_choices(
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    _require_binding_enabled()
    state = _load_session_state(
        request, response, current_user, DatabaseConnector()
    )
    return {
        "status": state["status"],
        "tenants": state["tenants"],
        "csrf_token": state["csrf_token"],
    }


@router.put("/tenant")
def bind_tenant_scope(
    payload: TenantSelectionRequest,
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None),
    current_user: dict = Depends(get_current_user),
):
    _require_binding_enabled()
    auth_token, _claims = _verify_scope_csrf(
        request, current_user, x_csrf_token
    )
    db = DatabaseConnector()
    try:
        with db.tenant_transaction(
            authenticated_user_id=current_user["id"],
            requested_tenant_id=payload.tenant_id,
        ) as transaction:
            principal = transaction.principal
    except TenantAccessDenied as error:
        raise HTTPException(
            status_code=404,
            detail="tenant_not_available",
        ) from error

    scope_token = auth_module.make_tenant_scope_token(
        auth_token,
        current_user["id"],
        principal.tenant_id,
    )
    _set_scope_cookie(response, scope_token)
    return {
        "status": "active",
        "tenant": {
            "tenant_id": str(principal.tenant_id),
            "role": principal.role,
        },
        "csrf_token": auth_module.tenant_scope_csrf_token(scope_token),
    }


@router.delete("/tenant")
def unbind_tenant_scope(
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None),
    current_user: dict = Depends(get_current_user),
):
    _require_binding_enabled()
    auth_token, _claims = _verify_scope_csrf(
        request, current_user, x_csrf_token
    )
    scope_token = _new_unbound_scope(auth_token, current_user["id"])
    _set_scope_cookie(response, scope_token)
    return {
        "status": "unbound",
        "csrf_token": auth_module.tenant_scope_csrf_token(scope_token),
    }
