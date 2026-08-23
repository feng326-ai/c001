"""正式环境内部同步入口。

只接受带时间戳 HMAC 的事件批次；按文章 UUID 映射本地/正式自增 ID，且只更新
采集与 AI 字段。分配、跟进、个人状态等正式业务字段不在白名单内。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any

import psycopg2
from fastapi import APIRouter, HTTPException, Request
from psycopg2.extras import Json


router = APIRouter()

_ARTICLE_FIELDS = (
    "uuid", "content_hash", "url_fingerprint", "simhash", "title", "summary",
    "account", "account_id", "mid", "idx", "sn", "publish_time", "source_date",
    "canonical_url", "original_url", "source_channel", "keyword", "intent_category",
    "has_lead_value", "lead_type", "priority_score", "priority_level",
    "scoring_breakdown", "llm_reasoning", "content", "content_clean", "created_at",
    "updated_at", "resource_level", "keywords", "channels", "event_name",
    "event_details", "collected_at", "is_recurring", "activity_category",
    "activity_region", "recurrence", "activity_status", "source_channels",
    "organizer_name", "organizer_contact", "organizer_region", "voting_platform",
    "recurrence_period", "edition_no", "voting_status", "event_key",
)

_LEAD_FIELDS = (
    "title", "summary", "content", "url", "account", "publish_time",
    "source_channel", "keyword", "intent_category", "has_lead_value", "lead_type",
    "priority_score", "priority_level", "scoring_breakdown", "llm_reasoning",
    "created_at", "updated_at", "resource_level", "event_name", "event_details",
    "llm_status", "llm_last_run_at", "llm_attempts", "is_online_voting",
    "online_voting_url", "collected_at", "is_recurring", "activity_category",
    "activity_region", "recurrence", "activity_status", "organizer_name",
    "organizer_contact", "organizer_region", "voting_platform", "recurrence_period",
    "edition_no", "voting_status", "event_key", "is_annual_recurring",
    "event_cycle_month", "next_wake_up_date", "wake_up_status",
)

_JSON_FIELDS = {"scoring_breakdown", "organizer_contact"}


def _lead_payload_is_publishable(payload: dict[str, Any]) -> bool:
    """Fail closed unless the source explicitly completed LLM cleaning."""
    return str(payload.get("llm_status") or "").strip().lower() == "done"


def _connect():
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    from ..db_connector import DatabaseConnector
    return psycopg2.connect(**DatabaseConnector()._db_config)


def _verify_signature(body: bytes, timestamp: str | None, signature: str | None) -> None:
    secret = os.getenv("SYNC_SHARED_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="sync receiver is not configured")
    try:
        ts = int(timestamp or "")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid sync timestamp") from exc
    if abs(int(time.time()) - ts) > 300:
        raise HTTPException(status_code=401, detail="expired sync request")
    expected = hmac.new(
        secret.encode("utf-8"),
        str(ts).encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid sync signature")


def _db_value(field: str, value: Any) -> Any:
    if field in _JSON_FIELDS and value is not None:
        return Json(value)
    return value


def _upsert_article(cur, payload: dict[str, Any]) -> int:
    try:
        source_uuid = uuid.UUID(str(payload.get("uuid")))
    except (TypeError, ValueError) as exc:
        raise ValueError("article.uuid is required") from exc

    cur.execute(
        "SELECT target_article_id FROM production_sync_article_keys "
        "WHERE source_uuid = %s FOR UPDATE",
        (str(source_uuid),),
    )
    mapped = cur.fetchone()
    target_id = mapped[0] if mapped else None

    if target_id is None:
        cur.execute(
            """
            SELECT id FROM articles_core
            WHERE uuid = %s::uuid
               OR content_hash = %s
               OR (NULLIF(%s, '') IS NOT NULL AND url_fingerprint = %s)
               OR (NULLIF(%s, '') IS NOT NULL AND canonical_url = %s)
            ORDER BY CASE WHEN uuid = %s::uuid THEN 0 ELSE 1 END
            LIMIT 1 FOR UPDATE
            """,
            (
                str(source_uuid), payload.get("content_hash"),
                payload.get("url_fingerprint"), payload.get("url_fingerprint"),
                payload.get("canonical_url"), payload.get("canonical_url"),
                str(source_uuid),
            ),
        )
        row = cur.fetchone()
        target_id = row[0] if row else None

    fields = [f for f in _ARTICLE_FIELDS if f in payload]
    if target_id is None:
        if not fields:
            raise ValueError("article payload is empty")
        placeholders = ",".join(["%s"] * len(fields))
        cur.execute(
            f"INSERT INTO articles_core ({','.join(fields)}) VALUES ({placeholders}) RETURNING id",
            tuple(_db_value(f, payload[f]) for f in fields),
        )
        target_id = cur.fetchone()[0]
    else:
        update_fields = [f for f in fields if f not in {"uuid", "created_at"}]
        if update_fields:
            assignments = ",".join(f"{f}=%s" for f in update_fields)
            cur.execute(
                f"UPDATE articles_core SET {assignments} WHERE id=%s",
                tuple(_db_value(f, payload[f]) for f in update_fields) + (target_id,),
            )

    cur.execute(
        """
        INSERT INTO production_sync_article_keys (source_uuid, target_article_id)
        VALUES (%s, %s)
        ON CONFLICT (source_uuid) DO UPDATE
        SET target_article_id = EXCLUDED.target_article_id
        """,
        (str(source_uuid), target_id),
    )
    return int(target_id)


def _upsert_lead(cur, payload: dict[str, Any]) -> int | None:
    # Defense in depth: an old/misconfigured sender must not bypass the source
    # quality gate and create a business lead from a rule-only draft.
    if not _lead_payload_is_publishable(payload):
        return None
    try:
        source_uuid = uuid.UUID(str(payload.get("article_uuid")))
    except (TypeError, ValueError) as exc:
        raise ValueError("lead.article_uuid is required") from exc

    cur.execute(
        "SELECT target_article_id FROM production_sync_article_keys WHERE source_uuid=%s",
        (str(source_uuid),),
    )
    row = cur.fetchone()
    if row:
        article_id = row[0]
    else:
        cur.execute("SELECT id FROM articles_core WHERE uuid=%s::uuid", (str(source_uuid),))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"article not synced: {source_uuid}")
        article_id = row[0]
        cur.execute(
            "INSERT INTO production_sync_article_keys (source_uuid,target_article_id) "
            "VALUES (%s,%s) ON CONFLICT (source_uuid) DO NOTHING",
            (str(source_uuid), article_id),
        )

    cur.execute("SELECT id, updated_by_human FROM qualified_leads WHERE article_id=%s FOR UPDATE", (article_id,))
    existing = cur.fetchone()
    fields = [f for f in _LEAD_FIELDS if f in payload]
    if existing:
        lead_id, human_edited = existing
        if not human_edited:
            update_fields = [f for f in fields if f != "created_at"]
            if update_fields:
                assignments = ",".join(f"{f}=%s" for f in update_fields)
                cur.execute(
                    f"UPDATE qualified_leads SET {assignments} WHERE id=%s",
                    tuple(_db_value(f, payload[f]) for f in update_fields) + (lead_id,),
                )
        return int(lead_id)

    insert_fields = ["article_id"] + fields
    placeholders = ",".join(["%s"] * len(insert_fields))
    values = (article_id,) + tuple(_db_value(f, payload[f]) for f in fields)
    cur.execute(
        f"INSERT INTO qualified_leads ({','.join(insert_fields)}) VALUES ({placeholders}) RETURNING id",
        values,
    )
    return int(cur.fetchone()[0])


@router.post("/sync/batch")
async def receive_sync_batch(request: Request):
    body = await request.body()
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="sync batch too large")
    _verify_signature(
        body,
        request.headers.get("X-Sync-Timestamp"),
        request.headers.get("X-Sync-Signature"),
    )
    try:
        document = json.loads(body)
        events = document["events"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid sync payload") from exc
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise HTTPException(status_code=400, detail="events must contain 1..100 items")

    conn = _connect()
    accepted: list[str] = []
    skipped: list[str] = []
    try:
        cur = conn.cursor()
        for event in events:
            event_id = uuid.UUID(str(event["event_id"]))
            entity_type = str(event["entity_type"])
            entity_key = str(event["entity_key"])
            source_updated_at = datetime.fromisoformat(str(event["source_updated_at"]).replace("Z", "+00:00"))
            payload = event["payload"]
            if entity_type not in {"article", "lead"} or not isinstance(payload, dict):
                raise ValueError("invalid entity event")

            cur.execute("SELECT 1 FROM production_sync_receipts WHERE event_id=%s", (str(event_id),))
            if cur.fetchone():
                skipped.append(str(event_id))
                continue
            # Do not advance the entity-version watermark for a quarantined lead:
            # a later cleaned event may legitimately carry the same source time.
            if entity_type == "lead" and not _lead_payload_is_publishable(payload):
                skipped.append(str(event_id))
            else:
                cur.execute(
                    "SELECT source_updated_at FROM production_sync_entity_versions "
                    "WHERE entity_type=%s AND entity_key=%s FOR UPDATE",
                    (entity_type, entity_key),
                )
                version_row = cur.fetchone()
                if version_row and source_updated_at < version_row[0]:
                    skipped.append(str(event_id))
                else:
                    if entity_type == "article":
                        _upsert_article(cur, payload)
                    else:
                        _upsert_lead(cur, payload)
                    cur.execute(
                        """
                        INSERT INTO production_sync_entity_versions
                            (entity_type,entity_key,source_updated_at,event_id)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (entity_type,entity_key) DO UPDATE
                        SET source_updated_at=EXCLUDED.source_updated_at,event_id=EXCLUDED.event_id
                        """,
                        (entity_type, entity_key, source_updated_at, str(event_id)),
                    )
                    accepted.append(str(event_id))

            payload_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            cur.execute(
                """
                INSERT INTO production_sync_receipts
                    (event_id,entity_type,entity_key,source_updated_at,payload_hash)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (str(event_id), entity_type, entity_key, source_updated_at, payload_hash),
            )
        conn.commit()
        return {"ok": True, "accepted": accepted, "skipped": skipped}
    except (KeyError, TypeError, ValueError) as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
