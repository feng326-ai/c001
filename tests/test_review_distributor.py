"""Unit and orchestration contract for the dormant review distributor."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from wxsearch.review_distributor import (
    ReviewDistributor,
    ReviewDistributorConflict,
    ReviewDistributorDisabled,
    ReviewDistributorError,
    ReviewDistributorInvalidInput,
    review_distributor_enabled,
)

FORBIDDEN_INTERFACE_FIELDS = {
    "tenant",
    "tenant_id",
    "tenant_ids",
    "policy",
    "policy_version",
    "collection_mode",
    "mode",
    "score",
    "ruleset_version",
}


class FakeDatabaseError(RuntimeError):
    def __init__(self, message: str, pgcode: str | None = None):
        super().__init__(message)
        self.pgcode = pgcode


@dataclass
class ScriptedStep:
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    execute_error: BaseException | None = None
    commit_error: BaseException | None = None


class FakeCursor:
    def __init__(self, connection: FakeConnection):
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).lower().split())
        self.connection.events.append(("execute", normalized, params))
        if "public.app_" in normalized:
            if self.connection.step.execute_error is not None:
                raise self.connection.step.execute_error
            self.rows = list(self.connection.step.rows)

    def fetchall(self):
        self.connection.events.append(("fetchall",))
        return list(self.rows)

    def close(self):
        self.closed = True
        self.connection.events.append(("cursor_close",))


class FakeConnection:
    def __init__(self, step: ScriptedStep, serial: int):
        self.step = step
        self.serial = serial
        self.events: list[tuple[Any, ...]] = []
        self.autocommit = True
        self.closed = False

    def cursor(self):
        self.events.append(("cursor",))
        return FakeCursor(self)

    def commit(self):
        self.events.append(("commit",))
        if self.step.commit_error is not None:
            raise self.step.commit_error

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.closed = True
        self.events.append(("close",))


class ScriptedConnectionFactory:
    def __init__(self, *steps: ScriptedStep):
        self.steps = list(steps)
        self.connections: list[FakeConnection] = []

    def __call__(self):
        assert self.steps, "unexpected database connection"
        connection = FakeConnection(self.steps.pop(0), len(self.connections) + 1)
        self.connections.append(connection)
        return connection


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_DISTRIBUTOR_ENABLED", "true")


def _business_calls(factory: ScriptedConnectionFactory):
    return [
        event
        for connection in factory.connections
        for event in connection.events
        if event[0] == "execute" and "public.app_" in event[1]
    ]


def _assert_committed_and_closed(connection: FakeConnection) -> None:
    assert ("commit",) in connection.events
    assert ("rollback",) not in connection.events
    assert connection.events[-2:] == [("cursor_close",), ("close",)]
    assert connection.closed is True
    assert connection.autocommit is False


def test_feature_flag_defaults_off_and_accepts_only_explicit_booleans() -> None:
    assert review_distributor_enabled({}) is False
    for value in ("1", "true", "TRUE", " yes ", "on"):
        assert review_distributor_enabled({"REVIEW_DISTRIBUTOR_ENABLED": value}) is True
    for value in ("0", "false", "FALSE", " no ", "off"):
        assert (
            review_distributor_enabled({"REVIEW_DISTRIBUTOR_ENABLED": value}) is False
        )
    for value in ("", "enabled", "2", "null"):
        with pytest.raises(
            ReviewDistributorInvalidInput,
            match="REVIEW_DISTRIBUTOR_ENABLED",
        ):
            review_distributor_enabled({"REVIEW_DISTRIBUTOR_ENABLED": value})


def test_disabled_distributor_fails_before_borrowing_a_connection(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_DISTRIBUTOR_ENABLED", raising=False)
    factory = ScriptedConnectionFactory(ScriptedStep())
    distributor = ReviewDistributor(connection_factory=factory)

    with pytest.raises(ReviewDistributorDisabled, match="review_distributor_disabled"):
        distributor.claim_target(worker_id="qa-worker")

    assert factory.connections == []


def test_public_methods_cannot_accept_tenant_policy_mode_or_score(monkeypatch) -> None:
    _enable(monkeypatch)
    allowed = {
        "expand_inbox": {"self", "inbox_id"},
        "claim_target": {"self", "worker_id", "lease_seconds"},
        "apply_target": {"self", "target_id", "fencing_token"},
        "fail_target": {"self", "target_id", "fencing_token", "error_code"},
        "process_one": {"self", "worker_id", "lease_seconds"},
    }
    for method_name, expected in allowed.items():
        signature = inspect.signature(getattr(ReviewDistributor, method_name))
        assert set(signature.parameters) == expected
        assert FORBIDDEN_INTERFACE_FIELDS.isdisjoint(signature.parameters)
        assert all(
            parameter.kind is not inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    distributor = ReviewDistributor(
        connection_factory=lambda: pytest.fail(
            "forbidden fields must fail before database access"
        )
    )
    with pytest.raises(TypeError):
        distributor.expand_inbox(inbox_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    with pytest.raises(TypeError):
        distributor.claim_target(worker_id="qa-worker", policy="shared_competition")
    with pytest.raises(TypeError):
        distributor.apply_target(
            target_id=uuid.uuid4(),
            fencing_token=uuid.uuid4(),
            collection_mode="realtime_signal",
        )


@pytest.mark.parametrize(
    "invalid_identifier",
    [None, "", "not-a-uuid", "00000000-0000-0000-0000-00000000000z"],
)
def test_invalid_uuid_input_fails_before_database_access(
    monkeypatch, invalid_identifier
) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep())
    distributor = ReviewDistributor(connection_factory=factory)

    with pytest.raises(ReviewDistributorInvalidInput):
        distributor.expand_inbox(inbox_id=invalid_identifier)
    with pytest.raises(ReviewDistributorInvalidInput):
        distributor.apply_target(
            target_id=invalid_identifier,
            fencing_token=uuid.uuid4(),
        )

    assert factory.connections == []


def test_uuid_strings_must_be_canonical_lowercase(monkeypatch) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep())
    distributor = ReviewDistributor(connection_factory=factory)
    identifier = str(uuid.uuid4()).upper()
    with pytest.raises(ReviewDistributorInvalidInput, match="inbox_id_invalid"):
        distributor.expand_inbox(inbox_id=identifier)
    assert factory.connections == []


@pytest.mark.parametrize(
    "worker_id",
    [None, True, 123, "", "ab", "bad worker", "x" * 65],
)
def test_worker_id_is_a_bounded_string_not_a_coercible_value(
    monkeypatch, worker_id
) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep())
    with pytest.raises(ReviewDistributorInvalidInput, match="worker_id_invalid"):
        ReviewDistributor(connection_factory=factory).claim_target(worker_id=worker_id)
    assert factory.connections == []


@pytest.mark.parametrize("lease_seconds", [True, 4, 301, 30.0, "30", None])
def test_lease_seconds_is_a_bounded_integer(monkeypatch, lease_seconds) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep())
    with pytest.raises(ReviewDistributorInvalidInput, match="lease_seconds_invalid"):
        ReviewDistributor(connection_factory=factory).claim_target(
            worker_id="qa-worker", lease_seconds=lease_seconds
        )
    assert factory.connections == []


@pytest.mark.parametrize(
    "error_code",
    [None, True, 123, "", "UPPERCASE", "has-hyphen", "x" * 65],
)
def test_failure_code_is_a_bounded_normalized_string(monkeypatch, error_code) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep())
    with pytest.raises(ReviewDistributorInvalidInput, match="error_code_invalid"):
        ReviewDistributor(connection_factory=factory).fail_target(
            target_id=uuid.uuid4(),
            fencing_token=uuid.uuid4(),
            error_code=error_code,
        )
    assert factory.connections == []


def test_four_commands_use_only_the_frozen_functions_and_separate_transactions(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    inbox_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    target_id = uuid.uuid4()
    token = uuid.uuid4()
    grant_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    expires_at = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    retry_at = datetime(2026, 8, 23, 12, 1, tzinfo=timezone.utc)
    factory = ScriptedConnectionFactory(
        ScriptedStep(rows=[(batch_id, "queued", 3, False)]),
        ScriptedStep(rows=[(target_id, token, expires_at)]),
        ScriptedStep(
            rows=[
                (
                    target_id,
                    "succeeded",
                    "created",
                    grant_id,
                    candidate_id,
                )
            ]
        ),
        ScriptedStep(rows=[(target_id, "retry", 1, retry_at)]),
    )
    distributor = ReviewDistributor(connection_factory=factory)

    assert distributor.expand_inbox(inbox_id=inbox_id) == {
        "batch_id": str(batch_id),
        "status": "queued",
        "target_count": 3,
        "replayed": False,
    }
    assert distributor.claim_target(worker_id="qa-worker-01", lease_seconds=45) == {
        "target_id": str(target_id),
        "fencing_token": str(token),
        "lease_expires_at": expires_at.isoformat(),
    }
    assert distributor.apply_target(target_id=target_id, fencing_token=token) == {
        "target_id": str(target_id),
        "status": "succeeded",
        "outcome_code": "created",
        "grant_id": str(grant_id),
        "candidate_id": str(candidate_id),
    }
    assert distributor.fail_target(
        target_id=target_id,
        fencing_token=token,
        error_code="database_timeout",
    ) == {
        "target_id": str(target_id),
        "status": "retry",
        "attempt_count": 1,
        "next_attempt_at": retry_at.isoformat(),
    }

    assert len(factory.connections) == 4
    assert [
        call[1].split("(", 1)[0].rsplit(".", 1)[-1] for call in _business_calls(factory)
    ] == [
        "app_expand_review_distribution",
        "app_claim_review_distribution_target",
        "app_apply_review_distribution_target",
        "app_report_review_distribution_failure",
    ]
    assert [call[2] for call in _business_calls(factory)] == [
        (str(inbox_id),),
        ("qa-worker-01", 45),
        (str(target_id), str(token)),
        (str(target_id), str(token), "database_timeout"),
    ]
    assert len({id(connection) for connection in factory.connections}) == 4
    for connection in factory.connections:
        sql = [event[1] for event in connection.events if event[0] == "execute"]
        assert sql[:3] == [
            "set local lock_timeout = '2s'",
            "set local statement_timeout = '5s'",
            "set local idle_in_transaction_session_timeout = '5s'",
        ]
        _assert_committed_and_closed(connection)


def test_empty_claim_is_a_committed_no_work_result(monkeypatch) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(ScriptedStep(rows=[]))
    result = ReviewDistributor(connection_factory=factory).claim_target(
        worker_id="qa-worker"
    )
    assert result is None
    assert len(_business_calls(factory)) == 1
    _assert_committed_and_closed(factory.connections[0])


@pytest.mark.parametrize("sqlstate", ["40001", "40P01", "23505"])
def test_stale_or_competing_apply_is_a_rollback_conflict(monkeypatch, sqlstate) -> None:
    _enable(monkeypatch)
    factory = ScriptedConnectionFactory(
        ScriptedStep(execute_error=FakeDatabaseError("stale claim", pgcode=sqlstate))
    )
    with pytest.raises(ReviewDistributorConflict, match="review_distribution_conflict"):
        ReviewDistributor(connection_factory=factory).apply_target(
            target_id=uuid.uuid4(), fencing_token=uuid.uuid4()
        )
    connection = factory.connections[0]
    assert ("commit",) not in connection.events
    assert ("rollback",) in connection.events
    assert connection.closed is True


def test_process_one_claims_then_applies_without_reporting_failure(monkeypatch) -> None:
    _enable(monkeypatch)
    target_id = uuid.uuid4()
    token = uuid.uuid4()
    grant_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    factory = ScriptedConnectionFactory(
        ScriptedStep(
            rows=[
                (
                    target_id,
                    token,
                    datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
                )
            ]
        ),
        ScriptedStep(
            rows=[
                (
                    target_id,
                    "succeeded",
                    "created",
                    grant_id,
                    candidate_id,
                )
            ]
        ),
    )

    result = ReviewDistributor(connection_factory=factory).process_one(
        worker_id="qa-worker"
    )
    assert result["status"] == "succeeded"
    assert [call[2] for call in _business_calls(factory)] == [
        ("qa-worker", 30),
        (str(target_id), str(token)),
    ]


def test_process_one_reports_apply_failure_with_the_exact_claim_token(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    target_id = uuid.uuid4()
    token = uuid.uuid4()
    expires_at = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    retry_at = datetime(2026, 8, 23, 12, 1, tzinfo=timezone.utc)
    factory = ScriptedConnectionFactory(
        ScriptedStep(rows=[(target_id, token, expires_at)]),
        ScriptedStep(execute_error=FakeDatabaseError("connection lost")),
        ScriptedStep(rows=[(target_id, "retry", 1, retry_at)]),
    )

    with pytest.raises(
        ReviewDistributorError, match="review_distribution_database_error"
    ):
        ReviewDistributor(connection_factory=factory).process_one(worker_id="qa-worker")

    assert [call[2] for call in _business_calls(factory)] == [
        ("qa-worker", 30),
        (str(target_id), str(token)),
        (str(target_id), str(token), "apply_failed"),
    ]
    assert ("rollback",) in factory.connections[1].events
    _assert_committed_and_closed(factory.connections[2])


def test_late_token_cannot_be_converted_into_a_failure_update(monkeypatch) -> None:
    _enable(monkeypatch)
    target_id = uuid.uuid4()
    stale_token = uuid.uuid4()
    expires_at = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    factory = ScriptedConnectionFactory(
        ScriptedStep(rows=[(target_id, stale_token, expires_at)]),
        ScriptedStep(execute_error=FakeDatabaseError("stale apply", pgcode="40001")),
        ScriptedStep(execute_error=FakeDatabaseError("stale failure", pgcode="40001")),
    )

    with pytest.raises(ReviewDistributorConflict):
        ReviewDistributor(connection_factory=factory).process_one(worker_id="qa-worker")

    calls = _business_calls(factory)
    assert calls[-2][2] == (str(target_id), str(stale_token))
    assert calls[-1][2] == (str(target_id), str(stale_token), "apply_failed")
    late_connections = factory.connections[1:]
    assert all(("rollback",) in connection.events for connection in late_connections)
    assert all(("commit",) not in connection.events for connection in late_connections)


def test_database_or_commit_failure_never_leaks_connection_details(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    # A scanner-approved placeholder is sufficient to prove that database
    # exception details never escape through the public error contract.
    secret_dsn = "postgresql://distributor:test-only@example.invalid/review"
    for step, expected in (
        (
            ScriptedStep(execute_error=FakeDatabaseError(secret_dsn)),
            "review_distribution_database_error",
        ),
        (
            ScriptedStep(commit_error=FakeDatabaseError(secret_dsn)),
            "review_distribution_commit_unknown",
        ),
    ):
        factory = ScriptedConnectionFactory(step)
        with pytest.raises(ReviewDistributorError) as exc_info:
            ReviewDistributor(connection_factory=factory).claim_target(
                worker_id="qa-worker"
            )
        assert str(exc_info.value) == expected
        assert "test-only" not in str(exc_info.value)
        assert ("rollback",) in factory.connections[0].events
        assert factory.connections[0].closed is True
