"""Regression tests for the fail-closed lead quality boundary."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from wxsearch.api.leads import _build_where, _require_quarantine_access
from wxsearch.api import sync as sync_api
from wxsearch.api.sync import _lead_payload_is_publishable, _upsert_lead
from wxsearch.production_sync import _partition_quality_gate


def _outbox_row(event_id: str, entity_type: str, payload: dict):
    return (event_id, entity_type, "entity-key", None, payload)


def test_sender_only_publishes_cleaned_leads_but_keeps_articles():
    rows = [
        _outbox_row("article", "article", {}),
        _outbox_row("legacy", "lead", {}),
        _outbox_row("pending", "lead", {"llm_status": "pending"}),
        _outbox_row("fail", "lead", {"llm_status": "fail"}),
        _outbox_row("done-true", "lead", {"llm_status": "done", "has_lead_value": True}),
        _outbox_row("done-false", "lead", {"llm_status": "done", "has_lead_value": False}),
    ]

    publishable, suppressed = _partition_quality_gate(rows)

    assert [row[0] for row in publishable] == ["article", "done-true", "done-false"]
    assert suppressed == ["legacy", "pending", "fail"]


def test_receiver_rejects_missing_pending_and_failed_llm_status():
    assert not _lead_payload_is_publishable({})
    assert not _lead_payload_is_publishable({"llm_status": "pending"})
    assert not _lead_payload_is_publishable({"llm_status": "fail"})
    assert _lead_payload_is_publishable({"llm_status": "DONE"})


def test_receiver_does_not_touch_database_for_unfinished_lead():
    class RejectDatabaseUse:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("unfinished lead must not touch the database")

    assert _upsert_lead(RejectDatabaseUse(), {"llm_status": "pending"}) is None


def test_receiver_quarantines_unfinished_event_without_advancing_version(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, statement, _params=None):
            self.statements.append(" ".join(statement.split()))

        def fetchone(self):
            return None

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.committed = False
            self.closed = False

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("valid quarantine event must not roll back")

        def close(self):
            self.closed = True

    class Request:
        headers = {}

        async def body(self):
            return json.dumps(
                {
                    "events": [
                        {
                            "event_id": "00000000-0000-0000-0000-000000000001",
                            "entity_type": "lead",
                            "entity_key": "article-key",
                            "source_updated_at": "2026-08-23T00:00:00+00:00",
                            "payload": {"llm_status": "pending"},
                        }
                    ]
                }
            ).encode()

    connection = Connection()
    monkeypatch.setattr(sync_api, "_connect", lambda: connection)
    monkeypatch.setattr(sync_api, "_verify_signature", lambda *_args: None)

    result = asyncio.run(sync_api.receive_sync_batch(Request()))

    assert result["accepted"] == []
    assert result["skipped"] == ["00000000-0000-0000-0000-000000000001"]
    assert connection.committed and connection.closed
    assert not any(
        "production_sync_entity_versions" in statement
        for statement in connection.cursor_instance.statements
    )


def test_default_lead_query_is_fail_closed():
    where, _ = _build_where(None, None, None)
    assert "llm_status = 'done'" in where
    assert "has_lead_value = TRUE" in where


def test_explicit_diagnostic_query_can_see_quarantine():
    where, _ = _build_where(None, None, None, include_non_lead=True)
    assert "llm_status = 'done'" not in where


def test_quarantine_requires_administrator_role():
    with pytest.raises(HTTPException) as error:
        _require_quarantine_access(True, {"role": "sales"})
    assert error.value.status_code == 403
    _require_quarantine_access(True, {"role": "admin"})
    _require_quarantine_access(False, {"role": "sales"})


def test_collect_release_mounts_external_read_only_secret_directory():
    compose = (
        Path(__file__).resolve().parents[1] / "docker-compose.collect-release.yml"
    ).read_text(encoding="utf-8")
    assert "WXSEARCH_SECRETS_PATH: /run/secrets/wxsearch/secrets.json" in compose
    assert "${COLLECT_SECRETS_DIR:?COLLECT_SECRETS_DIR_required}" in compose
    assert ":/run/secrets/wxsearch:ro" in compose


def test_backend_release_mounts_git_artifact_read_only():
    compose = (
        Path(__file__).resolve().parents[1] / "docker-compose.backend-release.yml"
    ).read_text(encoding="utf-8")
    assert "${BACKEND_RELEASE_DIR:?BACKEND_RELEASE_DIR_required}/wxsearch" in compose
    assert "${BACKEND_RELEASE_DIR:?BACKEND_RELEASE_DIR_required}/docs" in compose
    assert compose.count(":ro") == 2
    assert "${BACKEND_RUNTIME_DIR:?BACKEND_RUNTIME_DIR_required}/logs" in compose
