"""
分布式任务调度器
基于 Redis 实现关键词任务池 + 领取消耗锁 + 20 分钟更新周期控制。
支持多台 VM/实例并发领取，自动负载均衡。

无人值守地基（容器侧 celery-beat 周期驱动，见 wxsearch/tasks.py）：
  - requeue_due_keywords() : 到期的 completed 词翻回 pending，维持 20 分钟周期自转（补断链）；
  - recover_stale_claims() : 回收崩溃/掉线 VM 遗留的 running 词，绝不漏采；
  - health_snapshot()      : PG/Redis/池状态快照，供心跳与后续告警使用。

约定：
  - 普通 DB 访问走 DatabaseConnector.execute_query/execute_write；租约生命周期内的
    多语句状态提交使用其 cursor()/commit()/rollback()/close() 事务接口，并设置
    短于 Redis 锁 TTL 的本地 PG 超时；
  - Redis 连接优先用 REDIS_URL（线上带 requirepass），见 from_env()；
  - 权威表结构以 docs/db_schema.sql 为准（keywords 无 updated_at 列）。
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from urllib.parse import quote, unquote

import redis

log = logging.getLogger(__name__)


class DistributedTaskScheduler:
    """Redis 驱动的任务调度器"""

    # ==================== 命名空间配置 ====================

    KEYWORD_POOL = "wxsearch:keyword_pool"      # Set<keyword> (待采词库)
    CLAIMED_SET = "wxsearch:claimed_current"    # Set<legacy keyword | v2 channel+keyword>
    LOCK_PREFIX = "wxsearch:task_lock:"         # Channel 锁前缀
    RESULT_PREFIX = "wxsearch:task_result:"     # 任务结果前缀
    KW_PREFIX = "wxsearch:kw:"                  # 单词运行态 hash 前缀
    HEARTBEAT_KEY = "wxsearch:heartbeat:worker" # worker 健康心跳
    LEASE_LOCK = "wxsearch:task_lock:leases"    # 心跳续租 / stale 回收串行锁
    LEASE_MEMBER_PREFIX = "v2|"
    CLAIM_PROTOCOL_FLOORS_ENV = "CLAIM_PROTOCOL_FLOORS"
    PG_LOCK_TIMEOUT_MS = 2_000
    PG_STATEMENT_TIMEOUT_MS = 5_000
    STALE_RECOVERY_BATCH_LIMIT = 20
    STALE_RECOVERY_BUDGET_SEC = 40
    STALE_RECOVERY_OPERATION_RESERVE_SEC = 20
    STALE_REDIS_BATCH_LIMIT = 10
    STALE_ORPHAN_PROCESS_LIMIT = 10
    STALE_ORPHAN_SCAN_LIMIT = 100
    ORPHAN_CURSOR_KEY = "wxsearch:recovery:orphan_cursor"
    STALE_CURSOR_KEY = "wxsearch:recovery:stale_cursor"
    RECOVERY_PHASE_KEY = "wxsearch:recovery:first_phase"

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
                socket_timeout=5,
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
            url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5
        )
        return cls(redis_client=client)

    # ==================== DB 助手 ====================

    @staticmethod
    def _db():
        """获取数据库连接器单例（惰性导入，避免非 DB 场景引入依赖）。"""
        from .db_connector import DatabaseConnector
        return DatabaseConnector()

    # ==================== 租约身份（兼容 legacy） ====================

    @classmethod
    def _lease_member(cls, channel: str, keyword: str) -> str:
        """v2 租约集合成员：渠道和关键词均编码，避免跨渠道互相覆盖。"""
        return f"{cls.LEASE_MEMBER_PREFIX}{quote(str(channel), safe='')}|{quote(str(keyword), safe='')}"

    @classmethod
    def _lease_key(cls, channel: str, keyword: str) -> str:
        return f"{cls.KW_PREFIX}{cls._lease_member(channel, keyword)}"

    @classmethod
    def _decode_lease_member(cls, member: str):
        text = str(member)
        if text.startswith(cls.LEASE_MEMBER_PREFIX):
            encoded = text[len(cls.LEASE_MEMBER_PREFIX):]
            parts = encoded.split("|", 1)
            if len(parts) == 2:
                return unquote(parts[0]), unquote(parts[1]), True
        return None, text, False

    @classmethod
    def _configured_claim_protocol_floors(cls) -> Dict[str, int]:
        """解析不可变发布配置中的单设备领词协议下限。

        格式：``vm-a:2,vm-b:3``。v3 是部署排空态（当前 v1/v2
        请求均被拒绝）。任何配置错误均由调用方 fail closed。
        """
        raw = os.getenv(cls.CLAIM_PROTOCOL_FLOORS_ENV, "").strip()
        if not raw:
            return {}
        floors: Dict[str, int] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item or ":" not in item:
                raise ValueError(f"无效的领词协议门禁项：{item!r}")
            vm_instance_id, value = item.rsplit(":", 1)
            vm_instance_id = vm_instance_id.strip()
            if not vm_instance_id or vm_instance_id in floors:
                raise ValueError(f"重复或空的门禁设备 ID：{vm_instance_id!r}")
            minimum = int(value.strip())
            if minimum not in (1, 2, 3):
                raise ValueError(f"不支持的领词协议下限：{minimum}")
            floors[vm_instance_id] = minimum
        return floors

    @classmethod
    def _claim_protocol_allowed(
        cls, vm_instance_id: str, requested_protocol: int
    ) -> bool:
        try:
            minimum = cls._configured_claim_protocol_floors().get(
                vm_instance_id, 1
            )
        except (TypeError, ValueError) as exc:
            log.error("领词协议门禁配置非法，已全部拒绝：%s", exc)
            return False
        if requested_protocol < minimum:
            log.info(
                "设备 %s 的协议 v%s 领取被最低 v%s 门禁拒绝",
                vm_instance_id,
                requested_protocol,
                minimum,
            )
            return False
        return True

    def _lease_record(self, member: str):
        channel, keyword, is_v2 = self._decode_lease_member(member)
        key = self._lease_key(channel, keyword) if is_v2 else f"{self.KW_PREFIX}{keyword}"
        data = self.redis.hgetall(key)
        if not channel:
            channel = data.get("channel") or ""
        return {
            "member": member,
            "key": key,
            "channel": channel,
            "keyword": keyword,
            "is_v2": is_v2,
            "data": data,
        }

    def _find_lease(self, channel: str, keyword: str):
        """优先查 v2 渠道租约，再兼容升级前的 legacy 关键词租约。"""
        member = self._lease_member(channel, keyword)
        if self.redis.sismember(self.CLAIMED_SET, member):
            return self._lease_record(member)
        if self.redis.sismember(self.CLAIMED_SET, keyword):
            record = self._lease_record(keyword)
            if not record["channel"] or record["channel"] == channel:
                return record
        return None

    @classmethod
    def _set_pg_local_timeouts(cls, cur) -> None:
        """把 PG 行锁/语句等待限制在 Redis lease lock TTL 以内。"""
        cur.execute(f"SET LOCAL lock_timeout = '{cls.PG_LOCK_TIMEOUT_MS}ms'")
        cur.execute(
            f"SET LOCAL statement_timeout = '{cls.PG_STATEMENT_TIMEOUT_MS}ms'"
        )

    def _execute_write_with_timeouts(self, db, sql_text: str, params=None) -> int:
        """在 lease lock 内执行有界 PG 写；轻量 fake DB 保持测试兼容。"""
        if not all(hasattr(db, name) for name in ("cursor", "commit", "rollback", "close")):
            return db.execute_write(sql_text, params)
        cur = None
        try:
            cur = db.cursor()
            self._set_pg_local_timeouts(cur)
            cur.execute(sql_text, params)
            affected = cur.rowcount
            db.commit()
            return affected
        except Exception:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            db.close()

    def _execute_query_with_timeouts(self, db, sql_text: str, params=None) -> list:
        """在 lease lock 内执行有界 PG 读，避免 DDL 锁拖过 Redis TTL。"""
        if not all(hasattr(db, name) for name in ("cursor", "rollback", "close")):
            return db.execute_query(sql_text, params)
        cur = None
        try:
            cur = db.cursor()
            self._set_pg_local_timeouts(cur)
            cur.execute(sql_text, params)
            return cur.fetchall()
        finally:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            db.close()

    def _compensate_claim_pg(self, db, has_channel_state: bool, channel: str,
                             vm_instance_id: str, keywords: List[str]) -> bool:
        """领取 Redis 建态失败时把 PG running 恢复；失败则由 marker 自愈。"""
        try:
            if has_channel_state:
                affected = self._execute_write_with_timeouts(
                    db,
                    """
                    UPDATE keyword_channel_state s SET status='pending', claimer=NULL,
                        last_claimed=NULL, next_collect_time=NOW()
                    FROM keywords k
                    WHERE s.keyword_id=k.id AND s.channel=%s AND s.claimer=%s
                      AND s.status='running' AND k.keyword=ANY(%s)
                    """,
                    (channel, vm_instance_id, keywords),
                )
            else:
                affected = self._execute_write_with_timeouts(
                    db,
                    "UPDATE keywords SET status='pending', next_collect_time=NOW() "
                    "WHERE keyword=ANY(%s) AND status='running'",
                    (keywords,),
                )
            return affected == len(keywords)
        except Exception as exc:  # noqa: BLE001
            log.error(f"领取 PG 补偿失败，将保留恢复 marker：{exc}")
            return False

    def _ensure_recovery_markers(self, records: List[dict]) -> None:
        if hasattr(self.redis, "pipeline"):
            try:
                pipe = self.redis.pipeline(transaction=False)
                for record in records:
                    mapping = dict(record["mapping"])
                    mapping["recovery_only"] = "1"
                    pipe.hset(record["key"], mapping=mapping)
                    pipe.sadd(self.CLAIMED_SET, record["member"])
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                log.error(f"恢复 marker 批量建立失败：{exc}")
                return
        for record in records:
            try:
                mapping = dict(record["mapping"])
                # 客户端从未收到此领取；即使是 legacy 设备在线，也不得保护它。
                mapping["recovery_only"] = "1"
                self.redis.hset(record["key"], mapping=mapping)
                self.redis.sadd(self.CLAIMED_SET, record["member"])
            except Exception as exc:  # noqa: BLE001
                # Redis 整体不可用时无法现场修复；后续 PG-running 对账仍会回收。
                log.error(
                    f"恢复 marker 建立失败（{record['keyword']}）：{exc}"
                )

    def _cleanup_recovery_markers(self, records: List[dict]) -> None:
        if hasattr(self.redis, "pipeline"):
            try:
                pipe = self.redis.pipeline(transaction=False)
                for record in records:
                    pipe.srem(self.CLAIMED_SET, record["member"])
                    pipe.delete(record["key"])
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                log.error(f"补偿后 Redis marker 批量清理失败：{exc}")
                return
        for record in records:
            try:
                self.redis.srem(self.CLAIMED_SET, record["member"])
                self.redis.delete(record["key"])
            except Exception as exc:  # noqa: BLE001
                # PG 已 pending；残留 marker 会由 stale recovery 幂等清除。
                log.error(
                    f"补偿后 Redis marker 清理失败（{record['keyword']}）：{exc}"
                )

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
                   max_keywords: int = 10, lease_aware: bool = False) -> List:
        """领取一批关键词。

        lease_aware=False 兼容旧节点并返回字符串列表；新节点传 True 时返回
        ``[{keyword, lease_id}]``，后续续租和结果上报必须携带同一 lease_id。
        """
        log.debug(f"[{channel}] {vm_instance_id} 开始领取，最大数量={max_keywords}")

        lock_key = f"{self.LOCK_PREFIX}{channel}"
        claimed_keywords: List[str] = []
        response: List = []

        # 先按渠道串行，再进入全局租约锁；所有生命周期路径均遵循此顺序。
        with self.redis.lock(lock_key, timeout=300, blocking_timeout=10):
            requested_protocol = 2 if lease_aware else 1
            if not self._claim_protocol_allowed(vm_instance_id, requested_protocol):
                return []
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                # 必须在锁内重查；锁外查询会让等待锁的第二个调用使用过期列表。
                claim_limit = min(50, max(1, int(max_keywords)))
                has_channel_state = self._channel_has_state(channel)
                if not has_channel_state:
                    log.error(
                        f"[{channel}] 拒绝领取：迁移 008 未部署或该渠道尚未播种 "
                        "keyword_channel_state"
                    )
                    return []
                available_kws = self._get_available_keywords(
                    channel, limit=claim_limit * 2
                )
                # legacy 只有 keyword 身份，不能与任何渠道的同名租约并存；v2 也
                # 必须避开正在运行的 legacy 同名租约，防止旧 hash 被覆盖。
                claimed_members = list(self.redis.smembers(self.CLAIMED_SET))
                legacy_busy = {
                    self._decode_lease_member(member)[1]
                    for member in claimed_members
                    if not self._decode_lease_member(member)[2]
                }
                if lease_aware:
                    claimed_keywords = [
                        kw for kw in available_kws if kw not in legacy_busy
                    ][:claim_limit]
                else:
                    all_busy = {
                        self._decode_lease_member(member)[1]
                        for member in claimed_members
                    }
                    claimed_keywords = [
                        kw for kw in available_kws if kw not in all_busy
                    ][:claim_limit]
                if not claimed_keywords:
                    log.info(f"[{channel}] 无可用关键词")
                    return []

                db = self._db()
                now_iso = datetime.now().isoformat()
                intended_records = []
                for kw in claimed_keywords:
                    lease_id = str(uuid.uuid4())
                    if lease_aware:
                        member = self._lease_member(channel, kw)
                        key = self._lease_key(channel, kw)
                    else:
                        # 旧 VM 继续写旧 member/hash，服务端回滚时仍能识别和释放。
                        member = kw
                        key = f"{self.KW_PREFIX}{kw}"
                    intended_records.append({
                        "member": member,
                        "key": key,
                        "keyword": kw,
                        "lease_id": lease_id,
                        "mapping": {
                            "status": "running",
                            "last_claimed": now_iso,
                            "claimer": vm_instance_id,
                            "channel": channel,
                            "keyword": kw,
                            "lease_id": lease_id,
                            "lease_protocol": "2" if lease_aware else "1",
                            "lease_required": "1" if lease_aware else "0",
                        },
                    })
                if has_channel_state:
                    affected = self._execute_write_with_timeouts(
                        db,
                        """
                        UPDATE keyword_channel_state s SET
                            status = 'running', claimer = %s, last_claimed = NOW()
                        FROM keywords k
                        WHERE s.keyword_id = k.id AND s.channel = %s
                          AND s.status IN ('pending', 'failed')
                          AND k.keyword = ANY(%s)
                        """,
                        (vm_instance_id, channel, claimed_keywords),
                    )
                else:
                    affected = self._execute_write_with_timeouts(
                        db,
                        """
                        UPDATE keywords SET status = 'running'
                        WHERE keyword = ANY(%s) AND status IN ('pending', 'failed')
                        """,
                        (claimed_keywords,),
                    )
                if affected != len(claimed_keywords):
                    compensated = self._compensate_claim_pg(
                        db, has_channel_state, channel, vm_instance_id,
                        claimed_keywords,
                    )
                    if not compensated:
                        self._ensure_recovery_markers(intended_records)
                    raise RuntimeError(
                        f"领取状态写入不完整：期望 {len(claimed_keywords)}，实际 {affected}"
                    )

                try:
                    if hasattr(self.redis, "pipeline"):
                        pipe = self.redis.pipeline(transaction=False)
                        for record in intended_records:
                            pipe.hset(record["key"], mapping=record["mapping"])
                            pipe.sadd(self.CLAIMED_SET, record["member"])
                        pipe.execute()
                    else:
                        for record in intended_records:
                            # intended record 已在 Redis I/O 前登记；即使 SADD 服务端成功
                            # 但客户端收到异常，补偿路径也知道完整 marker 身份。
                            self.redis.hset(record["key"], mapping=record["mapping"])
                            self.redis.sadd(self.CLAIMED_SET, record["member"])
                    for record in intended_records:
                        response.append(
                            {
                                "keyword": record["keyword"],
                                "lease_id": record["lease_id"],
                            }
                            if lease_aware else record["keyword"]
                        )
                except Exception:
                    # 先补偿 PG，再清 Redis。PG 补偿失败时反而保留/重建 marker，
                    # 让 stale recovery 能发现 running 状态，避免永久卡死。
                    compensated = self._compensate_claim_pg(
                        db, has_channel_state, channel, vm_instance_id,
                        claimed_keywords,
                    )
                    if compensated:
                        self._cleanup_recovery_markers(intended_records)
                    else:
                        self._ensure_recovery_markers(intended_records)
                    raise

        log.info(f"[{channel}] {vm_instance_id} 成功领取 {len(response)} 个关键词")
        return response

    def report_result(self, keyword: str, articles_count: int, success: bool,
                      error_message: str = None, device_id: str = None,
                      channel: str = None, lease_id: str = None) -> bool:
        """带 owner/channel/lease fencing 的结果提交。

        PG 调度状态在单事务内提交成功后才释放 Redis 租约。v2 租约必须匹配
        lease_id；legacy 协议仍校验 owner/channel，供滚动升级期间使用。
        """
        if not keyword or not device_id or not channel:
            log.error(f"[结果上报拒绝] 缺少 keyword/device_id/channel：{keyword}")
            return False

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

        committed = False
        effective_lease_id = None
        try:
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                record = self._find_lease(channel, keyword)
                if not record:
                    log.warning(f"[结果上报拒绝] 找不到活跃租约：{channel}/{keyword}")
                    return False
                data = record["data"]
                effective_lease_id = data.get("lease_id")
                if (
                    data.get("status") != "running"
                    or data.get("claimer") != device_id
                    or data.get("channel") != channel
                ):
                    log.warning(
                        f"[结果上报拒绝] owner/channel 已变化：{channel}/{keyword} "
                        f"owner={data.get('claimer')} reporter={device_id}"
                    )
                    return False
                if data.get("lease_required") == "1" and (
                    not lease_id or lease_id != effective_lease_id
                ):
                    log.warning(f"[结果上报拒绝] lease_id 过期：{channel}/{keyword}")
                    return False
                if lease_id and effective_lease_id and lease_id != effective_lease_id:
                    log.warning(f"[结果上报拒绝] lease_id 不匹配：{channel}/{keyword}")
                    return False

                persist_state = self._persist_result_state(
                    keyword=keyword,
                    channel=channel,
                    device_id=device_id,
                    articles_count=articles_count,
                    success=success,
                )
                if persist_state not in {"committed", "already_committed"}:
                    return False
                committed = persist_state == "committed"

                # required PG 状态已经提交，才释放 Redis owner。
                if hasattr(self.redis, "pipeline"):
                    pipe = self.redis.pipeline(transaction=False)
                    pipe.hset(record["key"], mapping=kw_data)
                    pipe.srem(self.CLAIMED_SET, record["member"])
                    pipe.execute()
                else:
                    self.redis.hset(record["key"], mapping=kw_data)
                    self.redis.srem(self.CLAIMED_SET, record["member"])
        except Exception as e:  # noqa: BLE001
            log.error(f"[结果上报] 租约提交失败（{channel}/{keyword}）：{e}")
            return False

        result_identity = effective_lease_id or timestamp
        result_key = f"{self.RESULT_PREFIX}{channel}:{keyword}:{result_identity}"
        try:
            self.redis.setex(result_key, 86400 * 30, json.dumps({
                "keyword": keyword,
                "channel": channel,
                "lease_id": effective_lease_id,
                "articles_count": articles_count,
                "success": success,
                "completed_at": timestamp,
                "error_message": error_message,
            }, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.error(f"保存结果历史失败（不影响已提交状态）：{e}")

        # 设备级历史：每次上报写一行 collect_tasks（有 device_id 才写，向后兼容）。
        if committed and device_id:
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
            elif committed:
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
        """回收崩溃/掉线 VM 遗留的 running 词。

        v2 以 last_claimed 为唯一活跃依据：客户端停止声明后即使设备仍在线也会回收。
        legacy 协议没有活跃集合，滚动升级期间继续用新鲜设备心跳保护长任务。
        """
        threshold = datetime.now() - timedelta(minutes=stale_after_minutes)
        candidates = []
        persisted = []
        orphaned = []

        try:
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                deadline = time.monotonic() + self.STALE_RECOVERY_BUDGET_SEC
                db = self._db()
                rows = self._execute_query_with_timeouts(
                    db,
                    """
                    SELECT device_id, current_keyword FROM devices
                    WHERE last_heartbeat IS NOT NULL
                      AND last_heartbeat >= NOW() - make_interval(mins => %s)
                    """,
                    (int(stale_after_minutes),),
                )
                recent_devices = {r[0] for r in (rows or [])}
                recent_active = {
                    (r[0], r[1]) for r in (rows or [])
                    if len(r) > 1 and r[1]
                }

                # 两类恢复轮流优先；即使优先阶段遇到最坏超时，另一类也只延后一轮，
                # 不会被固定头部毒记录永久饿死。
                orphan_first = self._next_recovery_first_phase() == "orphan"
                if orphan_first:
                    orphaned = self._recover_orphaned_running_locked(
                        stale_after_minutes,
                        recent_active=recent_active,
                        limit=self.STALE_ORPHAN_PROCESS_LIMIT,
                        deadline=deadline,
                    )

                for member in self._iter_claimed_members(deadline):
                    if not self._has_recovery_budget(deadline):
                        break
                    record = self._lease_record(member)
                    data = record["data"]
                    last_claimed = data.get("last_claimed")
                    stale = True
                    if last_claimed:
                        try:
                            stale = datetime.fromisoformat(last_claimed) < threshold
                        except ValueError:
                            stale = True  # 时间无法解析，保守判定为崩溃遗留
                    is_protocol_v2 = data.get("lease_protocol") == "2"
                    if not stale:
                        continue
                    retry_after = data.get("recovery_retry_after")
                    if retry_after:
                        try:
                            if datetime.fromisoformat(retry_after) > datetime.now():
                                continue
                        except ValueError:
                            pass
                    recovery_only = data.get("recovery_only") == "1"
                    if (
                        not is_protocol_v2
                        and not recovery_only
                        and data.get("claimer") in recent_devices
                    ):
                        continue
                    candidates.append(record)
                    if len(candidates) >= self.STALE_REDIS_BATCH_LIMIT:
                        break

                # 每个租约独立 PG 事务；单条历史坏数据只隔离自身，不拖住正常回收。
                for record in candidates:
                    if not self._has_recovery_budget(deadline):
                        log.warning("[自愈] 达到单轮时间预算，剩余租约留待下轮")
                        break
                    if not self._persist_recovered_claims([record]):
                        self._defer_poisoned_lease(record)
                        log.error(
                            "[自愈] 隔离无法回收的坏租约：%s/%s owner=%s",
                            record.get("channel"), record.get("keyword"),
                            record.get("data", {}).get("claimer"),
                        )
                        continue
                    persisted.append(record)
                    terminal_status = record.get("_pg_terminal_status")
                    try:
                        self._finalize_recovered_marker(record, terminal_status)
                    except Exception as exc:  # noqa: BLE001
                        # PG 已提交；保留 marker 供下轮幂等清理，不能回滚正常项。
                        log.error(
                            f"[自愈] Redis marker 清理失败，将重试 "
                            f"({record['channel']}/{record['keyword']})：{exc}"
                        )

                if not orphan_first:
                    orphaned = self._recover_orphaned_running_locked(
                        stale_after_minutes,
                        recent_active=recent_active,
                        limit=self.STALE_ORPHAN_PROCESS_LIMIT,
                        deadline=deadline,
                    )

        except Exception as e:  # noqa: BLE001
            # 无法同时确认设备心跳和 Redis owner 时，安全侧失败：本轮不释放。
            log.error(f"[自愈] 校验设备心跳或租约失败，本轮不回收：{e}")
            return []

        recovered = [
            r["keyword"] for r in persisted if not r.get("_pg_terminal_status")
        ] + orphaned
        cleaned = [r["keyword"] for r in persisted if r.get("_pg_terminal_status")]
        if recovered:
            log.warning(f"[自愈] 回收崩溃遗留关键词 {len(recovered)} 个：{recovered}")
        if cleaned:
            log.info(f"[自愈] 清理已提交结果的残留 Redis 租约：{cleaned}")
        return recovered

    def _defer_poisoned_lease(self, record: dict) -> None:
        """坏 marker 短退避，避免小 Set 的 SSCAN cursor=0 时固定占满配额。"""
        try:
            self.redis.hset(
                record["key"],
                mapping={
                    "recovery_retry_after": (
                        datetime.now() + timedelta(minutes=5)
                    ).isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                f"[自愈] 坏租约退避标记失败 "
                f"({record.get('channel')}/{record.get('keyword')})：{exc}"
            )

    def _has_recovery_budget(self, deadline: float) -> bool:
        return time.monotonic() < (
            deadline - self.STALE_RECOVERY_OPERATION_RESERVE_SEC
        )

    def _next_recovery_first_phase(self) -> str:
        """原子轮换 Redis-stale / PG-orphan 的优先顺序。"""
        if hasattr(self.redis, "eval"):
            value = self.redis.eval(
                """
                local old = redis.call('GET', KEYS[1]) or 'orphan'
                if old == 'orphan' then
                    redis.call('SET', KEYS[1], 'redis')
                else
                    redis.call('SET', KEYS[1], 'orphan')
                end
                return old
                """,
                1,
                self.RECOVERY_PHASE_KEY,
            )
            return str(value or "orphan")
        current = str(self.redis.get(self.RECOVERY_PHASE_KEY) or "orphan")
        self.redis.set(
            self.RECOVERY_PHASE_KEY,
            "redis" if current == "orphan" else "orphan",
        )
        return current

    def _iter_claimed_members(self, deadline: float):
        """用 SSCAN 分页读 claimed set，并在硬预算前停止。"""
        if hasattr(self.redis, "sscan"):
            try:
                cursor = int(self.redis.get(self.STALE_CURSOR_KEY) or 0)
            except Exception:  # noqa: BLE001
                cursor = 0
            while self._has_recovery_budget(deadline):
                cursor, members = self.redis.sscan(
                    self.CLAIMED_SET, cursor=cursor, count=10
                )
                try:
                    self.redis.set(self.STALE_CURSOR_KEY, int(cursor))
                except Exception as exc:  # noqa: BLE001
                    log.error(f"[自愈] stale 游标保存失败：{exc}")
                for member in members:
                    yield member
                if int(cursor) == 0:
                    break
            return
        for member in list(self.redis.smembers(self.CLAIMED_SET)):
            if not self._has_recovery_budget(deadline):
                break
            yield member

    def _finalize_recovered_marker(self, record: dict, terminal_status=None) -> None:
        mapping = {
            "status": terminal_status or "pending",
            "next_collect_time": datetime.now().isoformat(),
        }
        if hasattr(self.redis, "pipeline"):
            pipe = self.redis.pipeline(transaction=False)
            pipe.hset(record["key"], mapping=mapping)
            pipe.srem(self.CLAIMED_SET, record["member"])
            pipe.execute()
        else:
            self.redis.hset(record["key"], mapping=mapping)
            self.redis.srem(self.CLAIMED_SET, record["member"])

    def _recover_orphaned_running_locked(
        self, stale_after_minutes: int, recent_active=None,
        limit: int = None, deadline: float = None,
    ) -> List[str]:
        """回收 PG 有 running、Redis 却无租约 marker 的领取半失败记录。"""
        limit = max(0, min(
            int(limit if limit is not None else self.STALE_RECOVERY_BATCH_LIMIT),
            self.STALE_RECOVERY_BATCH_LIMIT,
        ))
        if limit == 0 or (
            deadline is not None and not self._has_recovery_budget(deadline)
        ):
            return []
        try:
            cursor_id = int(self.redis.get(self.ORPHAN_CURSOR_KEY) or 0)
        except Exception:  # noqa: BLE001
            cursor_id = 0
        try:
            db = self._db()
            rows = self._execute_query_with_timeouts(
                db,
                """
                SELECT s.id, k.keyword, s.channel, s.claimer
                FROM keyword_channel_state s
                JOIN keywords k ON k.id=s.keyword_id
                WHERE s.status='running' AND s.claimer IS NOT NULL
                  AND (s.last_claimed IS NULL
                       OR s.last_claimed < NOW() - make_interval(mins => %s))
                ORDER BY (s.id <= %s), s.id
                LIMIT %s
                """,
                (int(stale_after_minutes), cursor_id,
                 self.STALE_ORPHAN_SCAN_LIMIT),
            )
        except Exception as exc:  # noqa: BLE001
            # 迁移 008 是领取硬前置；异常只阻断本轮，不触碰 Redis owner。
            log.error(f"[自愈] PG/Redis orphan 对账失败：{exc}")
            return []

        recovered = []
        recent_active = set(recent_active or set())
        last_examined_id = cursor_id
        for row in (rows or []):
            if not isinstance(row, (list, tuple)) or len(row) not in {3, 4}:
                log.error(f"[自愈] 忽略非法 orphan 对账行：{row!r}")
                continue
            if len(row) == 4:
                row_id, keyword, channel, owner = row
                last_examined_id = int(row_id)
            else:  # 仅供轻量 fake DB 单测
                keyword, channel, owner = row
            if deadline is not None and not self._has_recovery_budget(deadline):
                break
            if self._find_lease(channel, keyword):
                continue
            # Redis 全丢时无法区分 v1/v2；仍在明确采这个词的在线设备优先完成，
            # current_keyword 清空或设备离线后再回收。
            if (owner, keyword) in recent_active:
                continue
            record = {
                "member": self._lease_member(channel, keyword),
                "key": self._lease_key(channel, keyword),
                "channel": channel,
                "keyword": keyword,
                "is_v2": True,
                "data": {"claimer": owner, "channel": channel},
            }
            if self._persist_recovered_claims([record]):
                recovered.append(keyword)
                log.warning(
                    f"[自愈] 已回收 PG-running/Redis-missing："
                    f"{channel}/{keyword}/{owner}"
                )
                if len(recovered) >= limit:
                    break
        try:
            self.redis.set(self.ORPHAN_CURSOR_KEY, last_examined_id)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[自愈] orphan 游标保存失败：{exc}")
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
        return [
            self._decode_lease_member(member)[1]
            for member in self.redis.smembers(self.CLAIMED_SET)
        ]

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
        """有界强制释放某 VM 的一批任务；超过 10 条时重复调用。"""
        records = []
        persisted = []
        try:
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                deadline = time.monotonic() + self.STALE_RECOVERY_BUDGET_SEC
                for member in self._iter_claimed_members(deadline):
                    if not self._has_recovery_budget(deadline):
                        break
                    record = self._lease_record(member)
                    if record["data"].get("claimer") == vm_instance_id:
                        records.append(record)
                        if len(records) >= self.STALE_REDIS_BATCH_LIMIT:
                            break
                for record in records:
                    if not self._has_recovery_budget(deadline):
                        break
                    if not self._persist_recovered_claims([record]):
                        continue
                    persisted.append(record)
                    self._finalize_recovered_marker(record)
        except Exception as e:  # noqa: BLE001
            log.error(f"force_release_all 失败（{vm_instance_id}）：{e}")
            return []
        released = [r["keyword"] for r in persisted]
        log.warning(
            f"VM {vm_instance_id} 有界释放任务：{len(released)} 个"
            "（如仍有占用可重复调用）"
        )
        return released

    # ==================== 设备注册 / 心跳 / 采集历史 ====================

    def device_drain_status(self, device_id: str, channel: str) -> Dict:
        """返回单设备是否已在关键词边界排空，不暴露关键词内容。

        Redis owner 与 ``devices.current_keyword`` 均为空才视为安全。
        owner 按设备跨渠道统计，并校验设备登记渠道，避免只在
        单个 channel 上建立错误边界。缺少心跳行也不能证明安全。
        """
        if not device_id or not channel:
            return {
                "drained": False,
                "owned_claims": 0,
                "current_keyword_active": True,
                "channel_match": False,
                "protocol_floor": -1,
            }
        try:
            protocol_floor = self._configured_claim_protocol_floors().get(
                device_id, 1
            )
        except (TypeError, ValueError):
            protocol_floor = -1
        with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
            owned = 0
            for member in self.redis.smembers(self.CLAIMED_SET):
                record = self._lease_record(member)
                if record["data"].get("claimer") == device_id:
                    owned += 1
            rows = self._execute_query_with_timeouts(
                self._db(),
                """
                SELECT current_keyword, channel
                FROM devices
                WHERE device_id = %s
                """,
                (device_id,),
            )
            row_exists = bool(rows)
            current_active = not row_exists or bool(rows[0][0])
            channel_match = row_exists and rows[0][1] == channel
            return {
                "drained": (
                    protocol_floor >= 3
                    and owned == 0
                    and not current_active
                    and channel_match
                ),
                "owned_claims": owned,
                "current_keyword_active": current_active,
                "channel_match": channel_match,
                "protocol_floor": protocol_floor,
            }

    def heartbeat_device(self, device_id: str, device_type: str = "pc",
                         channel: str = "souyisou", current_keyword: str = None,
                         active_keywords: List = None) -> bool:
        """设备心跳并续租该设备明确声明仍活跃的关键词。

        active_keywords 是向后兼容的可选参数：旧 VM 不传时只刷新设备心跳，不会
        盲目续租历史遗留任务；新 VM 传入当前批次尚未完成的词集合。
        """
        if not device_id:
            return False
        try:
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                db = self._db()
                self._execute_write_with_timeouts(
                    db,
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
                self._renew_claims_locked(device_id, channel, active_keywords)
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"设备心跳写入失败 ({device_id})：{e}")
            return False

    def renew_claims(self, device_id: str, channel: str,
                     active_keywords: List) -> List[str]:
        """续租该设备/渠道仍活跃的关键词；主要供测试和受控运维调用。"""
        try:
            with self.redis.lock(self.LEASE_LOCK, timeout=60, blocking_timeout=10):
                return self._renew_claims_locked(device_id, channel, active_keywords)
        except Exception as e:  # noqa: BLE001
            log.error(f"关键词续租失败 ({device_id}/{channel})：{e}")
            return []

    def _renew_claims_locked(self, device_id: str, channel: str,
                             active_keywords: List) -> List[str]:
        raw_items = list(active_keywords or [])
        if len(raw_items) > 50:
            raise ValueError("active_keywords 超过 50 条上限")
        requested = []
        seen = set()
        for item in raw_items:
            if isinstance(item, dict):
                keyword = str(item.get("keyword") or "").strip()
                lease_id = str(item.get("lease_id") or "").strip()
                strict_v2 = True
            else:
                keyword = str(item or "").strip()
                lease_id = ""
                strict_v2 = False
            if not keyword or len(keyword) > 500:
                raise ValueError("active_keywords 含空值或超长关键词")
            identity = (keyword, lease_id)
            if identity not in seen:
                seen.add(identity)
                requested.append((keyword, lease_id, strict_v2))
        if not requested:
            return []

        renewed: List[str] = []
        now_iso = datetime.now().isoformat()
        claimed_members = set(self.redis.smembers(self.CLAIMED_SET))
        records = []
        for keyword, lease_id, strict_v2 in requested:
            v2_member = self._lease_member(channel, keyword)
            if v2_member in claimed_members:
                member = v2_member
                key = self._lease_key(channel, keyword)
            elif keyword in claimed_members:
                member = keyword
                key = f"{self.KW_PREFIX}{keyword}"
            else:
                if strict_v2:
                    raise RuntimeError(f"活跃租约已不存在：{channel}/{keyword}")
                records.append((keyword, lease_id, strict_v2, None, None))
                continue
            records.append((keyword, lease_id, strict_v2, member, key))

        active_records = [record for record in records if record[4] is not None]
        if hasattr(self.redis, "pipeline") and active_records:
            pipe = self.redis.pipeline(transaction=False)
            for record in active_records:
                pipe.hgetall(record[4])
            active_data = iter(pipe.execute())
        else:
            active_data = iter([
                self.redis.hgetall(record[4]) for record in active_records
            ])

        renew_keys = []
        for keyword, lease_id, strict_v2, member, key in records:
            if key is None:
                continue
            data = next(active_data)
            if (
                data.get("status") == "running"
                and data.get("claimer") == device_id
                and data.get("channel") == channel
            ):
                if data.get("lease_protocol") == "2" and lease_id != data.get("lease_id"):
                    raise RuntimeError(f"活跃 lease_id 已过期：{channel}/{keyword}")
                renew_keys.append(key)
                renewed.append(keyword)
            elif strict_v2:
                raise RuntimeError(f"活跃租约 owner/channel 已变化：{channel}/{keyword}")

        if renew_keys:
            if hasattr(self.redis, "pipeline"):
                pipe = self.redis.pipeline(transaction=False)
                for key in renew_keys:
                    pipe.hset(key, mapping={"last_claimed": now_iso})
                pipe.execute()
            else:
                for key in renew_keys:
                    self.redis.hset(key, mapping={"last_claimed": now_iso})

        if renewed and self._channel_has_state(channel):
            db = self._db()
            self._execute_write_with_timeouts(
                db,
                """
                UPDATE keyword_channel_state s SET last_claimed = NOW()
                FROM keywords k
                WHERE s.keyword_id = k.id
                  AND s.channel = %s
                  AND s.claimer = %s
                  AND s.status = 'running'
                  AND k.keyword = ANY(%s)
                """,
                (channel, device_id, renewed),
            )
        return renewed

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

    def mark_offline_devices(self, timeout_seconds: int = 600) -> List[str]:
        """将心跳超时的设备置 offline，返回刚转为离线的设备 id 列表（供告警）。

        先取候选，再逐个使用同一时间条件 UPDATE；若心跳已在两步之间恢复，rowcount=0，
        不会被旧扫描结果覆盖为 offline。
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
            newly_offline = []
            for (device_id,) in (rows or []):
                changed = self._db().execute_write(
                    """
                    UPDATE devices SET status = 'offline', current_keyword = NULL
                    WHERE device_id = %s AND status = 'online'
                      AND (last_heartbeat IS NULL
                           OR last_heartbeat < NOW() - make_interval(secs => %s))
                    """,
                    (device_id, int(timeout_seconds)),
                )
                if changed:
                    newly_offline.append(device_id)
            return newly_offline
        except Exception as e:  # noqa: BLE001
            log.error(f"标记离线设备失败：{e}")
            return []

    # ==================== 内部方法 ====================

    def _channel_has_state(self, channel: str) -> bool:
        """迁移 008 是否已部署且该渠道至少已有一行状态。"""
        try:
            db = self._db()
            rows = self._execute_query_with_timeouts(
                db,
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM keyword_channel_state
                        WHERE channel = %s
                    ) AS has_any,
                    NOT EXISTS (
                        SELECT 1 FROM keywords k
                        WHERE k.enabled = TRUE
                          AND k.channels @> ARRAY[%s]::text[]
                          AND NOT EXISTS (
                              SELECT 1 FROM keyword_channel_state s
                              WHERE s.keyword_id = k.id AND s.channel = %s
                          )
                    ) AS seed_complete
                """,
                (channel, channel, channel),
            )
            return bool(rows and rows[0][0] and rows[0][1])
        except Exception:  # noqa: BLE001
            return False

    def _get_available_keywords(self, channel: str, limit: int = 100) -> List[str]:
        """从数据库获取可采关键词列表（按权重排序）。

        按渠道调度：迁移 008 和渠道播种是领取硬前置，按 (词,渠道) 状态取可采词，
        搜一搜/搜狗各自独立循环；缺失时返回空列表，不再进入不具备 owner 的旧 fallback。
        条件：enabled 且 status ∈ (pending, failed) 且 next_collect_time 到期。
        """
        try:
            db = self._db()
            rows = self._execute_query_with_timeouts(
                db,
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
        except Exception as e:  # noqa: BLE001
            log.error(f"查询可采关键词失败：{e}")
            return []
        return [row[0] for row in rows]

    def _insert_keyword_to_db(self, keyword: str, category: str = None) -> bool:
        """原子写关键词及全部渠道状态，避免完整播种门禁被新增词打断。"""
        db = self._db()
        cur = None
        try:
            cur = db.cursor()
            self._set_pg_local_timeouts(cur)
            cur.execute(
                """
                INSERT INTO keywords (keyword, category, created_at, next_collect_time)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (keyword) DO NOTHING
                RETURNING id, channels
                """,
                (keyword, category),
            )
            row = cur.fetchone()
            inserted = row is not None
            if row is None:
                cur.execute(
                    "SELECT id, channels FROM keywords WHERE keyword=%s",
                    (keyword,),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("关键词插入后无法回读")
            keyword_id, channels = row
            channels = list(channels or ["souyisou", "sogou"])
            cur.execute(
                """
                INSERT INTO keyword_channel_state
                    (keyword_id, channel, status, next_collect_time)
                SELECT %s, c.ch, 'pending', NOW()
                FROM unnest(%s::text[]) AS c(ch)
                ON CONFLICT (keyword_id, channel) DO NOTHING
                """,
                (keyword_id, channels),
            )
            db.commit()
            return inserted
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.error(f"插入关键词失败：{keyword}, {e}")
            return False
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _persist_result_state(self, keyword: str, channel: str, device_id: str,
                              articles_count: int, success: bool) -> str:
        """单事务提交关键词全局统计与渠道状态。

        返回 committed / already_committed / failed。already_committed 用于处理
        “PG 已提交、Redis 释放响应未知”后的同 lease 重试，避免重复累计统计。
        """
        db = self._db()
        cur = None
        desired = "completed" if success else "failed"
        ch_default = self.CHANNEL_DEFAULT_CYCLE.get(channel)
        try:
            cur = db.cursor()
            self._set_pg_local_timeouts(cur)
            cur.execute(
                "SELECT 1 FROM keyword_channel_state WHERE channel = %s LIMIT 1",
                (channel,),
            )
            has_channel_state = bool(cur.fetchone())
            if not has_channel_state:
                raise RuntimeError(
                    f"迁移 008 未部署或渠道未播种，拒绝提交结果：{channel}"
                )

            cur.execute(
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
                      AND s.status = 'running' AND s.claimer = %s
                    """,
                    (desired, int(articles_count or 0), ch_default,
                     channel, keyword, device_id),
            )
            if cur.rowcount != 1:
                cur.execute(
                        """
                        SELECT s.status, s.claimer
                        FROM keyword_channel_state s JOIN keywords k ON k.id=s.keyword_id
                        WHERE s.channel=%s AND k.keyword=%s
                        """,
                        (channel, keyword),
                )
                existing = cur.fetchone()
                if existing and existing[0] == desired and existing[1] is None:
                    db.rollback()
                    return "already_committed"
                raise RuntimeError(
                    f"渠道结果状态不属于当前租约：{channel}/{keyword}/{device_id}"
                )

            cur.execute(
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
                        desired,
                        keyword,
                    ),
            )

            db.commit()
            return "committed"
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.error(f"结果调度状态事务失败（{channel}/{keyword}）：{e}")
            return "failed"
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _persist_recovered_claims(self, records: List[dict]) -> bool:
        """在单个 PG 事务中把待回收租约恢复为 pending；支持提交后重试。"""
        db = self._db()
        cur = None
        try:
            cur = db.cursor()
            self._set_pg_local_timeouts(cur)
            for record in records:
                keyword = record["keyword"]
                channel = record["channel"]
                owner = record["data"].get("claimer")
                cur.execute(
                    """
                    SELECT 1 FROM keyword_channel_state s
                    JOIN keywords k ON k.id=s.keyword_id
                    WHERE s.channel=%s AND k.keyword=%s
                    """,
                    (channel, keyword),
                )
                has_channel_state = bool(cur.fetchone())
                if not has_channel_state:
                    raise RuntimeError(
                        f"迁移 008 未部署或渠道未播种，拒绝回收：{channel}"
                    )
                cur.execute(
                        """
                        UPDATE keyword_channel_state s SET status='pending', claimer=NULL,
                            last_claimed=NULL, next_collect_time=NOW()
                        FROM keywords k
                        WHERE s.keyword_id=k.id AND s.channel=%s AND k.keyword=%s
                          AND s.status='running' AND s.claimer=%s
                        """,
                        (channel, keyword, owner),
                )
                if cur.rowcount != 1:
                    cur.execute(
                            """
                            SELECT s.status, s.claimer FROM keyword_channel_state s
                            JOIN keywords k ON k.id=s.keyword_id
                            WHERE s.channel=%s AND k.keyword=%s
                            """,
                            (channel, keyword),
                    )
                    existing = cur.fetchone()
                    if (
                        existing
                        and existing[0] in {"completed", "failed"}
                        and existing[1] is None
                    ):
                        record["_pg_terminal_status"] = existing[0]
                        continue
                    if not existing or existing[0] != "pending" or existing[1] is not None:
                        raise RuntimeError(
                            f"回收状态不属于旧 owner：{channel}/{keyword}/{owner}"
                        )
            db.commit()
            return True
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.error(f"回收租约 PG 事务失败：{e}")
            return False
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

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
        scheduler.report_result(
            kw, articles_count=10, success=True,
            device_id="vm-001", channel="wechat_pc",
        )

    print("\n=== 调度器统计 ===")
    print(json.dumps(scheduler.get_statistics(), indent=2, ensure_ascii=False))
