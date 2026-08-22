"""
告警层（阶段四·无人值守 第 2 步）——默认安全、可开关、占位不崩。

定位：由 celery-beat 周期触发（在 worker 执行），基于已落地的心跳数据与
基础设施可达性，产出告警。设计与 AI 层/调度层一致：
  - 总开关 ALERT_ENABLED（默认 true）；关闭时 evaluate_and_alert() 直接跳过；
  - 外发仅在配置了 ALERT_WEBHOOK_URL 时进行，否则只写 WARNING 日志（默认零外部依赖）；
  - 冷却去重：同一告警键在 ALERT_COOLDOWN_MINUTES 内不重复外发，避免刷屏；
  - 永不抛异常：告警是加分项，任何失败只 log，绝不拖垮 worker。

检查项（均可在 worker 侧执行）：
  1. PG 可达性（DatabaseConnector.health_check）；
  2. Redis 可达性（ping）；
  3. 心跳新鲜度：wxsearch:heartbeat:worker 缺失或超过 ALERT_HEARTBEAT_TIMEOUT_SECONDS；
  4. 连续采集失败：wxsearch:collect:fail_streak 达到 ALERT_FAILURE_THRESHOLD。

局限：真正的“worker/beat 进程整体宕机”需外部看门狗检测（本 worker 侧任务无法自检），
留作后续增量；本步先把可自检项与通知通道打通。

环境变量：
  ALERT_ENABLED                    : 1/true/yes/on 开启（缺省 true）。
  ALERT_WEBHOOK_URL                : 通用 JSON webhook 地址（缺省空=只记日志）。
  ALERT_HEARTBEAT_TIMEOUT_SECONDS  : 心跳超时秒数（缺省 180，即 3 个心跳周期）。
  ALERT_COOLDOWN_MINUTES           : 同键冷却分钟（缺省 30）。
  ALERT_FAILURE_THRESHOLD          : 连续采集失败阈值（缺省 5）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import redis

log = logging.getLogger(__name__)

# 与 task_scheduler 约定共享的 Redis 键（保持字面量一致）
HEARTBEAT_KEY = "wxsearch:heartbeat:worker"
FAIL_STREAK_KEY = "wxsearch:collect:fail_streak"
ALERT_SENT_PREFIX = "wxsearch:alert:sent:"


def _truthy(val) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Alert:
    """一条告警。key 用于冷却去重；level 供通道分级。"""
    key: str
    level: str          # warning / critical
    subject: str
    message: str

    def to_dict(self) -> dict:
        return {"key": self.key, "level": self.level,
                "subject": self.subject, "message": self.message}


class Alerter:
    """告警评估与派发。检查基础设施与心跳/失败流，命中则日志+可选 webhook。"""

    def __init__(self, redis_client=None, enabled: bool = True,
                 webhook_url: str = "", heartbeat_timeout: int = 180,
                 cooldown_minutes: int = 30, failure_threshold: int = 5):
        self.enabled = bool(enabled)
        self.webhook_url = (webhook_url or "").strip()
        self.heartbeat_timeout = heartbeat_timeout
        self.cooldown_seconds = max(1, cooldown_minutes * 60)
        self.failure_threshold = failure_threshold

        if redis_client is not None:
            self.redis = redis_client
        else:
            url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            self.redis = redis.Redis.from_url(
                url, decode_responses=True, socket_connect_timeout=5)

    @classmethod
    def from_env(cls) -> "Alerter":
        return cls(
            enabled=_truthy(os.getenv("ALERT_ENABLED", "true")),
            webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
            heartbeat_timeout=_int_env("ALERT_HEARTBEAT_TIMEOUT_SECONDS", 180),
            cooldown_minutes=_int_env("ALERT_COOLDOWN_MINUTES", 30),
            failure_threshold=_int_env("ALERT_FAILURE_THRESHOLD", 5),
        )

    @staticmethod
    def _db():
        from .db_connector import DatabaseConnector
        return DatabaseConnector()

    # ---- 采集结果计数（供 task_scheduler.report_result 调用） ----
    def record_collection_result(self, success: bool) -> int:
        """记录一次采集结果：成功清零连续失败计数，失败累加。返回当前失败流长度。"""
        try:
            if success:
                self.redis.delete(FAIL_STREAK_KEY)
                return 0
            return int(self.redis.incr(FAIL_STREAK_KEY))
        except Exception as e:  # noqa: BLE001
            log.error(f"记录采集失败流异常：{e}")
            return -1

    # ---- 评估：返回命中的告警列表（不外发） ----
    def evaluate(self) -> List[Alert]:
        alerts: List[Alert] = []

        # 1. PG 可达性
        try:
            pg_ok = self._db().health_check()
        except Exception:  # noqa: BLE001
            pg_ok = False
        if not pg_ok:
            alerts.append(Alert("pg_down", "critical", "PostgreSQL 不可达",
                                "worker 无法连接 PostgreSQL，去重入库将失败。"))

        # 2. Redis 可达性
        try:
            redis_ok = bool(self.redis.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
        if not redis_ok:
            alerts.append(Alert("redis_down", "critical", "Redis 不可达",
                                "worker 无法连接 Redis，调度与任务队列将中断。"))
            return alerts  # Redis 挂了，后续依赖 Redis 的检查无意义

        # 3. 心跳新鲜度
        hb = self.redis.get(HEARTBEAT_KEY)
        if hb is None:
            alerts.append(Alert("heartbeat_missing", "critical", "心跳缺失",
                                f"未读到 {HEARTBEAT_KEY}，心跳任务可能已停止。"))
        else:
            try:
                ts = json.loads(hb).get("ts")
                age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                if age > self.heartbeat_timeout:
                    alerts.append(Alert(
                        "heartbeat_stale", "critical", "心跳过期",
                        f"心跳已 {int(age)}s 未更新（阈值 {self.heartbeat_timeout}s）。"))
            except Exception as e:  # noqa: BLE001
                alerts.append(Alert("heartbeat_bad", "warning", "心跳数据异常",
                                    f"心跳内容无法解析：{e}"))

        # 4. 连续采集失败
        try:
            streak = int(self.redis.get(FAIL_STREAK_KEY) or 0)
        except (TypeError, ValueError):
            streak = 0
        if streak >= self.failure_threshold:
            alerts.append(Alert(
                "collect_fail_streak", "warning", "采集连续失败",
                f"连续采集失败 {streak} 次（阈值 {self.failure_threshold}），请检查采集节点。"))

        return alerts

    # ---- 派发：冷却去重 + 日志 + 可选 webhook ----
    def dispatch(self, alerts: List[Alert]) -> List[str]:
        sent: List[str] = []
        for a in alerts:
            if not self._acquire_cooldown(a.key):
                log.info(f"[告警] 冷却中，跳过外发：{a.key}")
                continue
            log.warning(f"[告警][{a.level}] {a.subject} - {a.message}")
            self._send_webhook(a)
            sent.append(a.key)
        return sent

    def _acquire_cooldown(self, key: str) -> bool:
        """冷却窗口内首次命中返回 True（可发），窗口内重复命中返回 False（抑制）。"""
        try:
            ok = self.redis.set(ALERT_SENT_PREFIX + key,
                                 datetime.now().isoformat(),
                                 nx=True, ex=self.cooldown_seconds)
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            log.error(f"冷却判定异常（默认放行）：{e}")
            return True

    def _send_webhook(self, alert: Alert) -> None:
        if not self.webhook_url:
            return
        payload = {
            "text": f"[{alert.level}] {alert.subject}\n{alert.message}",
            "level": alert.level,
            "key": alert.key,
            "ts": datetime.now().isoformat(),
        }
        try:
            import requests  # 容器内已装；缺失时走 except 兜底
            requests.post(self.webhook_url, json=payload, timeout=5)
        except Exception as e:  # noqa: BLE001
            log.error(f"[告警] webhook 外发失败（不影响主流程）：{e}")

    # ---- 入口：beat 任务调用 ----
    def evaluate_and_alert(self) -> dict:
        if not self.enabled:
            return {"skipped": "alert_disabled"}
        alerts = self.evaluate()
        sent = self.dispatch(alerts)
        return {"triggered": [a.key for a in alerts], "sent": sent}
