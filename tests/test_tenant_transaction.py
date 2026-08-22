"""Unit contract for tenant-scoped database transactions."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from unittest.mock import Mock

import psycopg2
import pytest

from wxsearch.db_connector import (
    DatabaseConnector,
    TenantAccessDenied,
    TenantChoice,
    TenantPrincipal,
    TenantTransactionUsageError,
)


@dataclass
class FakeScenario:
    user_public_id: uuid.UUID = field(default_factory=uuid.uuid4)
    membership_id: uuid.UUID = field(default_factory=uuid.uuid4)
    role: str = "sales"
    user_enabled: bool = True
    membership_active: bool = True
    choices: list[tuple] = field(default_factory=list)
    fail_business: BaseException | None = None
    fail_commit: BaseException | None = None
    fail_rollback: BaseException | None = None
    events: list = field(default_factory=list)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.scenario = connection.scenario
        self.rows = []
        self.rowcount = -1
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).lower().split())
        self.scenario.events.append(("execute", normalized, params, id(self.connection)))
        if "from users" in normalized:
            self.rows = (
                [(self.scenario.user_public_id,)]
                if self.scenario.user_enabled
                else []
            )
            self.rowcount = len(self.rows)
        elif "set_config" in normalized and "app.tenant_id" in normalized:
            self.rows = [(str(params[0]),)]
            self.rowcount = 1
        elif "app_list_active_tenants" in normalized:
            if params is not None and len(params) == 2:
                self.rows = (
                    [(self.scenario.membership_id, self.scenario.role)]
                    if self.scenario.membership_active
                    else []
                )
            else:
                self.rows = list(self.scenario.choices)
            self.rowcount = len(self.rows)
        elif "app_authorize_tenant_write" in normalized:
            self.rows = (
                [
                    (
                        self.scenario.user_public_id,
                        self.scenario.membership_id,
                        self.scenario.role,
                    )
                ]
                if self.scenario.user_enabled
                and self.scenario.membership_active
                else []
            )
            self.rowcount = len(self.rows)
        elif "tenant_memberships" in normalized:
            self.rows = (
                [(self.scenario.membership_id, self.scenario.role)]
                if self.scenario.membership_active
                else []
            )
            self.rowcount = len(self.rows)
        elif "unit_business_failure" in normalized:
            raise self.scenario.fail_business or RuntimeError("business failed")
        elif normalized.startswith("select"):
            self.rows = [("business-result",)]
            self.rowcount = 1
        else:
            self.rows = []
            self.rowcount = 3

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        if not self.closed:
            self.closed = True
            self.scenario.events.append(("cursor_close", id(self.connection)))


class FakeConnection:
    def __init__(self, scenario):
        self.scenario = scenario
        self.closed = False
        self.autocommit = False

    def cursor(self, *args, **kwargs):
        self.scenario.events.append(("cursor", id(self), args, kwargs))
        return FakeCursor(self)

    def commit(self):
        self.scenario.events.append(("commit", id(self)))
        if self.scenario.fail_commit is not None:
            raise self.scenario.fail_commit

    def rollback(self):
        self.scenario.events.append(("rollback", id(self)))
        if self.scenario.fail_rollback is not None:
            raise self.scenario.fail_rollback


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.events = connection.scenario.events

    def getconn(self):
        self.events.append(("getconn", id(self.connection)))
        return self.connection

    def putconn(self, connection, close=False):
        self.events.append(("putconn", id(connection), close))
        if close:
            connection.closed = True


def _connector(scenario: FakeScenario):
    connection = FakeConnection(scenario)
    connector = object.__new__(DatabaseConnector)
    connector.pool = FakePool(connection)
    connector._local = threading.local()
    # Tenant authorization must never escape to the autonomous helpers, since
    # those helpers borrow a different connection and lose SET LOCAL state.
    connector.execute_query = Mock(
        side_effect=AssertionError("autonomous execute_query is forbidden")
    )
    connector.execute_write = Mock(
        side_effect=AssertionError("autonomous execute_write is forbidden")
    )
    return connector, connection


def _executed_sql(events):
    return [event[1] for event in events if event[0] == "execute"]


def _putconn_events(events):
    return [event for event in events if event[0] == "putconn"]


def test_tenant_transaction_pins_authorization_and_business_to_one_connection():
    scenario = FakeScenario()
    connector, connection = _connector(scenario)
    tenant_id = uuid.uuid4()

    with connector.tenant_transaction(
        authenticated_user_id=17,
        requested_tenant_id=str(tenant_id),
    ) as transaction:
        assert transaction.principal == TenantPrincipal(
            user_id=17,
            user_public_id=scenario.user_public_id,
            tenant_id=tenant_id,
            membership_id=scenario.membership_id,
            role="sales",
        )
        assert transaction.execute_query("SELECT unit_business") == [
            ("business-result",)
        ]
        assert transaction.execute_write(
            "UPDATE unit_business SET value=%s", (1,)
        ) == 3
        with pytest.raises(TenantTransactionUsageError):
            DatabaseConnector.execute_query(connector, "SELECT 1")
        with pytest.raises(TenantTransactionUsageError):
            DatabaseConnector.execute_write(connector, "UPDATE unit SET x=1")
        for forbidden in ("commit", "rollback", "cursor", "connection"):
            assert not hasattr(transaction, forbidden)

    sql_calls = _executed_sql(scenario.events)
    user_index = next(i for i, sql_text in enumerate(sql_calls) if "from users" in sql_text)
    set_index = next(i for i, sql_text in enumerate(sql_calls) if "set_config" in sql_text)
    membership_index = next(
        i for i, sql_text in enumerate(sql_calls)
        if "app_list_active_tenants" in sql_text
    )
    business_index = next(
        i for i, sql_text in enumerate(sql_calls) if "unit_business" in sql_text
    )
    assert user_index < membership_index < set_index < business_index
    assert "tenant_memberships" not in sql_calls[membership_index]
    assert "for share" not in sql_calls[membership_index]
    authorization_event = next(
        event
        for event in scenario.events
        if event[0] == "execute" and "app_list_active_tenants" in event[1]
    )
    assert authorization_event[2] == (
        str(scenario.user_public_id),
        str(tenant_id),
    )
    execute_connection_ids = {
        event[3] for event in scenario.events if event[0] == "execute"
    }
    assert execute_connection_ids == {id(connection)}
    assert any(event[0] == "commit" for event in scenario.events)
    assert _putconn_events(scenario.events)[-1][2] is False
    connector.execute_query.assert_not_called()
    connector.execute_write.assert_not_called()
    with pytest.raises(TenantTransactionUsageError):
        transaction.execute_query("SELECT after_close")


def test_tenant_write_transaction_uses_locked_narrow_authorization_first():
    scenario = FakeScenario(role="resource_reviewer")
    connector, connection = _connector(scenario)
    tenant_id = uuid.uuid4()

    with connector.tenant_write_transaction(
        authenticated_user_id=17,
        requested_tenant_id=tenant_id,
    ) as transaction:
        assert transaction.principal == TenantPrincipal(
            user_id=17,
            user_public_id=scenario.user_public_id,
            tenant_id=tenant_id,
            membership_id=scenario.membership_id,
            role="resource_reviewer",
        )
        transaction.execute_write("UPDATE review_business SET value=1")

    sql_calls = _executed_sql(scenario.events)
    authorization_index = next(
        i
        for i, sql_text in enumerate(sql_calls)
        if "app_authorize_tenant_write" in sql_text
    )
    set_index = next(
        i for i, sql_text in enumerate(sql_calls) if "set_config" in sql_text
    )
    business_index = next(
        i for i, sql_text in enumerate(sql_calls) if "review_business" in sql_text
    )
    assert authorization_index < set_index < business_index
    authorization_event = next(
        event
        for event in scenario.events
        if event[0] == "execute" and "app_authorize_tenant_write" in event[1]
    )
    assert authorization_event[2] == (17, str(tenant_id))
    assert authorization_event[3] == id(connection)
    assert sql_calls[:3] == [
        "set local lock_timeout = '2s'",
        "set local statement_timeout = '5s'",
        "set local idle_in_transaction_session_timeout = '5s'",
    ]


def test_tenant_write_denial_never_sets_scope_or_yields():
    scenario = FakeScenario(membership_active=False)
    connector, _connection = _connector(scenario)
    yielded = False

    with pytest.raises(TenantAccessDenied):
        with connector.tenant_write_transaction(
            authenticated_user_id=17,
            requested_tenant_id=uuid.uuid4(),
        ):
            yielded = True

    assert yielded is False
    assert not any(
        "set_config" in sql_text for sql_text in _executed_sql(scenario.events)
    )
    assert not any(event[0] == "commit" for event in scenario.events)
    assert any(event[0] == "rollback" for event in scenario.events)


@pytest.mark.parametrize(
    ("user_enabled", "membership_active"),
    [(False, True), (True, False)],
)
def test_denied_principal_never_yields_or_commits(user_enabled, membership_active):
    scenario = FakeScenario(
        user_enabled=user_enabled,
        membership_active=membership_active,
    )
    connector, _connection = _connector(scenario)
    yielded = False

    with pytest.raises(TenantAccessDenied):
        with connector.tenant_transaction(
            authenticated_user_id=23,
            requested_tenant_id=uuid.uuid4(),
        ):
            yielded = True

    assert yielded is False
    assert not any(event[0] == "commit" for event in scenario.events)
    assert any(event[0] == "rollback" for event in scenario.events)
    assert _putconn_events(scenario.events)[-1][2] is False
    assert not any(
        "set_config" in sql_text for sql_text in _executed_sql(scenario.events)
    )


@pytest.mark.parametrize(
    "invalid_tenant_id",
    [None, "", "not-a-uuid", "00000000-0000-0000-0000-00000000000z"],
)
def test_invalid_tenant_uuid_fails_closed_before_borrowing(invalid_tenant_id):
    scenario = FakeScenario()
    connector, _connection = _connector(scenario)

    with pytest.raises((TenantAccessDenied, TypeError, ValueError)):
        with connector.tenant_transaction(
            authenticated_user_id=1,
            requested_tenant_id=invalid_tenant_id,
        ):
            pytest.fail("invalid UUID must not yield a transaction")

    assert not any(event[0] == "getconn" for event in scenario.events)


def test_business_exception_rolls_back_and_returns_healthy_connection():
    scenario = FakeScenario()
    connector, _connection = _connector(scenario)

    with pytest.raises(ValueError, match="abort business"):
        with connector.tenant_transaction(
            authenticated_user_id=5,
            requested_tenant_id=uuid.uuid4(),
        ):
            raise ValueError("abort business")

    assert not any(event[0] == "commit" for event in scenario.events)
    assert any(event[0] == "rollback" for event in scenario.events)
    assert _putconn_events(scenario.events)[-1][2] is False


def test_broken_business_query_is_rolled_back_and_evicted():
    scenario = FakeScenario(fail_business=psycopg2.OperationalError("lost connection"))
    connector, connection = _connector(scenario)

    with pytest.raises(psycopg2.OperationalError, match="lost connection"):
        with connector.tenant_transaction(
            authenticated_user_id=5,
            requested_tenant_id=uuid.uuid4(),
        ) as transaction:
            transaction.execute_query("SELECT unit_business_failure")

    assert any(event[0] == "rollback" for event in scenario.events)
    assert _putconn_events(scenario.events)[-1] == (
        "putconn",
        id(connection),
        True,
    )


def test_commit_failure_is_rolled_back_and_connection_is_evicted():
    scenario = FakeScenario(
        fail_commit=psycopg2.OperationalError("commit outcome unknown")
    )
    connector, connection = _connector(scenario)

    with pytest.raises(psycopg2.OperationalError, match="commit outcome unknown"):
        with connector.tenant_transaction(
            authenticated_user_id=5,
            requested_tenant_id=uuid.uuid4(),
        ):
            pass

    commit_index = next(
        i for i, event in enumerate(scenario.events) if event[0] == "commit"
    )
    rollback_index = next(
        i for i, event in enumerate(scenario.events) if event[0] == "rollback"
    )
    assert commit_index < rollback_index
    assert _putconn_events(scenario.events)[-1] == (
        "putconn",
        id(connection),
        True,
    )


def test_list_active_tenants_uses_enabled_public_id_and_narrow_function():
    tenant_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    scenario = FakeScenario(
        choices=[
            (tenant_id, "tenant-a", "Tenant A", membership_id, "resource_reviewer")
        ]
    )
    connector, connection = _connector(scenario)

    choices = connector.list_active_tenants(authenticated_user_id=31)

    assert choices == [
        TenantChoice(
            tenant_id=tenant_id,
            code="tenant-a",
            name="Tenant A",
            membership_id=membership_id,
            role="resource_reviewer",
        )
    ]
    sql_calls = _executed_sql(scenario.events)
    assert "from users" in sql_calls[0]
    function_index = next(
        i for i, text in enumerate(sql_calls) if "app_list_active_tenants" in text
    )
    function_event = [
        event
        for event in scenario.events
        if event[0] == "execute" and "app_list_active_tenants" in event[1]
    ][0]
    assert function_index > 0
    assert "%s" in function_event[1]
    assert function_event[2] == (str(scenario.user_public_id),)
    assert function_event[3] == id(connection)
    assert not any(event[0] == "commit" for event in scenario.events)
    connector.execute_query.assert_not_called()
    connector.execute_write.assert_not_called()
