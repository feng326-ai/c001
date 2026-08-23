"""Dormant, least-privilege orchestration for review Candidate distribution.

The trusted intake writer is intentionally outside this release.  This module
only expands an opaque inbox id and processes database-selected target ids; no
public method accepts a tenant, visibility policy, collection mode, or score.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$")
ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
POLICY_VERSION = "shared-competition/1.0.0"


class ReviewDistributorError(RuntimeError):
    """Stable base error that never embeds a connection string."""


class ReviewDistributorInvalidInput(ReviewDistributorError):
    pass


class ReviewDistributorDisabled(ReviewDistributorError):
    pass


class ReviewDistributorConflict(ReviewDistributorError):
    pass


def review_distributor_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    raw = env.get("REVIEW_DISTRIBUTOR_ENABLED", "false").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ReviewDistributorInvalidInput(
        "REVIEW_DISTRIBUTOR_ENABLED must be an explicit boolean"
    )


def _canonical_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ReviewDistributorInvalidInput(f"{field}_invalid") from error
    if isinstance(value, str) and str(parsed) != value:
        raise ReviewDistributorInvalidInput(f"{field}_invalid")
    return parsed


def _bounded_lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ReviewDistributorInvalidInput("lease_seconds_invalid")
    return value


def _database_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    direct = env.get("DISTRIBUTOR_DATABASE_URL")
    file_name = env.get("DISTRIBUTOR_DATABASE_URL_FILE")
    if direct is not None and file_name is not None:
        raise ReviewDistributorInvalidInput(
            "configure exactly one of DISTRIBUTOR_DATABASE_URL or "
            "DISTRIBUTOR_DATABASE_URL_FILE"
        )
    if file_name is not None:
        path = Path(file_name)
        if not path.is_file():
            raise ReviewDistributorInvalidInput(
                "DISTRIBUTOR_DATABASE_URL_FILE is not a regular file"
            )
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ReviewDistributorInvalidInput(
                "cannot read DISTRIBUTOR_DATABASE_URL_FILE"
            ) from error
    else:
        value = direct
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ReviewDistributorInvalidInput("missing distributor database url")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.path.strip("/")
        or not parsed.username
        or parsed.password is None
    ):
        raise ReviewDistributorInvalidInput("invalid distributor database url")
    return value


def _default_connection_factory():
    import psycopg2

    return psycopg2.connect(_database_url())


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class ReviewDistributor:
    """Call only the four fenced distributor functions, one transaction each."""

    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        self._connection_factory = connection_factory or _default_connection_factory

    def __repr__(self) -> str:
        return "ReviewDistributor(connection_factory=<redacted>)"

    @staticmethod
    def _require_enabled() -> None:
        if not review_distributor_enabled():
            raise ReviewDistributorDisabled("review_distributor_disabled")

    def _call(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        connection = cursor = None
        commit_started = False
        try:
            connection = self._connection_factory()
            if getattr(connection, "autocommit", False):
                connection.autocommit = False
            cursor = connection.cursor()
            cursor.execute("SET LOCAL lock_timeout = '2s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '5s'")
            cursor.execute(query, params)
            rows = list(cursor.fetchall())
            commit_started = True
            connection.commit()
            return rows
        except ReviewDistributorError:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001,S110
                    pass
            sqlstate = getattr(error, "pgcode", None)
            if sqlstate in {"40001", "40P01", "23505"}:
                raise ReviewDistributorConflict(
                    "review_distribution_conflict"
                ) from error
            if sqlstate in {"22023", "P0002"}:
                raise ReviewDistributorInvalidInput(
                    "review_distribution_invalid"
                ) from error
            detail = (
                "review_distribution_commit_unknown"
                if commit_started
                else "review_distribution_database_error"
            )
            raise ReviewDistributorError(detail) from error
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001,S110
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001,S110
                    pass

    def expand_inbox(self, *, inbox_id: uuid.UUID | str) -> dict[str, Any]:
        self._require_enabled()
        parsed_inbox = _canonical_uuid(inbox_id, "inbox_id")
        rows = self._call(
            """
            SELECT batch_id, batch_status, target_count, replayed
            FROM public.app_expand_review_distribution(%s)
            """,
            (str(parsed_inbox),),
        )
        if len(rows) != 1:
            raise ReviewDistributorConflict("review_distribution_expand_invalid")
        batch_id, status, target_count, replayed = rows[0]
        return {
            "batch_id": str(batch_id),
            "status": str(status),
            "target_count": int(target_count),
            "replayed": bool(replayed),
        }

    def claim_target(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> dict[str, Any] | None:
        self._require_enabled()
        if not isinstance(worker_id, str):
            raise ReviewDistributorInvalidInput("worker_id_invalid")
        cleaned_worker = worker_id.strip()
        if not WORKER_ID.fullmatch(cleaned_worker):
            raise ReviewDistributorInvalidInput("worker_id_invalid")
        lease = _bounded_lease_seconds(lease_seconds)
        rows = self._call(
            """
            SELECT target_id, claim_token, lease_expires_at
            FROM public.app_claim_review_distribution_target(%s, %s)
            """,
            (cleaned_worker, lease),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ReviewDistributorConflict("review_distribution_claim_invalid")
        target_id, token, expires_at = rows[0]
        return {
            "target_id": str(target_id),
            "fencing_token": str(token),
            "lease_expires_at": _json_value(expires_at),
        }

    def apply_target(
        self,
        *,
        target_id: uuid.UUID | str,
        fencing_token: uuid.UUID | str,
    ) -> dict[str, Any]:
        self._require_enabled()
        parsed_target = _canonical_uuid(target_id, "target_id")
        parsed_token = _canonical_uuid(fencing_token, "fencing_token")
        rows = self._call(
            """
            SELECT target_id, target_status, outcome_code,
                   grant_id, candidate_id
            FROM public.app_apply_review_distribution_target(%s, %s)
            """,
            (str(parsed_target), str(parsed_token)),
        )
        if len(rows) != 1:
            raise ReviewDistributorConflict("review_distribution_apply_invalid")
        result_target, status, outcome, grant_id, candidate_id = rows[0]
        return {
            "target_id": str(result_target),
            "status": str(status),
            "outcome_code": str(outcome),
            "grant_id": None if grant_id is None else str(grant_id),
            "candidate_id": None if candidate_id is None else str(candidate_id),
        }

    def fail_target(
        self,
        *,
        target_id: uuid.UUID | str,
        fencing_token: uuid.UUID | str,
        error_code: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        parsed_target = _canonical_uuid(target_id, "target_id")
        parsed_token = _canonical_uuid(fencing_token, "fencing_token")
        if not isinstance(error_code, str):
            raise ReviewDistributorInvalidInput("error_code_invalid")
        cleaned_error = error_code.strip()
        if not ERROR_CODE.fullmatch(cleaned_error):
            raise ReviewDistributorInvalidInput("error_code_invalid")
        rows = self._call(
            """
            SELECT target_id, target_status, attempt_count, next_attempt_at
            FROM public.app_report_review_distribution_failure(%s, %s, %s)
            """,
            (str(parsed_target), str(parsed_token), cleaned_error),
        )
        if len(rows) != 1:
            raise ReviewDistributorConflict("review_distribution_failure_invalid")
        result_target, status, attempts, retry_at = rows[0]
        return {
            "target_id": str(result_target),
            "status": str(status),
            "attempt_count": int(attempts),
            "next_attempt_at": _json_value(retry_at),
        }

    def process_one(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> dict[str, Any] | None:
        claim = self.claim_target(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return None
        try:
            return self.apply_target(
                target_id=claim["target_id"],
                fencing_token=claim["fencing_token"],
            )
        except ReviewDistributorError:
            try:
                self.fail_target(
                    target_id=claim["target_id"],
                    fencing_token=claim["fencing_token"],
                    error_code="apply_failed",
                )
            except ReviewDistributorError:
                pass
            raise
