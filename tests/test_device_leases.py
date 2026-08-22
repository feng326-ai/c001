"""租约协议 v2：渠道身份、续租、fencing 与故障安全测试。"""

import sys
import types
import unittest
from datetime import datetime, timedelta

try:
    import redis  # noqa: F401
except ModuleNotFoundError:
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = object
    redis_stub.ConnectionError = OSError
    sys.modules["redis"] = redis_stub

from wxsearch.task_scheduler import DistributedTaskScheduler


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Redis:
    def __init__(self):
        self.sets = {}
        self.hashes = {}
        self.values = {}

    def lock(self, *args, **kwargs):
        return _Lock()

    def sadd(self, key, *values):
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(values)
        return len(target) - before

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sscan(self, key, cursor=0, count=10):
        values = sorted(self.sets.get(key, set()))
        start = int(cursor or 0)
        end = min(len(values), start + max(1, int(count)))
        next_cursor = 0 if end >= len(values) else end
        return next_cursor, values[start:end]

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def srem(self, key, *values):
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.difference_update(values)
        return before - len(target)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})
        return 1

    def delete(self, key):
        existed = key in self.values or key in self.hashes or key in self.sets
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.sets.pop(key, None)
        return int(existed)

    def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = str(value)
        return True

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]


class _FailFirstSaddRedis(_Redis):
    def __init__(self):
        super().__init__()
        self.fail_next_sadd = True

    def sadd(self, key, *values):
        if self.fail_next_sadd:
            self.fail_next_sadd = False
            raise OSError("ambiguous Redis SADD failure")
        return super().sadd(key, *values)


class _AllAtOnceSscanRedis(_Redis):
    """模拟 Redis 小 Set：忽略 COUNT，一次返回全部且 cursor=0。"""

    def sscan(self, key, cursor=0, count=10):
        return 0, sorted(self.sets.get(key, set()))


class _Db:
    def __init__(self, query_rows=None, query_results=None, write_results=None):
        self.query_rows = list(query_rows or [])
        self.query_results = list(query_results or [])
        self.write_results = list(write_results or [])
        self.queries = []
        self.writes = []

    def execute_query(self, sql, params=None):
        self.queries.append((sql, params))
        if self.query_results:
            return list(self.query_results.pop(0))
        return list(self.query_rows)

    def execute_write(self, sql, params=None):
        self.writes.append((sql, params))
        return self.write_results.pop(0) if self.write_results else 1


def _scheduler(fake_redis=None, fake_db=None):
    sched = DistributedTaskScheduler.__new__(DistributedTaskScheduler)
    sched.redis = fake_redis or _Redis()
    db = fake_db or _Db()
    sched._db = lambda: db
    return sched, db


def _seed_claim(sched, keyword, device, channel="wechat_pc", minutes_old=30,
                lease_id="lease-1", protocol="2"):
    if protocol == "legacy":
        member = keyword
        key = f"{sched.KW_PREFIX}{keyword}"
        lease_protocol = ""
        lease_required = "0"
    else:
        member = sched._lease_member(channel, keyword)
        key = sched._lease_key(channel, keyword)
        lease_protocol = str(protocol)
        lease_required = "1" if str(protocol) == "2" else "0"
    sched.redis.sadd(sched.CLAIMED_SET, member)
    sched.redis.hset(key, mapping={
        "status": "running",
        "claimer": device,
        "channel": channel,
        "keyword": keyword,
        "lease_id": lease_id,
        "lease_protocol": lease_protocol,
        "lease_required": lease_required,
        "last_claimed": (datetime.now() - timedelta(minutes=minutes_old)).isoformat(),
    })
    return member, key


class DeviceLeaseTests(unittest.TestCase):
    def test_claim_v2_and_legacy_responses_use_compatible_storage(self):
        sched, _ = _scheduler()
        sched._channel_has_state = lambda channel: True
        available = [["v2-keyword"], ["legacy-keyword"]]
        sched._get_available_keywords = lambda channel, limit: available.pop(0)

        v2 = sched.claim_task("wechat_pc", "vm-v2", 1, lease_aware=True)
        legacy = sched.claim_task("wechat_pc", "vm-old", 1, lease_aware=False)

        self.assertEqual(v2[0]["keyword"], "v2-keyword")
        self.assertTrue(v2[0]["lease_id"])
        self.assertEqual(legacy, ["legacy-keyword"])
        self.assertEqual(len(sched.redis.smembers(sched.CLAIMED_SET)), 2)
        self.assertIn("legacy-keyword", sched.redis.smembers(sched.CLAIMED_SET))
        self.assertNotIn(
            sched._lease_member("wechat_pc", "legacy-keyword"),
            sched.redis.smembers(sched.CLAIMED_SET),
        )

    def test_same_keyword_has_independent_channel_lease_keys(self):
        sched, _ = _scheduler()
        wx_member, wx_key = _seed_claim(
            sched, "同一关键词", "vm-wx", channel="wechat_pc", lease_id="wx-lease"
        )
        sg_member, sg_key = _seed_claim(
            sched, "同一关键词", "vm-sg", channel="sogou", lease_id="sg-lease"
        )
        self.assertNotEqual(wx_member, sg_member)
        self.assertNotEqual(wx_key, sg_key)
        self.assertEqual(sched._find_lease("wechat_pc", "同一关键词")["data"]["claimer"], "vm-wx")
        self.assertEqual(sched._find_lease("sogou", "同一关键词")["data"]["claimer"], "vm-sg")

    def test_renew_requires_matching_owner_channel_and_lease_id(self):
        sched, _ = _scheduler()
        _, key = _seed_claim(sched, "owned", "vm-a", lease_id="lease-a")
        old_time = sched.redis.hashes[key]["last_claimed"]
        renewed = sched.renew_claims(
            "vm-a", "wechat_pc", [{"keyword": "owned", "lease_id": "lease-a"}]
        )
        self.assertEqual(renewed, ["owned"])
        self.assertGreater(sched.redis.hashes[key]["last_claimed"], old_time)
        self.assertEqual(
            sched.renew_claims(
                "vm-a", "wechat_pc", [{"keyword": "owned", "lease_id": "old"}]
            ),
            [],
        )

    def test_v2_stale_claim_recovers_even_when_device_heartbeat_is_recent(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[("vm-a",)]))
        member, _ = _seed_claim(sched, "failed-report", "vm-a")
        sched._persist_recovered_claims = lambda records: True
        self.assertEqual(sched.recover_stale_claims(15), ["failed-report"])
        self.assertNotIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_legacy_stale_claim_is_protected_by_recent_device_heartbeat(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[("vm-old",)]))
        member, _ = _seed_claim(sched, "legacy-long", "vm-old", protocol="legacy")
        sched._persist_recovered_claims = lambda records: True
        self.assertEqual(sched.recover_stale_claims(15), [])
        self.assertIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_legacy_recovery_only_marker_ignores_recent_device_heartbeat(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[("vm-old", None)]))
        member, key = _seed_claim(
            sched, "legacy-init-failed", "vm-old", protocol="legacy"
        )
        sched.redis.hset(key, mapping={"recovery_only": "1"})
        sched._persist_recovered_claims = lambda records: True

        self.assertEqual(sched.recover_stale_claims(15), ["legacy-init-failed"])
        self.assertNotIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_recovery_pg_failure_keeps_redis_owner_for_retry(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[]))
        member, _ = _seed_claim(sched, "keep-on-pg-error", "vm-a")
        sched._persist_recovered_claims = lambda records: False
        self.assertEqual(sched.recover_stale_claims(15), [])
        self.assertIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_poisoned_lease_does_not_block_other_stale_recovery(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[]))
        bad_member, _ = _seed_claim(sched, "bad", "vm-bad")
        good_member, _ = _seed_claim(sched, "good", "vm-good")

        def persist(records):
            return records[0]["keyword"] != "bad"

        sched._persist_recovered_claims = persist
        self.assertEqual(sched.recover_stale_claims(15), ["good"])
        self.assertIn(bad_member, sched.redis.sets[sched.CLAIMED_SET])
        self.assertNotIn(good_member, sched.redis.sets[sched.CLAIMED_SET])

    def test_orphaned_pg_running_without_redis_marker_is_recovered(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[
            ("orphan", "wechat_pc", "vm-orphan"),
        ]))
        persisted = []
        sched._persist_recovered_claims = (
            lambda records: persisted.extend(records) or True
        )

        self.assertEqual(sched.recover_stale_claims(15), ["orphan"])
        self.assertEqual(persisted[0]["data"]["claimer"], "vm-orphan")

    def test_stale_recovery_is_bounded_per_round(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[]))
        for index in range(sched.STALE_RECOVERY_BATCH_LIMIT + 5):
            _seed_claim(sched, f"stale-{index:02d}", f"vm-{index:02d}")
        persisted = []
        sched._persist_recovered_claims = (
            lambda records: persisted.extend(records) or True
        )

        recovered = sched.recover_stale_claims(15)
        self.assertEqual(len(recovered), sched.STALE_REDIS_BATCH_LIMIT)
        self.assertEqual(len(persisted), sched.STALE_REDIS_BATCH_LIMIT)

    def test_stale_cursor_advances_past_persistent_poison_markers(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[]))
        for index in range(sched.STALE_REDIS_BATCH_LIMIT):
            _seed_claim(sched, f"a-poison-{index:02d}", "vm-poison")
            _seed_claim(sched, f"z-good-{index:02d}", "vm-good")

        sched._persist_recovered_claims = (
            lambda records: not records[0]["keyword"].startswith("a-poison")
        )
        self.assertEqual(sched.recover_stale_claims(15), [])
        recovered = sched.recover_stale_claims(15)

        self.assertEqual(len(recovered), sched.STALE_REDIS_BATCH_LIMIT)
        self.assertTrue(all(keyword.startswith("z-good") for keyword in recovered))

    def test_small_redis_set_poison_backoff_allows_later_stale_items(self):
        sched, _ = _scheduler(
            fake_redis=_AllAtOnceSscanRedis(),
            fake_db=_Db(query_rows=[]),
        )
        for index in range(sched.STALE_REDIS_BATCH_LIMIT):
            _seed_claim(sched, f"a-poison-{index:02d}", "vm-poison")
            _seed_claim(sched, f"z-good-{index:02d}", "vm-good")
        sched._persist_recovered_claims = (
            lambda records: not records[0]["keyword"].startswith("a-poison")
        )

        self.assertEqual(sched.recover_stale_claims(15), [])
        recovered = sched.recover_stale_claims(15)

        self.assertEqual(len(recovered), sched.STALE_REDIS_BATCH_LIMIT)
        self.assertTrue(all(keyword.startswith("z-good") for keyword in recovered))

    def test_redis_stale_batch_reserves_capacity_for_pg_orphan_scan(self):
        sched, _ = _scheduler(fake_db=_Db(query_results=[
            [],
            [(101, "orphan-fair", "wechat_pc", "vm-orphan")],
        ]))
        for index in range(sched.STALE_REDIS_BATCH_LIMIT + 5):
            _seed_claim(sched, f"redis-{index:02d}", f"vm-{index:02d}")
        sched._persist_recovered_claims = lambda records: True

        recovered = sched.recover_stale_claims(15)

        self.assertEqual(len(recovered), sched.STALE_REDIS_BATCH_LIMIT + 1)
        self.assertIn("orphan-fair", recovered)
        self.assertEqual(
            sched.redis.get(sched.ORPHAN_CURSOR_KEY), "101"
        )

    def test_late_result_with_old_lease_id_cannot_release_new_owner(self):
        sched, _ = _scheduler()
        member, _ = _seed_claim(sched, "reassigned", "vm-b", lease_id="new-lease")
        called = []
        sched._persist_result_state = lambda **kwargs: called.append(kwargs) or "committed"
        accepted = sched.report_result(
            "reassigned", 3, True, device_id="vm-a", channel="wechat_pc",
            lease_id="old-lease",
        )
        self.assertFalse(accepted)
        self.assertFalse(called)
        self.assertIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_report_pg_failure_does_not_release_redis_lease(self):
        sched, _ = _scheduler()
        member, _ = _seed_claim(sched, "pg-fail", "vm-a", lease_id="lease-a")
        sched._persist_result_state = lambda **kwargs: "failed"
        accepted = sched.report_result(
            "pg-fail", 1, True, device_id="vm-a", channel="wechat_pc",
            lease_id="lease-a",
        )
        self.assertFalse(accepted)
        self.assertIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_report_releases_only_matching_channel_lease(self):
        sched, _ = _scheduler()
        wx_member, _ = _seed_claim(
            sched, "shared", "vm-wx", channel="wechat_pc", lease_id="wx-lease"
        )
        sg_member, _ = _seed_claim(
            sched, "shared", "vm-sg", channel="sogou", lease_id="sg-lease"
        )
        sched._persist_result_state = lambda **kwargs: "committed"
        sched._record_collect_task = lambda *args, **kwargs: None
        self.assertTrue(sched.report_result(
            "shared", 2, True, device_id="vm-wx", channel="wechat_pc",
            lease_id="wx-lease",
        ))
        self.assertNotIn(wx_member, sched.redis.sets[sched.CLAIMED_SET])
        self.assertIn(sg_member, sched.redis.sets[sched.CLAIMED_SET])

    def test_legacy_protocol_result_remains_backward_compatible(self):
        sched, _ = _scheduler()
        member, _ = _seed_claim(
            sched, "legacy-result", "vm-old", lease_id="opaque", protocol="1"
        )
        sched._persist_result_state = lambda **kwargs: "committed"
        sched._record_collect_task = lambda *args, **kwargs: None

        self.assertTrue(sched.report_result(
            "legacy-result", 1, True, device_id="vm-old", channel="wechat_pc"
        ))
        self.assertNotIn(member, sched.redis.sets[sched.CLAIMED_SET])

    def test_claim_pg_failure_does_not_create_redis_lease(self):
        sched, _ = _scheduler(fake_db=_Db(write_results=[0, 1]))
        sched._get_available_keywords = lambda channel, limit: ["claim-fail"]
        sched._channel_has_state = lambda channel: True
        with self.assertRaises(RuntimeError):
            sched.claim_task("wechat_pc", "vm-a", 1, lease_aware=True)
        self.assertFalse(sched.redis.smembers(sched.CLAIMED_SET))

    def test_claim_refuses_unseeded_channel_instead_of_global_fallback(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[]))
        called = []
        sched._get_available_keywords = (
            lambda channel, limit: called.append((channel, limit)) or ["unsafe"]
        )

        self.assertEqual(
            sched.claim_task("wechat_pc", "vm-a", 1, lease_aware=False), []
        )
        self.assertFalse(called)

    def test_channel_seed_check_rejects_partial_backfill(self):
        sched, _ = _scheduler(fake_db=_Db(query_rows=[(True, False)]))
        self.assertFalse(sched._channel_has_state("wechat_pc"))

    def test_redis_claim_failure_and_pg_compensation_failure_keeps_marker(self):
        fake_redis = _FailFirstSaddRedis()
        sched, _ = _scheduler(
            fake_redis=fake_redis,
            fake_db=_Db(write_results=[1, 0]),
        )
        sched._get_available_keywords = lambda channel, limit: ["recoverable"]
        sched._channel_has_state = lambda channel: True

        with self.assertRaises(OSError):
            sched.claim_task("wechat_pc", "vm-a", 1, lease_aware=True)

        member = sched._lease_member("wechat_pc", "recoverable")
        self.assertIn(member, fake_redis.smembers(sched.CLAIMED_SET))
        self.assertEqual(
            fake_redis.hgetall(sched._lease_key("wechat_pc", "recoverable"))["claimer"],
            "vm-a",
        )

    def test_offline_candidate_is_ignored_if_conditional_update_loses_race(self):
        sched, db = _scheduler(fake_db=_Db(query_rows=[("vm-a",)], write_results=[0]))
        self.assertEqual(sched.mark_offline_devices(600), [])
        db.write_results = [1]
        self.assertEqual(sched.mark_offline_devices(600), ["vm-a"])

    def test_force_release_is_bounded_and_uses_safe_batch(self):
        sched, _ = _scheduler(fake_db=_Db())
        for index in range(sched.STALE_REDIS_BATCH_LIMIT + 5):
            _seed_claim(sched, f"force-{index:02d}", "vm-force")
        sched._persist_recovered_claims = lambda records: True

        released = sched.force_release_all("vm-force")

        self.assertEqual(len(released), sched.STALE_REDIS_BATCH_LIMIT)


if __name__ == "__main__":
    unittest.main()
