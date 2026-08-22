"""
分布式任务调度器
基于 Redis 实现关键词任务池 + 领取消耗锁 + 20 分钟更新周期控制。
支持多台 VM/实例并发领取，自动负载均衡。

无人值守地基（容器侧 celery-beat 周期驱动，见 wxsearch/tasks.py）：
  - requeue_due_keywords() : 到期的 completed 词翻回 pending，维持 20 分钟周期自转（补断链）；
  - recover_stale_claims() : 回收崩溃/掉线 VM 遗留的 running 词，绝不漏采；
  - health_snapshot()      : PG/Redis/池状态快照，供心跳与后续告警使用。

约定：
  - DB 访问统一走 DatabaseConnector.execute_query/execute_write（自动借还连接并提交），
    不再使用会泄漏连接、且该类并不提供的 cursor()/commit()/rollback()/close() 组合；
  - Redis 连接优先用 REDIS_URL（线上带 requirepass），见 from_env()；
  - 权威表结构以 docs/db_schema.sql 为准（keywords 无 updated_at 列）。
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict

import redis

log = logging.getLogger(__name__)


class DistributedTaskScheduler:
    """Redis 驱动的任务调度器"""

    # ==================== 命名空间配置 ====================

    KEYWORD_POOL = "wxsearch:keyword_pool"      # Set<keyword> (待采词库)
    CLAIMED_SET = "wxsearch:claimed_current"    # Set (当前被领取的关键词)
    LOCK_PREFIX = "wxsearch:task_lock:"         # Channel 锁前缀
    RESULT_PREFIX = "wxsearch:task_result:"     # 任务结果前缀
    KW_PREFIX = "wxsearch:kw:"                  # 单词运行态 hash 前缀
    HEARTBEAT_KEY = "wxsearch:heartbeat:worker" # worker 健康心跳

    # 渠道默认采集周期(分钟)：搜一搜(核心词)快、搜狗(拓展词)广而慢。
    # 优先级：kcs.update_cycle_minutes(词×渠道) > 渠道默认 > keywords.update_cycle_minutes > 20。
    CHANNEL_DEFAULT_CYCLE = {"souyisou": 20, "sogou": 180}

    def __init__(self, redis_client=None, redis_host: str = "localhost",
                 redis_port: int = 6379):
        """初始化 Redis 连接。

        优先使用传入的 redis_client（推荐经 from_env() 构造，带密码）；
        否则按 host/port 建默认连接（本地无密码场景，向后兼容旧调用）。
        """
        if redis_client is not None:
            self.redis = redis_client
        else:
            self.redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
                socket_connect_timeout=5,
            )

        try:
            self.redis.ping()
        except redis.ConnectionError as e:
            raise ConnectionError(f"Redis 连接失败：{e}")

    @classmethod
    def from_env(cls) -> "DistributedTaskScheduler":
        """worker/beat 容器默认入口：用 REDIS_URL（含 requirepass）建连接。"""
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=5
        )
        return cls(redis_client=client)

    # ==================== DB 助手 ====================

    @staticmethod
    def _db():
        """获取数据库连接器单例（惰性导入，避免非 DB 场景引入依赖）。"""
        from .db_connector import DatabaseConnector
        return DatabaseConnector()

    # ==================== 关键词注册 / 领取 / 上报 ====================

    def register_keywords(self, keywords: List[str], category: str = None) -> int:
        """注册一批关键词到任务池（Redis 集合去重 + 落库）。返回新增数量。"""
        inserted_count = 0
        for kw in keywords:
            self.redis.sadd(self.KEYWORD_POOL, kw)
            if self._insert_keyword_to_db(kw, category):
                inserted_count += 1
        return inserted_count

    def claim_task(self, channel: str, vm_instance_id: str,
                   max_keywords: int = 10) -> List[str]:
        """领取一批可采集的关键词（分布式锁防争抢；同时把 PG 置 running 防重复领取）。"""
        log.debug(f"[{channel}] {vm_instance_id} 开始领取，最大数量={max_keywords}")

        available_kws = self._get_available_keywords(channel, limit=max_keywords * 2)
        if not available_kws:
            log.info(f"[{channel}] 无可用关键词")
            return []

        lock_key = f"{self.LOCK_PREFIX}{channel}"
        claimed: List[str] = []

        # redis-py 客户端 lock：5 分钟持有超时，最多阻塞等待 10 秒
        with self.redis.lock(lock_key, timeout=300, blocking_timeout=10):
            now_iso = datetime.now().isoformat()
            for kw in available_kws[:max_keywords]:
                self.redis.hset(f"{self.KW_PREFIX}{kw}", mapping={
                    "status": "running",
                    "last_claimed": now_iso,
                    "claimer": vm_instance_id,
                    "channel": channel,
                })
                self.redis.sadd(self.CLAIMED_SET, kw)
                claimed.append(kw)

            if claimed:
                # PG 同步置 running，_get_available_keywords 只取 pending/failed，避免二次领取。
                if self._channel_has_state(channel):
                    # 按渠道调度：只置该渠道的 (词,渠道) 为 running，不影响其他渠道。
                    self._db().execute_write(
                        """
                        UPDATE keyword_channel_state s SET
                            status = 'running', claimer = %s, last_claimed = NOW()
                        FROM keywords k
                        WHERE s.keyword_id = k.id AND s.channel = %s AND k.keyword = ANY(%s)
                        """,
                        (vm_instance_id, channel, claimed),
                    )
                else:
                    self._db().execute_write(
                        "UPDATE keywords SET status = 'running' WHERE keyword = ANY(%s)",
                        (claimed,),
                    )

        log.info(f"[{channel}] {vm_instance_id} 成功领取 {len(claimed)} 个关键词")
        return claimed

    def report_result(self, keyword: str, articles_count: int, success: bool,
                      error_message: str = None, device_id: str = None,
                      channel: str = None) -> bool:
        """报告采集结果：更新 Redis 运行态 + 释放领取 + 落库统计 + 存历史。

        device_id/channel：设备级归属（有则额外写一行 collect_tasks 历史，支撑“每机采了哪些词/多少”统计）。
        """
        timestamp = datetime.now().isoformat()
        next_time = (datetime.now() + timedelta(minutes=20)).isoformat()

        kw_data = {
            "status": "completed" if success else "failed",
            "last_collect_time": timestamp,
            "last_collected_count": articles_count,
            "next_collect_time": next_time,
        }
        if not success and error_message:
            kw_data["error_message"] = error_message

        self.redis.hset(f"{self.KW_PREFIX}{keyword}", mapping=kw_data)
        # 成功或失败都释放领取占用（失败词回到 failed 状态可被重新领取）
        self.redis.srem(self.CLAIMED_SET, keyword)

        result_key = f"{self.RESULT_PREFIX}{keyword}:{timestamp}"
        self.redis.setex(result_key, 86400 * 30, json.dumps({
            "keyword": keyword,
            "articles_count": articles_count,
            "success": success,
            "completed_at": timestamp,
            "error_message": error_message,
        }, ensure_ascii=False))

        self._update_keyword_stats(keyword, articles_count, success)

        # 按渠道调度：该渠道已播种时，同步回写 (词,渠道) 状态与下次周期。
        if channel and self._channel_has_state(channel):
            self._update_channel_state_result(keyword, channel, articles_count, success)

        # 设备级历史：每次上报写一行 collect_tasks（有 device_id 才写，向后兼容）。
        if device_id:
            self._record_collect_task(device_id, keyword, channel, articles_count, success, error_message)
            # 设备级连续失败流（告警用）：成功清零、失败累加
            try:
                if success:
                    self.redis.delete(f"wxsearch:device:fail_streak:{device_id}")
                else:
                    self.redis.incr(f"wxsearch:device:fail_streak:{device_id}")
            except Exception:  # noqa: BLE001
                pass

        # 采集结果计数：成功清零、失败累加，供告警层判定“连续采集失败”。
        try:
            if success:
                self.redis.delete("wxsearch:collect:fail_streak")
            else:
                self.redis.incr("wxsearch:collect:fail_streak")
        except Exception as e:  # noqa: BLE001
            log.error(f"更新采集失败流异常：{e}")

        log.info(f"[结果上报] {keyword}: {'成功' if success else '失败'} ({articles_count} 条)")
        return True

    # ==================== 无人值守：调度 / 自愈 / 心跳 ====================

    def requeue_due_keywords(self) -> int:
        """把到期的 completed 词翻回 pending，维持 20 分钟周期自转（补断链）。返回重排条数。

        同时处理两层：keywords.status（旧/回退）与 keyword_channel_state（按渠道，各自到期）。
        """
        total = 0
        try:
            total += self._db().execute_write(
                """
                UPDATE keywords SET status = 'pending'
                WHERE enabled = TRUE
                  AND status = 'completed'
                  AND next_collect_time IS NOT NULL
                  AND next_collect_time <= NOW()
                """
            ) or 0
        except Exception as e:  # noqa: BLE001
            log.error(f"[调度] 到期重排失败：{e}")
        try:
            total += self._db().execute_write(
                """
                UPDATE keyword_channel_state SET status = 'pending'
                WHERE status = 'completed'
                  AND next_collect_time IS NOT NULL
                  AND next_collect_time <= NOW()
                """
            ) or 0
        except Exception as e:  # noqa: BLE001
            log.error(f"[调度] 渠道状态到期重排失败：{e}")
        if total:
            log.info(f"[调度] 到期重排 {total} 行 → pending")
        return total

    def recover_stale_claims(self, stale_after_minutes: int = 15) -> List[str]:
        """回收崩溃/掉线 VM 遗留的 running 词：last_claimed 超阈值 → 复位为 pending。"""
        threshold = datetime.now() - timedelta(minutes=stale_after_minutes)
        recovered: List[str] = []
        rec_channels: List[str] = []

        for kw in list(self.redis.smembers(self.CLAIMED_SET)):
            data = self.redis.hgetall(f"{self.KW_PREFIX}{kw}")
            last_claimed = data.get("last_claimed")
            stale = True
            if last_claimed:
                try:
                    stale = datetime.fromisoformat(last_claimed) < threshold
                except ValueError:
                    stale = True  # 时间无法解析，保守判定为崩溃遗留
            if stale:
                self.redis.hset(f"{self.KW_PREFIX}{kw}", mapping={
                    "status": "pending",
                    "next_collect_time": datetime.now().isoformat(),
                })
                self.redis.srem(self.CLAIMED_SET, kw)
                recovered.append(kw)
                rec_channels.append(data.get("channel") or "")

        if recovered:
            try:
                self._db().execute_write(
                    """
                    UPDATE keywords SET status = 'pending', next_collect_time = NOW()
                    WHERE keyword = ANY(%s) AND status = 'running'
                    """,
                    (recovered,),
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"[自愈] 回收落库失败：{e}")
            # 按渠道调度：精确复位 (词,渠道) 的 running 行，不误伤其他渠道/活跃设备。
            try:
                self._db().execute_write(
                    """
                    UPDATE keyword_channel_state s SET status = 'pending', claimer = NULL,
                        next_collect_time = NOW()
                    FROM keywords k, unnest(%s::text[], %s::text[]) AS p(kw, ch)
                    WHERE s.keyword_id = k.id AND k.keyword = p.kw
                      AND s.channel = p.ch AND s.status = 'running'
                    """,
                    (recovered, rec_channels),
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"[自愈] 渠道状态回收失败：{e}")
            log.warning(f"[自愈] 回收崩溃遗留关键词 {len(recovered)} 个：{recovered}")

        return recovered

    def health_snapshot(self) -> dict:
        """PG/Redis/池状态快照（供心跳与后续告警使用）。"""
        try:
            redis_ok = bool(self.redis.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False

        try:
            pg_ok = self._db().health_check()
        except Exception:  # noqa: BLE001
            pg_ok = False

        stats = {"pool_total": 0, "claimed": 0, "due": -1}
        try:
            stats = self.get_statistics()
        except Exception as e:  # noqa: BLE001
            log.error(f"health_snapshot 统计失败：{e}")

        return {
            "pg_ok": pg_ok,
            "redis_ok": redis_ok,
            "pool_total": stats.get("pool_total", 0),
            "claimed": stats.get("claimed", 0),
            "due": stats.get("due", -1),
            "ts": datetime.now().isoformat(),
        }

    # ==================== 查询 / 运维 ====================

    def get_claimed_keywords(self) -> List[str]:
        """获取当前所有 VM 正在领取的关键词"""
        return list(self.redis.smembers(self.CLAIMED_SET))

    def get_statistics(self) -> dict:
        """获取调度器统计：待采池总数 / 当前领取数 / 到期可采数。"""
        stats = {
            "pool_total": self.redis.scard(self.KEYWORD_POOL),
            "claimed": self.redis.scard(self.CLAIMED_SET),
        }
        try:
            rows = self._db().execute_query(
                """
                SELECT COUNT(*) FROM keywords
                WHERE enabled = TRUE
                  AND status IN ('pending', 'failed')
                  AND (next_collect_time IS NULL OR next_collect_time <= NOW())
                """
            )
            stats["due"] = rows[0][0] if rows else 0
        except Exception as e:  # noqa: BLE001
            log.error(f"统计 due 失败：{e}")
            stats["due"] = -1
        return stats

    def force_release_all(self, vm_instance_id: str) -> List[str]:
        """强制释放某 VM 的所有任务 (异常退出时使用)。"""
        released: List[str] = []
        rel_channels: List[str] = []
        for kw in list(self.redis.smembers(self.CLAIMED_SET)):
            data = self.redis.hgetall(f"{self.KW_PREFIX}{kw}")
            if data.get("claimer") == vm_instance_id:
                self.redis.hset(f"{self.KW_PREFIX}{kw}", mapping={
                    "status": "pending",
                    "next_collect_time": datetime.now().isoformat(),
                })
                released.append(kw)
                rel_channels.append(data.get("channel") or "")

        if released:
            self.redis.srem(self.CLAIMED_SET, *released)
            try:
                self._db().execute_write(
                    "UPDATE keywords SET status='pending', next_collect_time=NOW() "
                    "WHERE keyword = ANY(%s) AND status='running'",
                    (released,),
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"force_release_all 落库失败：{e}")
            try:
                self._db().execute_write(
                    """
                    UPDATE keyword_channel_state s SET status='pending', claimer=NULL,
                        next_collect_time=NOW()
                    FROM keywords k, unnest(%s::text[], %s::text[]) AS p(kw, ch)
                    WHERE s.keyword_id = k.id AND k.keyword = p.kw
                      AND s.channel = p.ch AND s.status='running'
                    """,
                    (released, rel_channels),
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"force_release_all 渠道状态释放失败：{e}")

        log.warning(f"VM {vm_instance_id} 异常释放任务：{len(released)} 个")
        return released

    # ==================== 设备注册 / 心跳 / 采集历史 ====================

    def heartbeat_device(self, device_id: str, device_type: str = "pc",
                         channel: str = "souyisou", current_keyword: str = None) -> bool:
        """设备心跳：upsert devices 表，刷新在线状态与当前在采关键词。失败只 log。"""
        if not device_id:
            return False
        try:
            self._db().execute_write(
                """
                INSERT INTO devices (device_id, device_type, channel, status,
                                     current_keyword, last_heartbeat, started_at)
                VALUES (%s, %s, %s, 'online', %s, NOW(), NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    device_type     = EXCLUDED.device_type,
                    channel         = EXCLUDED.channel,
                    status          = 'online',
                    current_keyword = EXCLUDED.current_keyword,
                    last_heartbeat  = NOW()
                """,
                (device_id, device_type, channel, current_keyword),
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"设备心跳写入失败 ({device_id})：{e}")
            return False

    def _record_collect_task(self, device_id: str, keyword: str, channel: str,
                             articles_count: int, success: bool, error_message: str = None):
        """写一行采集历史到 collect_tasks（按 keyword 解析 keyword_id）。属统计加分项，失败只 log。"""
        try:
            self._db().execute_write(
                """
                INSERT INTO collect_tasks
                    (keyword_id, channel, vm_instance, device_id, status,
                     articles_count, start_time, end_time, error_message)
                SELECT k.id, %s, %s, %s, %s, %s, NOW(), NOW(), %s
                FROM keywords k WHERE k.keyword = %s
                """,
                (
                    channel or "", device_id, device_id,
                    "completed" if success else "failed",
                    int(articles_count or 0),
                    (error_message or None),
                    keyword,
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"写 collect_tasks 失败 ({device_id}/{keyword})：{e}")

    def mark_offline_devices(self, timeout_seconds: int = 180) -> List[str]:
        """将心跳超时的设备置 offline，返回刚转为离线的设备 id 列表（供告警）。

        先 SELECT 阶旧在线设备，再 execute_write 更新（execute_query 不提交，不能用于 UPDATE）。
        """
        try:
            rows = self._db().execute_query(
                """
                SELECT device_id FROM devices
                WHERE status = 'online'
                  AND (last_heartbeat IS NULL
                       OR last_heartbeat < NOW() - make_interval(secs => %s))
                """,
                (int(timeout_seconds),),
            )
            stale = [r[0] for r in rows] if rows else []
            if stale:
                self._db().execute_write(
                    "UPDATE devices SET status = 'offline' WHERE device_id = ANY(%s)",
                    (stale,),
                )
            return stale
        except Exception as e:  # noqa: BLE001
            log.error(f"标记离线设备失败：{e}")
            return []

    # ==================== 内部方法 ====================

    def _channel_has_state(self, channel: str) -> bool:
        """该渠道是否已播种 keyword_channel_state（有则走按渠道调度，否则回退 keywords.status）。"""
        try:
            rows = self._db().execute_query(
                "SELECT 1 FROM keyword_channel_state WHERE channel = %s LIMIT 1", (channel,)
            )
            return bool(rows)
        except Exception:  # noqa: BLE001
            return False

    def _get_available_keywords(self, channel: str, limit: int = 100) -> List[str]:
        """从数据库获取可采关键词列表（按权重排序）。

        按渠道调度：该渠道已播种 keyword_channel_state 时，按 (词,渠道) 状态取可采词（
        搜一搜/搜狗各自独立循环）；否则回退到 keywords.status 旧逻辑（现网兼容）。
        条件：enabled 且 status ∈ (pending, failed) 且 next_collect_time 到期。
        """
        try:
            if self._channel_has_state(channel):
                rows = self._db().execute_query(
                    """
                    SELECT k.keyword FROM keyword_channel_state s
                    JOIN keywords k ON k.id = s.keyword_id
                    WHERE k.enabled = TRUE
                      AND s.channel = %s
                      AND k.channels @> ARRAY[%s]::text[]
                      AND s.status IN ('pending', 'failed')
                      AND (s.next_collect_time IS NULL OR s.next_collect_time <= NOW())
                    ORDER BY k.weight DESC, k.created_at ASC
                    LIMIT %s
                    """,
                    (channel, channel, limit),
                )
            else:
                rows = self._db().execute_query(
                    """
                    SELECT keyword FROM keywords
                    WHERE enabled = TRUE
                      AND status IN ('pending', 'failed')
                      AND (next_collect_time IS NULL OR next_collect_time <= NOW())
                    ORDER BY weight DESC, created_at ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
        except Exception as e:  # noqa: BLE001
            log.error(f"查询可采关键词失败：{e}")
            return []
        return [row[0] for row in rows]

    def _insert_keyword_to_db(self, keyword: str, category: str = None) -> bool:
        """插入或忽略关键词到数据库（新词立即可采：next_collect_time=NOW()）。"""
        try:
            rows = self._db().execute_write(
                """
                INSERT INTO keywords (keyword, category, created_at, next_collect_time)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (keyword) DO NOTHING
                """,
                (keyword, category),
            )
            return rows > 0
        except Exception as e:  # noqa: BLE001
            log.error(f"插入关键词失败：{keyword}, {e}")
            return False

    def _update_keyword_stats(self, keyword: str, articles_count: int, success: bool):
        """更新关键词统计表（列以 docs/db_schema.sql 为准，无 updated_at）。"""
        try:
            self._db().execute_write(
                """
                UPDATE keywords SET
                    total_collected = total_collected + %s,
                    last_collected_count = %s,
                    avg_daily_count = COALESCE(avg_daily_count, 0)
                        + (%s - COALESCE(avg_daily_count, 0)) / 2,
                    first_collect_time = COALESCE(
                        first_collect_time,
                        CASE WHEN %s THEN NOW() ELSE NULL END),
                    last_collect_time = CASE WHEN %s THEN NOW() ELSE last_collect_time END,
                    status = %s,
                    next_collect_time = NOW()
                        + make_interval(mins => COALESCE(update_cycle_minutes, 20))
                WHERE keyword = %s
                """,
                (
                    articles_count if success else 0,
                    articles_count,
                    articles_count,
                    success,
                    success,
                    'completed' if success else 'failed',
                    keyword,
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"更新关键词统计失败：{keyword}, {e}")

    def _update_channel_state_result(self, keyword: str, channel: str,
                                     articles_count: int, success: bool):
        """回写按渠道调度状态：(词,渠道) 置 completed/failed + 按周期排下次。

        下次周期优先级：kcs.update_cycle_minutes(词×渠道) > 渠道默认 > keywords.update_cycle_minutes > 20。
        """
        ch_default = self.CHANNEL_DEFAULT_CYCLE.get(channel)
        try:
            self._db().execute_write(
                """
                UPDATE keyword_channel_state s SET
                    status = %s,
                    claimer = NULL,
                    last_collect_time = NOW(),
                    last_count = %s,
                    next_collect_time = NOW()
                        + make_interval(mins => COALESCE(
                            s.update_cycle_minutes, %s, k.update_cycle_minutes, 20))
                FROM keywords k
                WHERE s.keyword_id = k.id AND s.channel = %s AND k.keyword = %s
                """,
                (
                    "completed" if success else "failed",
                    int(articles_count or 0),
                    ch_default,
                    channel, keyword,
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"更新渠道调度状态失败（{keyword}/{channel}）：{e}")

    def seed_keyword_channels(self, channels: Optional[List[str]] = None) -> int:
        """为启用关键词×其 channels[] 播种 keyword_channel_state 行（已存在则跳过）。

        channels 传入则只播种指定渠道；缺省按每个词的 keywords.channels[] 展开。
        返回新增行数。启用按渠道调度前调一次即可。
        """
        try:
            if channels:
                rows = self._db().execute_write(
                    """
                    INSERT INTO keyword_channel_state (keyword_id, channel, status, next_collect_time)
                    SELECT k.id, c.ch, 'pending', NOW()
                    FROM keywords k CROSS JOIN unnest(%s::text[]) AS c(ch)
                    WHERE k.enabled = TRUE
                    ON CONFLICT (keyword_id, channel) DO NOTHING
                    """,
                    (channels,),
                )
            else:
                rows = self._db().execute_write(
                    """
                    INSERT INTO keyword_channel_state (keyword_id, channel, status, next_collect_time)
                    SELECT k.id, ch, 'pending', NOW()
                    FROM keywords k, unnest(COALESCE(k.channels, ARRAY['souyisou','sogou'])) AS ch
                    WHERE k.enabled = TRUE
                    ON CONFLICT (keyword_id, channel) DO NOTHING
                    """
                )
            log.info(f"[调度] 播种 keyword_channel_state 新增 {rows} 行")
            return rows
        except Exception as e:  # noqa: BLE001
            log.error(f"播种 keyword_channel_state 失败：{e}")
            return 0

    def set_keyword_channels(self, keyword: str, channels: List[str],
                             cycles: Optional[dict] = None) -> bool:
        """设置一个关键词的分组(渠道)并同步 keyword_channel_state。

        channels: 该词归属的渠道集合（如 ['souyisou']=核心词、['sogou']=拓展词、两者=均属）。
        cycles: 可选，{channel: minutes} 设定该词在某渠道的专属周期（覆盖渠道默认）。
        同步逻辑：写 keywords.channels；为新增渠道建 kcs 行(立即可采)；删除不再属于的渠道行。
        """
        channels = [c for c in (channels or []) if c]
        db = self._db()
        try:
            # 1) 写回 keywords.channels（同时取到 keyword_id）
            rows = db.execute_query(
                "UPDATE keywords SET channels = %s WHERE keyword = %s RETURNING id",
                (channels, keyword),
            )
            if not rows:
                return False
            kid = rows[0][0]
            # 2) 删除不再属于的渠道调度行（停止该渠道采集）
            if channels:
                db.execute_write(
                    "DELETE FROM keyword_channel_state WHERE keyword_id = %s AND channel <> ALL(%s)",
                    (kid, channels),
                )
            else:
                db.execute_write(
                    "DELETE FROM keyword_channel_state WHERE keyword_id = %s", (kid,)
                )
            # 3) 为新增渠道建行（已存在则跳过），立即可采
            for ch in channels:
                cyc = (cycles or {}).get(ch)
                db.execute_write(
                    """
                    INSERT INTO keyword_channel_state
                        (keyword_id, channel, status, next_collect_time, update_cycle_minutes)
                    VALUES (%s, %s, 'pending', NOW(), %s)
                    ON CONFLICT (keyword_id, channel) DO UPDATE SET
                        update_cycle_minutes = COALESCE(EXCLUDED.update_cycle_minutes,
                                                        keyword_channel_state.update_cycle_minutes)
                    """,
                    (kid, ch, cyc),
                )
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"设置关键词分组失败（{keyword}）：{e}")
            return False


# ==================== 使用示例 ====================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    scheduler = DistributedTaskScheduler.from_env()

    print("=== 注册关键词 ===")
    test_kws = ["评选征集", "人工智能", "工业自动化", "传感器采购", "政府招标"]
    count = scheduler.register_keywords(test_kws, category="test")
    print(f"成功注册：{count} 个")

    print("\n=== VM-001 领取 PC 渠道任务 ===")
    claimed = scheduler.claim_task(channel="wechat_pc", vm_instance_id="vm-001",
                                   max_keywords=3)
    print(f"领取到：{claimed}")

    print("\n=== 上报采集结果 ===")
    for kw in claimed:
        scheduler.report_result(kw, articles_count=10, success=True)

    print("\n=== 调度器统计 ===")
    print(json.dumps(scheduler.get_statistics(), indent=2, ensure_ascii=False))
