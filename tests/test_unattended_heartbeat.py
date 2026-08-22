"""无人值守独立心跳单元测试（不加载 Celery/UIA 实现）。"""

import threading
import time
import unittest
from types import SimpleNamespace

from wxsearch.config import UnattendedConfig
from wxsearch.unattended import CLAIM_TASK, HEARTBEAT_TASK, UnattendedRunner


class _Result:
    def __init__(self, value=True):
        self.value = value

    def get(self, timeout=None):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _App:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.closed = 0

    def send_task(self, task, args=None):
        self.calls.append((time.monotonic(), task, list(args or [])))
        value = self.results.pop(0) if self.results else True
        return _Result(value)

    def close(self):
        self.closed += 1


class _Logger:
    def __init__(self):
        self.messages = []

    def _add(self, level, message):
        self.messages.append((level, str(message)))

    def info(self, message):
        self._add("info", message)

    def warning(self, message):
        self._add("warning", message)

    def exception(self, message):
        self._add("exception", message)


class _Closable:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _runner(heartbeat_results=None, claim_results=None):
    """绕过运行时 Celery/UIA 构造，仅装配被测心跳状态机。"""
    runner = UnattendedRunner.__new__(UnattendedRunner)
    runner.uc = SimpleNamespace(
        vm_instance_id="vm-test-01",
        device_type="pc",
        channel="wechat_pc",
        max_keywords=2,
        claim_timeout=1,
        report_retry_attempts=3,
        report_retry_backoff_sec=0,
        idle_sleep_sec=0,
        round_sleep_sec=0,
    )
    runner.dist = SimpleNamespace(enabled=True, broker_url="redis://test")
    runner.log = _Logger()
    runner._app = _App(claim_results)
    runner._heartbeat_app = _App(heartbeat_results)
    runner._heartbeat_interval_sec = 0.03
    runner._heartbeat_result_timeout_sec = 0.1
    runner._heartbeat_claim_max_age_sec = 0.2
    runner._heartbeat_failure_threshold = 2
    runner._state_lock = threading.Lock()
    runner._heartbeat_send_lock = threading.Lock()
    runner._current_keyword = None
    runner._active_claims = {}
    runner._heartbeat_failures = 0
    runner._heartbeat_confirmed_once = False
    runner._last_heartbeat_success_monotonic = None
    runner._claim_paused_logged = False
    runner._heartbeat_stop = threading.Event()
    runner._heartbeat_wake = threading.Event()
    runner._heartbeat_ready = threading.Event()
    runner._heartbeat_thread = None
    runner._shutdown_done = False
    runner.collector = SimpleNamespace(db=_Closable())
    return runner


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class UnattendedHeartbeatTests(unittest.TestCase):
    def test_default_logical_channel_matches_seeded_scheduler_channel(self):
        self.assertEqual(UnattendedConfig().channel, "souyisou")

    def test_heartbeat_continues_during_blocking_collection(self):
        runner = _runner()
        runner._set_active_claims([{"keyword": "长关键词", "lease_id": "lease-long"}])
        runner._set_current_keyword("长关键词")
        runner._start_heartbeat()

        # 主采集线程被 UIA 阻塞时，独立线程仍应跨过多个心跳周期。
        time.sleep(0.13)
        runner._heartbeat_stop.set()
        runner._heartbeat_wake.set()
        runner._heartbeat_thread.join(timeout=0.5)

        calls = [c for c in runner._heartbeat_app.calls if c[1] == HEARTBEAT_TASK]
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(c[2][3] == "长关键词" for c in calls))
        self.assertTrue(all(
            c[2][4] == [{"keyword": "长关键词", "lease_id": "lease-long"}]
            for c in calls
        ))

    def test_keyword_completion_clears_busy_and_stops_renewal(self):
        runner = _runner()
        runner._set_active_claims([
            {"keyword": "A", "lease_id": "lease-a"},
            {"keyword": "B", "lease_id": "lease-b"},
        ])
        runner._set_current_keyword("A")
        runner._start_heartbeat()
        self.assertTrue(_wait_until(lambda: bool(runner._heartbeat_app.calls)))

        runner._complete_keyword("A", "lease-a")
        self.assertTrue(_wait_until(
            lambda: any(
                c[2][3] is None
                and c[2][4] == [{"keyword": "B", "lease_id": "lease-b"}]
                for c in runner._heartbeat_app.calls
            )
        ))
        runner._shutdown()

    def test_consecutive_heartbeat_failures_pause_and_recovery_resumes_claim(self):
        runner = _runner(
            heartbeat_results=[False, False, True],
            claim_results=[[{"keyword": "恢复后的关键词", "lease_id": "lease-ok"}]],
        )
        self.assertFalse(runner._report_heartbeat())
        self.assertEqual(runner._claim(), [])
        self.assertFalse(runner._report_heartbeat())
        self.assertEqual(runner._claim(), [])
        self.assertFalse(any(c[1] == CLAIM_TASK for c in runner._app.calls))

        self.assertTrue(runner._report_heartbeat())
        self.assertEqual(
            runner._claim(),
            [{"keyword": "恢复后的关键词", "lease_id": "lease-ok"}],
        )
        claim_call = next(c for c in runner._app.calls if c[1] == CLAIM_TASK)
        self.assertIs(claim_call[2][3], True)

    def test_alive_but_stuck_heartbeat_thread_cannot_claim_after_success_is_stale(self):
        runner = _runner(claim_results=[[
            {"keyword": "不应领取", "lease_id": "lease-stale"}
        ]])
        runner._heartbeat_ready.set()
        runner._heartbeat_confirmed_once = True
        runner._heartbeat_failures = 0
        runner._last_heartbeat_success_monotonic = time.monotonic() - 1.0
        runner._heartbeat_thread = SimpleNamespace(is_alive=lambda: True)

        self.assertEqual(runner._claim(), [])
        self.assertFalse(any(c[1] == CLAIM_TASK for c in runner._app.calls))

    def test_report_passes_lease_id_as_backward_compatible_trailing_argument(self):
        runner = _runner(claim_results=[True])
        self.assertTrue(runner._report("关键词", 2, True, lease_id="lease-123"))
        report_call = runner._app.calls[-1]
        self.assertEqual(report_call[2][-1], "lease-123")

    def test_report_retries_transient_failure_with_same_lease(self):
        runner = _runner(claim_results=[False, True])
        self.assertTrue(runner._report("关键词", 2, True, lease_id="lease-retry"))
        self.assertEqual(len(runner._app.calls), 2)
        self.assertTrue(all(
            call[2][-1] == "lease-retry" for call in runner._app.calls
        ))

    def test_lost_batch_lease_stops_before_next_keyword(self):
        runner = _runner()
        claims = [
            {"keyword": "A", "lease_id": "lease-a"},
            {"keyword": "B", "lease_id": "lease-b"},
            {"keyword": "C", "lease_id": "lease-c"},
        ]
        runner._refresh_collect_settings = lambda: None
        runner._claim = lambda: claims
        confirmations = iter([True, False])
        runner._report_heartbeat = lambda: next(confirmations)
        collected = []
        runner.collector = SimpleNamespace(
            collect_keyword=lambda keyword: collected.append(keyword) or 1,
            db=_Closable(),
        )
        runner._report = lambda *args, **kwargs: True

        runner._run_one_round()

        self.assertEqual(collected, ["A"])
        self.assertEqual(runner._active_claims, {})

    def test_shutdown_is_idempotent(self):
        runner = _runner()
        runner._start_heartbeat()
        runner._shutdown()
        runner._shutdown()

        self.assertEqual(runner.collector.db.closed, 1)
        self.assertEqual(runner._app.closed, 1)
        self.assertEqual(runner._heartbeat_app.closed, 1)
        self.assertFalse(runner._heartbeat_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
