"""采集侧到正式业务环境的 outbox 发送器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import psycopg2
import requests

log = logging.getLogger(__name__)

_SUPPRESSED_REASON = "quality_gate:llm_not_done"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _event_is_publishable(row) -> bool:
    """Return whether an outbox row may cross into the business database.

    Articles are evidence and may be synchronized immediately.  A lead is only
    a business-facing projection after the asynchronous LLM pass completed;
    ``pending``/``fail`` and legacy payloads without a status stay local.
    """
    entity_type = str(row[1] or "")
    if entity_type != "lead":
        return True
    payload = row[4] if isinstance(row[4], dict) else {}
    return str(payload.get("llm_status") or "").strip().lower() == "done"


def _partition_quality_gate(rows):
    publishable = []
    suppressed_ids = []
    for row in rows:
        if _event_is_publishable(row):
            publishable.append(row)
        else:
            suppressed_ids.append(str(row[0]))
    return publishable, suppressed_ids


def _mark_suppressed(conn, event_ids: list[str]) -> None:
    """Acknowledge non-publishable legacy events without sending them.

    Migration 020 has no ``suppressed`` status and later migrations are not yet
    deployable, so ``sent`` is used as the terminal status while ``last_error``
    retains an auditable quality-gate marker.  A later ``done`` update creates a
    new outbox event and is sent normally.
    """
    if not event_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE production_sync_outbox
            SET status='sent', sent_at=NOW(), last_error=%s
            WHERE event_id = ANY(%s::uuid[])
              AND status IN ('pending', 'retry')
            """,
            (_SUPPRESSED_REASON, event_ids),
        )
    conn.commit()


def _connect():
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    from .db_connector import DatabaseConnector
    return psycopg2.connect(**DatabaseConnector()._db_config)


def _mark_retry(conn, event_ids: list[str], error: str) -> None:
    if not event_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE production_sync_outbox
            SET attempts = attempts + 1,
                status = CASE WHEN attempts + 1 >= 10 THEN 'dead' ELSE 'retry' END,
                next_attempt_at = NOW()
                    + make_interval(secs => LEAST(3600, 30 * power(2, LEAST(attempts, 7)))::INTEGER),
                last_error = LEFT(%s, 1000)
            WHERE event_id = ANY(%s::uuid[])
              AND status IN ('pending', 'retry')
            """,
            (error, event_ids),
        )
    conn.commit()


def sync_once(batch_size: int | None = None) -> dict[str, Any]:
    """发送一批到正式环境；默认关闭，未配置时无副作用。"""
    if not _truthy(os.getenv("PROD_SYNC_ENABLED", "false")):
        return {"enabled": False, "sent": 0}

    endpoint = os.getenv("PROD_SYNC_URL", "").strip()
    secret = os.getenv("PROD_SYNC_SECRET", "").strip()
    if not endpoint.startswith("https://") or not secret:
        raise RuntimeError("PROD_SYNC_URL must be HTTPS and PROD_SYNC_SECRET must be configured")
    try:
        size = max(1, min(100, int(batch_size or os.getenv("PROD_SYNC_BATCH", "20"))))
    except ValueError:
        size = 20

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 会话级 advisory lock 防止 beat 重叠；接收端仍以 event_id 兜底幂等。
            cur.execute("SELECT pg_try_advisory_lock(hashtext('production-sync-outbox'))")
            if not cur.fetchone()[0]:
                return {"enabled": True, "locked": True, "sent": 0}
            cur.execute(
                """
                SELECT event_id::TEXT, entity_type, entity_key, source_updated_at, payload
                FROM production_sync_outbox
                WHERE status IN ('pending', 'retry') AND next_attempt_at <= NOW()
                ORDER BY CASE entity_type WHEN 'article' THEN 0 ELSE 1 END, id
                LIMIT %s
                """,
                (size,),
            )
            rows = cur.fetchall()
        if not rows:
            return {"enabled": True, "sent": 0, "pending": 0}

        rows, suppressed_ids = _partition_quality_gate(rows)
        _mark_suppressed(conn, suppressed_ids)
        if suppressed_ids:
            log.warning(
                "质量门禁抑制 %s 条未完成 LLM 清洗的 lead 同步事件",
                len(suppressed_ids),
            )
        if not rows:
            return {
                "enabled": True,
                "sent": 0,
                "suppressed": len(suppressed_ids),
            }

        events = [
            {
                "event_id": row[0],
                "entity_type": row[1],
                "entity_key": row[2],
                "source_updated_at": row[3].isoformat(),
                "payload": row[4],
            }
            for row in rows
        ]
        body = json.dumps(
            {"source": os.getenv("PROD_SYNC_SOURCE", "local-collector"), "events": events},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
        try:
            response = requests.post(
                endpoint,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Sync-Timestamp": timestamp,
                    "X-Sync-Signature": signature,
                },
                timeout=float(os.getenv("PROD_SYNC_TIMEOUT", "30")),
            )
            response.raise_for_status()
            result = response.json()
            confirmed = set(result.get("accepted", [])) | set(result.get("skipped", []))
            sent_ids = [event["event_id"] for event in events if event["event_id"] in confirmed]
            missing_ids = [event["event_id"] for event in events if event["event_id"] not in confirmed]
            with conn.cursor() as cur:
                if sent_ids:
                    cur.execute(
                        """
                        UPDATE production_sync_outbox
                        SET status='sent', sent_at=NOW(), last_error=NULL
                        WHERE event_id = ANY(%s::uuid[])
                        """,
                        (sent_ids,),
                    )
            conn.commit()
            if missing_ids:
                _mark_retry(conn, missing_ids, "receiver did not confirm event")
            return {
                "enabled": True,
                "sent": len(sent_ids),
                "retry": len(missing_ids),
                "suppressed": len(suppressed_ids),
            }
        except Exception as exc:
            conn.rollback()
            event_ids = [event["event_id"] for event in events]
            _mark_retry(conn, event_ids, str(exc))
            log.warning("正式环境同步失败，已安排重试：%s", exc)
            return {
                "enabled": True,
                "sent": 0,
                "retry": len(event_ids),
                "suppressed": len(suppressed_ids),
                "error": str(exc),
            }
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(sync_once(), ensure_ascii=False))
