"""无人值守采集循环（阶段四·无人值守 第 4 步）。

让装微信的那台 VM 自己转起来：`领关键词 → 采集 → 上报 → 休眠 → 下一轮`，
单台即可，异常自愈、绝不整体退出。

沿用已验证的「VM 轻量」原则：
  - 领取/上报走轻量 Celery 通道（与 DistributedSink 一致）——只用 send_task 按名
    调用 worker 上的 claim_keywords_task/report_result_task，**不 import wxsearch.tasks**，
    VM 侧依赖只需 celery+redis 客户端，不装 psycopg2、不直连 PG。
  - 文章投递复用 Collector 内已接好的 DistributedSink（distributed.enabled=true 时生效）。
  - broker/backend 复用 config.distributed 段，不重复配置。

健壮性：
  - 单词采集异常只 log 并 report(success=False)，继续下一词；
  - 每一轮用顶层 try/except 包住，任何异常 log 后休眠再来，**绝不退出**；
  - KeyboardInterrupt → 优雅停止（已领取的词由 worker 端 recover_stale_claims 到期自愈回收）。
"""

from __future__ import annotations

import threading
import time

from .config import AppConfig

CLAIM_TASK = "wxsearch.tasks.claim_keywords_task"
REPORT_TASK = "wxsearch.tasks.report_result_task"
COLLECT_SETTINGS_TASK = "wxsearch.tasks.get_collection_settings_task"
HEARTBEAT_TASK = "wxsearch.tasks.heartbeat_device_task"


class UnattendedRunner:
    """采集器无人循环编排器。"""

    def __init__(self, config: AppConfig, logger):
        # 放在运行时导入：服务端/单元测试只读本模块时不需要加载 Windows UIA 依赖。
        from celery import Celery
        from .collector import Collector

        self.cfg = config
        self.log = logger
        self.uc = config.unattended
        self.dist = config.distributed

        # 轻量 Celery 生产者：仅按名调用 worker 上的 claim/report 任务（不 import tasks.py）。
        # broker/backend 复用 distributed 段（指向宿主机 Redis）。
        self._app = Celery(
            "wxsearch_unattended",
            broker=self.dist.broker_url,
            backend=self.dist.result_backend,
        )
        # 心跳与 UIA 采集完全解耦，并使用独立 Celery producer，避免长采集或共享
        # producer 连接状态阻断心跳。
        self._heartbeat_app = Celery(
            "wxsearch_heartbeat",
            broker=self.dist.broker_url,
            backend=self.dist.result_backend,
        )
        requested_interval = float(getattr(self.uc, "heartbeat_interval_sec", 60) or 60)
        self._heartbeat_interval_sec = min(180.0, max(10.0, requested_interval))
        if requested_interval != self._heartbeat_interval_sec:
            self.log.warning(
                f"heartbeat_interval_sec={requested_interval} 不安全，"
                f"已限制为 {self._heartbeat_interval_sec}s（现网在线阈值 600s）。"
            )
        self._heartbeat_result_timeout_sec = max(
            1.0, float(getattr(self.uc, "heartbeat_result_timeout_sec", 10) or 10)
        )
        # 即使 producer 线程仍 alive，只要最近一次成功心跳已经过旧，也必须停领。
        # 上限在 interval=180s 时为 550s，仍严格小于服务端 600s 离线阈值。
        self._heartbeat_claim_max_age_sec = min(
            540.0,
            self._heartbeat_interval_sec * 3 + self._heartbeat_result_timeout_sec,
        )
        self._heartbeat_failure_threshold = max(
            1, int(getattr(self.uc, "heartbeat_failure_threshold", 3) or 3)
        )
        self._state_lock = threading.Lock()
        self._heartbeat_send_lock = threading.Lock()
        self._current_keyword = None
        self._active_claims = {}
        self._heartbeat_failures = 0
        self._heartbeat_confirmed_once = False
        self._last_heartbeat_success_monotonic = None
        self._claim_paused_logged = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_wake = threading.Event()
        self._heartbeat_ready = threading.Event()
        self._heartbeat_thread = None
        self._shutdown_done = False
        # 复用一个长跑的 Collector（其内部 DistributedSink 一次构造、多轮共用）。
        self.collector = Collector(config, logger)

    # ---- 轻量通道：调用 worker 上的领取/上报任务 ----

    def _claim(self):
        """向 worker 领取一批关键词。异常/超时返回空列表（VM 侧按“无词”休眠）。"""
        if (
            self._heartbeat_thread is not None
            and not self._heartbeat_thread.is_alive()
            and not self._heartbeat_stop.is_set()
        ):
            self.log.warning("心跳线程意外退出，正在重建；恢复确认前不领取新关键词。")
            with self._state_lock:
                self._heartbeat_confirmed_once = False
                self._heartbeat_ready.clear()
            self._start_heartbeat()
        if not self._can_claim():
            if not self._claim_paused_logged:
                self.log.warning(
                    "心跳尚未建立或连续失败，暂停领取新关键词；"
                    "已领取任务不受影响，心跳恢复后自动继续。"
                )
                self._claim_paused_logged = True
            return []
        self._claim_paused_logged = False
        try:
            res = self._app.send_task(
                CLAIM_TASK,
                args=[self.uc.channel, self.uc.vm_instance_id, self.uc.max_keywords, True],
            )
            raw_claims = list(res.get(timeout=self.uc.claim_timeout) or [])
            claims = []
            for item in raw_claims:
                if isinstance(item, dict):
                    keyword = str(item.get("keyword") or "").strip()
                    lease_id = str(item.get("lease_id") or "").strip()
                else:
                    # 只用于诊断兼容；生产发布必须先升级 worker，v2 节点应收到 dict。
                    keyword = str(item or "").strip()
                    lease_id = ""
                if not keyword:
                    raise ValueError(f"领取响应含无效关键词：{item!r}")
                claims.append({"keyword": keyword, "lease_id": lease_id})
            return claims
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"领取关键词失败（broker 不可达？）：{exc}")
            return []

    def _report(self, keyword: str, count: int, success: bool, error: str = None,
                lease_id: str = None) -> bool:
        """向 worker 上报单词采集结果。返回 worker 是否明确确认，异常不打断循环。"""
        attempts = max(1, int(getattr(self.uc, "report_retry_attempts", 3) or 3))
        backoff = max(
            0.0, float(getattr(self.uc, "report_retry_backoff_sec", 2) or 0)
        )
        for attempt in range(1, attempts + 1):
            try:
                res = self._app.send_task(
                    REPORT_TASK,
                    args=[keyword, count, success, error, self.uc.vm_instance_id,
                          self.uc.channel, lease_id],
                )
                if bool(res.get(timeout=self.uc.claim_timeout)):
                    return True
                message = "worker 返回 False"
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
            if attempt < attempts:
                self.log.warning(
                    f"上报结果未确认（{keyword}，{attempt}/{attempts}）："
                    f"{message}；将按同一 lease 重试。"
                )
                if backoff:
                    time.sleep(backoff * attempt)
        self.log.warning(f"上报结果最终失败（{keyword}，共 {attempts} 次）：{message}")
        return False

    def _snapshot_heartbeat_state(self):
        with self._state_lock:
            active_claims = [
                {"keyword": keyword, "lease_id": lease_id}
                for keyword, lease_id in sorted(self._active_claims.items())
            ]
            return self._current_keyword, active_claims

    def _set_current_keyword(self, keyword: str = None) -> None:
        with self._state_lock:
            self._current_keyword = keyword
        self._heartbeat_wake.set()

    def _set_active_claims(self, claims) -> None:
        with self._state_lock:
            self._active_claims = {
                str(claim["keyword"]): str(claim.get("lease_id") or "")
                for claim in (claims or []) if claim.get("keyword")
            }
        self._heartbeat_wake.set()

    def _complete_keyword(self, keyword: str, lease_id: str = None) -> None:
        with self._state_lock:
            current_lease = self._active_claims.get(keyword)
            if lease_id is None or current_lease == lease_id:
                self._active_claims.pop(keyword, None)
            if self._current_keyword == keyword:
                self._current_keyword = None
        self._heartbeat_wake.set()

    def _clear_active_claims(self) -> None:
        with self._state_lock:
            self._active_claims.clear()
            self._current_keyword = None
        self._heartbeat_wake.set()

    def _can_claim(self) -> bool:
        now = time.monotonic()
        with self._state_lock:
            heartbeat_age_ok = (
                self._last_heartbeat_success_monotonic is not None
                and now - self._last_heartbeat_success_monotonic
                    <= self._heartbeat_claim_max_age_sec
            )
            state_ok = (
                self._heartbeat_ready.is_set()
                and self._heartbeat_confirmed_once
                and self._heartbeat_failures < self._heartbeat_failure_threshold
                and heartbeat_age_ok
            )
        thread_ok = self._heartbeat_thread is None or self._heartbeat_thread.is_alive()
        return state_ok and thread_ok

    def _report_heartbeat(self) -> bool:
        """串行化后台心跳与每词开始前的租约确认。"""
        with self._heartbeat_send_lock:
            return self._report_heartbeat_once()

    def _report_heartbeat_once(self) -> bool:
        """上报设备状态并等待 worker 明确确认；调用方已持有发送锁。"""
        current_keyword, active_keywords = self._snapshot_heartbeat_state()
        try:
            res = self._heartbeat_app.send_task(
                HEARTBEAT_TASK,
                args=[
                    self.uc.vm_instance_id,
                    getattr(self.uc, "device_type", "pc"),
                    self.uc.channel,
                    current_keyword,
                    active_keywords,
                ],
            )
            if not bool(res.get(timeout=self._heartbeat_result_timeout_sec)):
                raise RuntimeError("worker 返回 False")
            with self._state_lock:
                recovered = self._heartbeat_failures > 0
                self._heartbeat_failures = 0
                self._heartbeat_confirmed_once = True
                self._last_heartbeat_success_monotonic = time.monotonic()
            if recovered:
                self.log.info("设备心跳已恢复，允许继续领取新关键词。")
            return True
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._heartbeat_failures += 1
                failures = self._heartbeat_failures
            if failures == 1 or failures % self._heartbeat_failure_threshold == 0:
                self.log.warning(
                    f"上报心跳失败（连续 {failures} 次）：{exc}"
                )
            return False
        finally:
            self._heartbeat_ready.set()

    def _heartbeat_loop(self) -> None:
        """固定节拍心跳泵；状态变更时由 wake 事件触发一次即时上报。"""
        while not self._heartbeat_stop.is_set():
            self._report_heartbeat()
            self._heartbeat_wake.wait(self._heartbeat_interval_sec)
            self._heartbeat_wake.clear()

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"wxsearch-heartbeat-{self.uc.vm_instance_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        # 首次心跳未完成前不领词；有界等待，broker 故障时主循环仍会继续自愈。
        self._heartbeat_ready.wait(min(5.0, self._heartbeat_result_timeout_sec + 0.5))

    def _refresh_collect_settings(self):
        """从 worker 拉取最新采集参数（页面保存的搜一搜筛选）并覆盖到本地配置。

        用服务端配置（rule_config.json 的 collect_settings）覆盖 self.cfg，使「采集设置页」
        的修改无需重启采集器即可在下一轮生效。拉不到（broker 不可达/任务未注册）
        时只 log，保留本地 config.json 参数，绝不中断采集。
        """
        try:
            res = self._app.send_task(COLLECT_SETTINGS_TASK)
            settings = res.get(timeout=self.uc.claim_timeout) or {}
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"拉取采集参数失败，沿用本地配置：{exc}")
            return
        self._apply_collect_settings(settings)

    def _apply_collect_settings(self, settings: dict) -> None:
        """把服务端采集参数的 wechat 块应用到共享的 self.cfg（采集器持同一引用，即时生效）。"""
        w = (settings or {}).get("wechat", {}) or {}
        if not w:
            return
        sel, col = self.cfg.selectors, self.cfg.collect
        changed = []
        if w.get("filter_sort"):
            sel.filter_sort = str(w["filter_sort"]); changed.append(f"排序={sel.filter_sort}")
        if w.get("filter_type"):
            sel.filter_type = str(w["filter_type"]); changed.append(f"类型={sel.filter_type}")
        if w.get("filter_time"):
            sel.filter_time = str(w["filter_time"]); changed.append(f"时间={sel.filter_time}")
        if "filter_scope" in w:
            sel.filter_scope = str(w["filter_scope"] or "")
        try:
            if w.get("max_items_per_keyword"):
                col.max_items_per_keyword = int(w["max_items_per_keyword"]); changed.append(f"每词={col.max_items_per_keyword}")
            if w.get("max_scrolls"):
                col.max_scrolls = int(w["max_scrolls"])
        except (ValueError, TypeError):
            pass
        if changed:
            self.log.info(f"🔄 已应用服务端采集参数：{' '.join(changed)}")

    # ---- 主循环 ----

    def run_forever(self) -> int:
        """无人循环主入口：领→采→报→休眠→下一轮，绝不因异常整体退出。"""
        self.log.info("=" * 50)
        self.log.info(
            f"无人值守启动：channel={self.uc.channel} vm={self.uc.vm_instance_id} "
            f"每轮最多领 {self.uc.max_keywords} 词，broker={self.dist.broker_url}"
        )
        if not self.dist.enabled:
            self.log.warning(
                "distributed.enabled=false：文章投递将落本地 SQLite 而非分布式。"
                "无人循环仍会领词/上报，但建议 VM 侧开启 distributed。"
            )

        try:
            self._start_heartbeat()
            while True:
                try:
                    self._run_one_round()
                except Exception as exc:  # noqa: BLE001
                    # 顶层兜底：任何未预期异常都不退出，休眠后重来（自愈）。
                    self.log.exception(f"本轮发生未预期错误，{self.uc.round_sleep_sec}s 后重试：{exc}")
                    time.sleep(self.uc.round_sleep_sec)
        except KeyboardInterrupt:
            self.log.info("收到停止信号，优雅退出。已领取的词将由 worker 端到期自愈回收。")
            return 0
        finally:
            self._shutdown()

    def _run_one_round(self) -> None:
        """执行一轮：先心跳+拉取最新采集参数，再领取一批词，逐词采集并上报，然后休眠。"""
        self._refresh_collect_settings()
        claims = self._claim()
        if not claims:
            self.log.info(f"无可采关键词，休眠 {self.uc.idle_sleep_sec}s…")
            time.sleep(self.uc.idle_sleep_sec)
            return

        keywords = [claim["keyword"] for claim in claims]
        self.log.info(f"本轮领取 {len(keywords)} 个关键词：{keywords}")
        self._set_active_claims(claims)
        try:
            for claim in claims:
                keyword = claim["keyword"]
                lease_id = claim.get("lease_id") or None
                self.log.info("-" * 40)
                self.log.info(f"开始采集：{keyword}")
                # 每个词开始前重新确认整批剩余 lease。Redis 丢失/owner 变化时
                # 允许当前词收尾，但绝不继续执行本地批次里的后续旧任务。
                self._set_current_keyword(keyword)
                if not self._report_heartbeat():
                    self._set_current_keyword(None)
                    self.log.warning(
                        f"关键词「{keyword}」开始前租约确认失败，终止本批剩余任务。"
                    )
                    break
                try:
                    count = self.collector.collect_keyword(keyword)
                    self.log.info(f"关键词「{keyword}」完成，新增 {count} 条。")
                    self._report(keyword, count, success=True, lease_id=lease_id)
                except Exception as exc:  # noqa: BLE001
                    # 单词失败：只 log + 上报失败，继续下一词，绝不中断整轮。
                    self.log.exception(f"关键词「{keyword}」采集失败：{exc}")
                    self._report(
                        keyword, 0, success=False, error=str(exc)[:200],
                        lease_id=lease_id,
                    )
                finally:
                    # 无论结果上报是否成功，都停止续租本关键词；v2 stale recovery
                    # 仅按续租时间判断，不会被“设备仍在线”永久保护。
                    self._complete_keyword(keyword, lease_id)
        finally:
            # 循环编排自身发生异常时，也停止续租尚未开始的批次任务。
            self._clear_active_claims()

        self.log.info(f"本轮完成，休眠 {self.uc.round_sleep_sec}s 后进入下一轮…")
        time.sleep(self.uc.round_sleep_sec)

    def _shutdown(self) -> None:
        """释放本进程资源（关闭 Collector 落库层与 Celery 生产者）。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._heartbeat_stop.set()
        self._heartbeat_wake.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=self._heartbeat_result_timeout_sec + 1.0)
        heartbeat_still_alive = bool(
            self._heartbeat_thread and self._heartbeat_thread.is_alive()
        )
        if heartbeat_still_alive:
            self.log.warning("心跳线程仍在阻塞；保留其 Celery producer，由进程退出回收。")
        try:
            self.collector.db.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            driver = getattr(self.collector, "driver", None)
            if driver is not None and hasattr(driver, "close"):
                driver.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._app.close()
        except Exception:  # noqa: BLE001
            pass
        if not heartbeat_still_alive:
            try:
                self._heartbeat_app.close()
            except Exception:  # noqa: BLE001
                pass
