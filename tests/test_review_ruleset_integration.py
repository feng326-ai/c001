"""PostgreSQL proof that a published review ruleset cannot be rewritten."""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlsplit

import pytest

RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"
EXPECTED_SHA256 = (
    "285b8c2fe43ca8b6d3517df223488f4d2fd3e6c7940cbe84ae547ade4b3f48ff"
)


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("ruleset integration requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    if "_lease_qa_" not in urlsplit(database_url).path:
        raise RuntimeError("ruleset integration refused a non-QA database")
    return database_url


@pytest.mark.skipif(not RUN, reason="requires disposable QA PostgreSQL 15")
def test_published_review_ruleset_is_canonical_and_immutable() -> None:
    import psycopg2
    from psycopg2 import errors

    connection = psycopg2.connect(_guarded_qa_database_url())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT definition::text, definition_sha256
                FROM public.review_rulesets
                WHERE version = 'review-rules/1.0.0'
                """
            )
            definition_text, stored_sha256 = cursor.fetchone()
            canonical = json.dumps(
                json.loads(definition_text),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assert hashlib.sha256(canonical.encode()).hexdigest() == EXPECTED_SHA256
            assert stored_sha256 == EXPECTED_SHA256

        with connection.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                UPDATE public.review_rulesets
                SET approval_reference = 'qa-rewrite-must-fail'
                WHERE version = 'review-rules/1.0.0'
                """
            )
        connection.rollback()

        with connection.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO public.review_ruleset_completion_reasons(
                    ruleset_version, reason_code, review_decision, disposition
                ) VALUES (
                    'review-rules/1.0.0', 'late_unpublished_reason',
                    'rejected', 'archive'
                )
                """
            )
        connection.rollback()

        with connection.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO public.review_ruleset_reopen_reasons(
                    ruleset_version, reason_code, requires_new_realtime_source
                ) VALUES (
                    'review-rules/1.0.0', 'late_unpublished_reopen', TRUE
                )
                """
            )
        connection.rollback()

        with connection.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                DELETE FROM public.review_ruleset_completion_reasons
                WHERE ruleset_version = 'review-rules/1.0.0'
                  AND reason_code = 'not_selection_or_voting'
                """
            )
        connection.rollback()
    finally:
        connection.close()
