"""真实 PostgreSQL/Redis 租约集成测试。

只允许连接数据库名包含 ``_lease_qa_``、Redis DB 15 的一次性 QA 环境。
默认跳过，禁止指向 staging 快照或生产采集平面。
"""

import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


RUN = os.getenv("RUN_LEASE_INTEGRATION") == "1"


@unittest.skipUnless(RUN, "set RUN_LEASE_INTEGRATION=1 in disposable QA environment")
class RealLeaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1":
            raise RuntimeError("ALLOW_DESTRUCTIVE_TESTS=1 is required")
        database_url = os.environ["DATABASE_URL"]
        redis_url = os.environ["REDIS_URL"]
        db_name = urlsplit(database_url).path.lstrip("/")
        redis_db = urlsplit(redis_url).path.lstrip("/")
        if "_lease_qa_" not in db_name:
            raise RuntimeError(f"unsafe database name: {db_name}")
        if redis_db != "15":
            raise RuntimeError(f"unsafe Redis DB: {redis_db}; expected 15")

        import psycopg2
        import redis

        cls.pg = psycopg2.connect(database_url)
        cls.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        cls._create_schema()

        from wxsearch.db_connector import DatabaseConnector
        DatabaseConnector._instance = None
        from wxsearch.task_scheduler import DistributedTaskScheduler
        cls.scheduler_cls = DistributedTaskScheduler

    @classmethod
    def tearDownClass(cls):
        try:
            from wxsearch.db_connector import DatabaseConnector
            inst = DatabaseConnector._instance
            if inst is not None and hasattr(inst, "pool"):
                inst.pool.closeall()
            DatabaseConnector._instance = None
        finally:
            cls.pg.close()

    @classmethod
    def _create_schema(cls):
        repo_root = Path(__file__).resolve().parents[1]
        schema_files = [
            repo_root / "docs" / "db_schema.sql",
            repo_root / "docs" / "migrations" / "007_multi_device_foundation.sql",
            repo_root / "docs" / "migrations" / "008_keyword_channel_state.sql",
            repo_root / "docs" / "migrations" / "011_channel_cycle.sql",
        ]
        with cls.pg.cursor() as cur:
            for schema_file in schema_files:
                cur.execute(schema_file.read_text(encoding="utf-8"))
        cls.pg.commit()

    def setUp(self):
        with self.pg.cursor() as cur:
            cur.execute("TRUNCATE collect_tasks, devices, keyword_channel_state, keywords RESTART IDENTITY CASCADE")
            cur.execute("""
                INSERT INTO keywords(keyword, status, next_collect_time, channels)
                VALUES ('同一关键词', 'pending', NOW(), ARRAY['wechat_pc','sogou'])
                RETURNING id
            """)
            keyword_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO keyword_channel_state
                    (keyword_id, channel, status, next_collect_time)
                VALUES (%s, 'wechat_pc', 'pending', NOW()),
                       (%s, 'sogou', 'pending', NOW())
            """, (keyword_id, keyword_id))
        self.pg.commit()
        keys = list(self.redis.scan_iter("wxsearch:*"))
        if keys:
            self.redis.delete(*keys)
        self.sched = self.scheduler_cls.from_env()

    def _state(self, channel):
        with self.pg.cursor() as cur:
            cur.execute("""
                SELECT s.status, s.claimer FROM keyword_channel_state s
                JOIN keywords k ON k.id=s.keyword_id
                WHERE k.keyword='同一关键词' AND s.channel=%s
            """, (channel,))
            return cur.fetchone()

    def _state_for(self, keyword, channel):
        with self.pg.cursor() as cur:
            cur.execute("""
                SELECT s.status, s.claimer FROM keyword_channel_state s
                JOIN keywords k ON k.id=s.keyword_id
                WHERE k.keyword=%s AND s.channel=%s
            """, (keyword, channel))
            return cur.fetchone()

    def _keyword_stats(self, keyword="同一关键词"):
        with self.pg.cursor() as cur:
            cur.execute("""
                SELECT status, total_collected, last_collected_count
                FROM keywords WHERE keyword=%s
            """, (keyword,))
            return cur.fetchone()

    def test_cross_channel_fencing_and_v2_recovery(self):
        wx = self.sched.claim_task("wechat_pc", "vm-wx", 1, lease_aware=True)[0]
        sg = self.sched.claim_task("sogou", "vm-sg", 1, lease_aware=True)[0]
        self.assertNotEqual(wx["lease_id"], sg["lease_id"])
        self.assertEqual(self.redis.scard(self.sched.CLAIMED_SET), 2)

        self.assertTrue(self.sched.heartbeat_device(
            "vm-wx", channel="wechat_pc", current_keyword="同一关键词",
            active_keywords=[wx],
        ))
        self.assertFalse(self.sched.report_result(
            "同一关键词", 1, True, device_id="vm-wx", channel="wechat_pc",
            lease_id="stale-token",
        ))
        self.assertTrue(self.sched.report_result(
            "同一关键词", 3, True, device_id="vm-wx", channel="wechat_pc",
            lease_id=wx["lease_id"],
        ))
        self.assertEqual(self._state("wechat_pc"), ("completed", None))
        self.assertEqual(self._state("sogou"), ("running", "vm-sg"))
        self.assertEqual(self.redis.scard(self.sched.CLAIMED_SET), 1)

        sg_key = self.sched._lease_key("sogou", "同一关键词")
        self.redis.hset(sg_key, "last_claimed", (
            datetime.now() - timedelta(minutes=30)
        ).isoformat())
        with self.pg.cursor() as cur:
            cur.execute("""
                INSERT INTO devices(device_id,status,last_heartbeat,started_at)
                VALUES ('vm-sg','online',NOW(),NOW())
                ON CONFLICT(device_id) DO UPDATE SET last_heartbeat=NOW(), status='online'
            """)
        self.pg.commit()

        self.assertEqual(self.sched.recover_stale_claims(15), ["同一关键词"])
        self.assertEqual(self._state("sogou"), ("pending", None))
        self.assertEqual(self.redis.scard(self.sched.CLAIMED_SET), 0)

    def test_pg_owner_mismatch_keeps_redis_lease_then_can_retry(self):
        claim = self.sched.claim_task("wechat_pc", "vm-a", 1, lease_aware=True)[0]
        member = self.sched._lease_member("wechat_pc", "同一关键词")
        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state SET claimer='vm-other'
                WHERE channel='wechat_pc'
            """)
        self.pg.commit()

        self.assertFalse(self.sched.report_result(
            "同一关键词", 1, True, device_id="vm-a", channel="wechat_pc",
            lease_id=claim["lease_id"],
        ))
        self.assertTrue(self.redis.sismember(self.sched.CLAIMED_SET, member))

        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state SET claimer='vm-a'
                WHERE channel='wechat_pc'
            """)
        self.pg.commit()
        self.assertTrue(self.sched.report_result(
            "同一关键词", 1, True, device_id="vm-a", channel="wechat_pc",
            lease_id=claim["lease_id"],
        ))

    def test_legacy_claim_and_result_remain_compatible(self):
        claimed = self.sched.claim_task("wechat_pc", "vm-old", 1, lease_aware=False)
        self.assertEqual(claimed, ["同一关键词"])
        self.assertTrue(self.sched.heartbeat_device(
            "vm-old", device_type="pc", channel="wechat_pc",
        ))
        self.assertTrue(self.sched.report_result(
            "同一关键词", 1, True, device_id="vm-old", channel="wechat_pc",
        ))

    def test_result_transaction_is_idempotent_and_stats_increment_once(self):
        claim = self.sched.claim_task(
            "wechat_pc", "vm-idem", 1, lease_aware=True
        )[0]
        self.assertTrue(self.sched.report_result(
            "同一关键词", 4, True, device_id="vm-idem", channel="wechat_pc",
            lease_id=claim["lease_id"],
        ))
        self.assertEqual(self._keyword_stats(), ("completed", 4, 4))
        self.assertEqual(self.sched._persist_result_state(
            "同一关键词", "wechat_pc", "vm-idem", 4, True,
        ), "already_committed")
        self.assertEqual(self._keyword_stats(), ("completed", 4, 4))

    def test_register_keyword_atomically_seeds_all_default_channels(self):
        self.assertEqual(self.sched.register_keywords(["新增默认词"]), 1)
        with self.pg.cursor() as cur:
            cur.execute("""
                SELECT s.channel FROM keyword_channel_state s
                JOIN keywords k ON k.id=s.keyword_id
                WHERE k.keyword='新增默认词' ORDER BY s.channel
            """)
            channels = [row[0] for row in cur.fetchall()]
        self.assertEqual(channels, ["sogou", "souyisou"])
        self.assertTrue(self.sched._channel_has_state("souyisou"))
        self.assertTrue(self.sched._channel_has_state("sogou"))

    def test_real_redis_small_set_poison_backoff_prevents_starvation(self):
        for index in range(self.sched.STALE_REDIS_BATCH_LIMIT):
            for prefix, owner in (("poison", "vm-poison"), ("good", "vm-good")):
                keyword = f"{prefix}-{index:02d}"
                member = self.sched._lease_member("wechat_pc", keyword)
                self.redis.sadd(self.sched.CLAIMED_SET, member)
                self.redis.hset(self.sched._lease_key("wechat_pc", keyword), mapping={
                    "status": "running",
                    "claimer": owner,
                    "channel": "wechat_pc",
                    "keyword": keyword,
                    "lease_id": f"lease-{keyword}",
                    "lease_protocol": "2",
                    "lease_required": "1",
                    "last_claimed": (
                        datetime.now() - timedelta(minutes=30)
                    ).isoformat(),
                })

        original = self.sched._persist_recovered_claims
        self.sched._persist_recovered_claims = (
            lambda records: records[0]["keyword"].startswith("good-")
        )
        recovered = []
        try:
            for _ in range(4):
                recovered.extend(self.sched.recover_stale_claims(15))
                if any(keyword.startswith("good-") for keyword in recovered):
                    break
        finally:
            self.sched._persist_recovered_claims = original

        self.assertTrue(any(keyword.startswith("good-") for keyword in recovered))

    def test_poisoned_stale_lease_does_not_block_valid_lease(self):
        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state s SET status='completed'
                FROM keywords k
                WHERE s.keyword_id=k.id AND k.keyword='同一关键词'
                  AND s.channel='wechat_pc'
            """)
            cur.execute("""
                INSERT INTO keywords(keyword,status,next_collect_time,channels)
                VALUES ('坏租约','pending',NOW(),ARRAY['wechat_pc']),
                       ('好租约','pending',NOW(),ARRAY['wechat_pc'])
                RETURNING id, keyword
            """)
            ids = {keyword: keyword_id for keyword_id, keyword in cur.fetchall()}
            cur.execute("""
                INSERT INTO keyword_channel_state
                    (keyword_id,channel,status,next_collect_time)
                VALUES (%s,'wechat_pc','pending',NOW()),
                       (%s,'wechat_pc','pending',NOW())
            """, (ids["坏租约"], ids["好租约"]))
        self.pg.commit()

        claims = self.sched.claim_task("wechat_pc", "vm-batch", 2, True)
        by_keyword = {claim["keyword"]: claim for claim in claims}
        bad = by_keyword["坏租约"]
        good = by_keyword["好租约"]
        for claim in (bad, good):
            self.redis.hset(
                self.sched._lease_key("wechat_pc", claim["keyword"]),
                "last_claimed",
                (datetime.now() - timedelta(minutes=30)).isoformat(),
            )
        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state s SET claimer='vm-other'
                FROM keywords k
                WHERE s.keyword_id=k.id AND k.keyword=%s
            """, (bad["keyword"],))
        self.pg.commit()

        recovered = self.sched.recover_stale_claims(15)
        self.assertIn(good["keyword"], recovered)
        self.assertNotIn(bad["keyword"], recovered)
        self.assertEqual(
            self._state_for(good["keyword"], "wechat_pc"), ("pending", None)
        )
        self.assertEqual(
            self._state_for(bad["keyword"], "wechat_pc"),
            ("running", "vm-other"),
        )

    def test_pg_row_lock_times_out_before_redis_lease_lock_and_retry_succeeds(self):
        claim = self.sched.claim_task(
            "wechat_pc", "vm-lock", 1, lease_aware=True
        )[0]
        lock_conn = __import__("psycopg2").connect(os.environ["DATABASE_URL"])
        try:
            with lock_conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id FROM keyword_channel_state s
                    JOIN keywords k ON k.id=s.keyword_id
                    WHERE k.keyword='同一关键词' AND s.channel='wechat_pc'
                    FOR UPDATE
                """)
            started = datetime.now()
            self.assertFalse(self.sched.report_result(
                "同一关键词", 2, True, device_id="vm-lock",
                channel="wechat_pc", lease_id=claim["lease_id"],
            ))
            elapsed = (datetime.now() - started).total_seconds()
            self.assertLess(elapsed, 10)
            member = self.sched._lease_member("wechat_pc", "同一关键词")
            self.assertTrue(self.redis.sismember(self.sched.CLAIMED_SET, member))
        finally:
            lock_conn.rollback()
            lock_conn.close()

        self.assertTrue(self.sched.report_result(
            "同一关键词", 2, True, device_id="vm-lock",
            channel="wechat_pc", lease_id=claim["lease_id"],
        ))

    def test_pg_running_without_redis_marker_is_reconciled(self):
        claim = self.sched.claim_task(
            "wechat_pc", "vm-orphan", 1, lease_aware=True
        )[0]
        member = self.sched._lease_member("wechat_pc", "同一关键词")
        self.redis.srem(self.sched.CLAIMED_SET, member)
        self.redis.delete(self.sched._lease_key("wechat_pc", "同一关键词"))
        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state SET last_claimed=NOW()-INTERVAL '30 minutes'
                WHERE channel='wechat_pc'
            """)
        self.pg.commit()

        self.assertEqual(self.sched.recover_stale_claims(15), [claim["keyword"]])
        self.assertEqual(self._state("wechat_pc"), ("pending", None))

    def test_orphan_waits_for_recent_matching_current_keyword_then_recovers(self):
        claim = self.sched.claim_task(
            "wechat_pc", "vm-active", 1, lease_aware=True
        )[0]
        member = self.sched._lease_member("wechat_pc", claim["keyword"])
        self.redis.srem(self.sched.CLAIMED_SET, member)
        self.redis.delete(self.sched._lease_key("wechat_pc", claim["keyword"]))
        with self.pg.cursor() as cur:
            cur.execute("""
                UPDATE keyword_channel_state SET last_claimed=NOW()-INTERVAL '30 minutes'
                WHERE channel='wechat_pc'
            """)
            cur.execute("""
                INSERT INTO devices(
                    device_id,device_type,channel,status,current_keyword,
                    last_heartbeat,started_at
                ) VALUES ('vm-active','pc','wechat_pc','online',%s,NOW(),NOW())
            """, (claim["keyword"],))
        self.pg.commit()

        self.assertEqual(self.sched.recover_stale_claims(15), [])
        self.assertEqual(self._state("wechat_pc"), ("running", "vm-active"))

        with self.pg.cursor() as cur:
            cur.execute(
                "UPDATE devices SET current_keyword=NULL WHERE device_id='vm-active'"
            )
        self.pg.commit()
        self.assertEqual(self.sched.recover_stale_claims(15), [claim["keyword"]])
        self.assertEqual(self._state("wechat_pc"), ("pending", None))


if __name__ == "__main__":
    unittest.main()
