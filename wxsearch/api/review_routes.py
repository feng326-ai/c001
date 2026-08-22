"""Dormant v2 tenant review routes; production activation remains blocked."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..db_connector import DatabaseConnector, TenantAccessDenied
from ..review_service import (
    ReviewConflict,
    ReviewInvalidInput,
    ReviewInvalidTransition,
    ReviewNotFound,
    ReviewPermissionDenied,
    ReviewService,
)
from .auth import get_current_user
from .tenant_session import (
    require_tenant_command_scope,
    resolve_tenant_scope,
    tenant_review_enabled,
)

router = APIRouter(prefix="/api/v2", tags=["tenant-review"])


class StartReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_version: int


class CompleteReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_review_version: int
    expected_candidate_version: int
    decision: str
    disposition: str
    reason_code: str
    reviewer_note: str | None = None
    reopen_not_before: datetime | None = None


class ReopenReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_review_id: UUID
    expected_review_version: int
    expected_candidate_version: int
    reopen_reason_code: str
    trigger_source_document_id: UUID | None = None


def _require_review_enabled() -> None:
    if not tenant_review_enabled():
        raise HTTPException(status_code=404, detail="not_found")


def _map_review_error(error: Exception) -> HTTPException:
    if isinstance(error, ReviewNotFound):
        return HTTPException(status_code=404, detail="review_resource_not_found")
    if isinstance(error, ReviewPermissionDenied):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, ReviewConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (ReviewInvalidInput, ReviewInvalidTransition)):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, TenantAccessDenied):
        return HTTPException(status_code=404, detail="review_resource_not_found")
    raise error


@router.get("/tenant-candidates")
def list_tenant_candidates(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50),
    current_user: dict = Depends(get_current_user),
):
    _require_review_enabled()
    db = DatabaseConnector()
    scope = resolve_tenant_scope(request, current_user, db)
    try:
        with db.tenant_transaction(
            authenticated_user_id=current_user["id"],
            requested_tenant_id=scope.tenant_id,
        ) as transaction:
            candidates = ReviewService().list_candidates(
                transaction, status=status, limit=limit
            )
    except (
        ReviewNotFound,
        ReviewPermissionDenied,
        ReviewConflict,
        ReviewInvalidInput,
        ReviewInvalidTransition,
        TenantAccessDenied,
    ) as error:
        raise _map_review_error(error) from error
    return {"items": candidates}


@router.post("/tenant-candidates/{candidate_id}:start-review")
def start_tenant_review(
    candidate_id: UUID,
    payload: StartReviewRequest,
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    current_user: dict = Depends(get_current_user),
):
    _require_review_enabled()
    db = DatabaseConnector()
    scope = require_tenant_command_scope(
        request, current_user, x_csrf_token, db
    )
    try:
        with db.tenant_write_transaction(
            authenticated_user_id=current_user["id"],
            requested_tenant_id=scope.tenant_id,
        ) as transaction:
            result = ReviewService().start_review(
                transaction,
                candidate_id=candidate_id,
                expected_candidate_version=payload.expected_candidate_version,
                idempotency_key=idempotency_key or "",
            )
    except (
        ReviewNotFound,
        ReviewPermissionDenied,
        ReviewConflict,
        ReviewInvalidInput,
        ReviewInvalidTransition,
        TenantAccessDenied,
    ) as error:
        raise _map_review_error(error) from error
    headers = (
        {"Idempotency-Replayed": "true"}
        if result["idempotency_replayed"]
        else {}
    )
    return JSONResponse(result, status_code=201, headers=headers)


@router.post("/tenant-reviews/{review_id}:complete")
def complete_tenant_review(
    review_id: UUID,
    payload: CompleteReviewRequest,
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    current_user: dict = Depends(get_current_user),
):
    _require_review_enabled()
    db = DatabaseConnector()
    scope = require_tenant_command_scope(
        request, current_user, x_csrf_token, db
    )
    try:
        with db.tenant_write_transaction(
            authenticated_user_id=current_user["id"],
            requested_tenant_id=scope.tenant_id,
        ) as transaction:
            result = ReviewService().complete_review(
                transaction,
                review_id=review_id,
                expected_review_version=payload.expected_review_version,
                expected_candidate_version=payload.expected_candidate_version,
                decision=payload.decision,
                disposition=payload.disposition,
                idempotency_key=idempotency_key or "",
                reason_code=payload.reason_code,
                reviewer_note=payload.reviewer_note,
                reopen_not_before=payload.reopen_not_before,
            )
    except (
        ReviewNotFound,
        ReviewPermissionDenied,
        ReviewConflict,
        ReviewInvalidInput,
        ReviewInvalidTransition,
        TenantAccessDenied,
    ) as error:
        raise _map_review_error(error) from error
    headers = (
        {"Idempotency-Replayed": "true"}
        if result["idempotency_replayed"]
        else {}
    )
    return JSONResponse(result, headers=headers)


@router.post("/tenant-candidates/{candidate_id}:reopen-review")
def reopen_tenant_review(
    candidate_id: UUID,
    payload: ReopenReviewRequest,
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    current_user: dict = Depends(get_current_user),
):
    _require_review_enabled()
    db = DatabaseConnector()
    scope = require_tenant_command_scope(
        request, current_user, x_csrf_token, db
    )
    try:
        with db.tenant_write_transaction(
            authenticated_user_id=current_user["id"],
            requested_tenant_id=scope.tenant_id,
        ) as transaction:
            result = ReviewService().reopen_review(
                transaction,
                candidate_id=candidate_id,
                previous_review_id=payload.previous_review_id,
                expected_review_version=payload.expected_review_version,
                expected_candidate_version=payload.expected_candidate_version,
                reopen_reason_code=payload.reopen_reason_code,
                trigger_source_document_id=payload.trigger_source_document_id,
                idempotency_key=idempotency_key or "",
            )
    except (
        ReviewNotFound,
        ReviewPermissionDenied,
        ReviewConflict,
        ReviewInvalidInput,
        ReviewInvalidTransition,
        TenantAccessDenied,
    ) as error:
        raise _map_review_error(error) from error
    headers = (
        {"Idempotency-Replayed": "true"}
        if result["idempotency_replayed"]
        else {}
    )
    return JSONResponse(result, status_code=201, headers=headers)
