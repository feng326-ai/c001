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

import time

from celery import Celery

from .collector import Collector
from .config import AppConfig

CLAIM_TASK = "wxsearch.tasks.claim_keywords_task"
REPORT_TASK = "wxsearch.tasks.report_result_task"
COLLECT_SETTINGS_TASK = "wxsearch.tasks.get_collection_settings_task"
HEARTBEAT_TASK = "wxsearch.tasks.heartbeat_device_task"


class UnattendedRunner:
    """采集器无人循环编排器。"""

    def __init__(self, config: AppConfig, logger):
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
        # 复用一个长跑的 Collector（其内部 DistributedSink 一次构造、多轮共用）。
        self.collector = Collector(config, logger)

    # ---- 轻量通道：调用 worker 上的领取/上报任务 ----

    def _claim(self):
        """向 worker 领取一批关键词。异常/超时返回空列表（VM 侧按“无词”休眠）。"""
        try:
            res = self._app.send_task(
                CLAIM_TASK,
                args=[self.uc.channel, self.uc.vm_instance_id, self.uc.max_keywords],
            )
            kws = res.get(timeout=self.uc.claim_timeout)
            return list(kws or [])
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"领取关键词失败（broker 不可达？）：{exc}")
            return []

    def _report(self, keyword: str, count: int, success: bool, error: str = None):
        """向 worker 上报单词采集结果。异常只 log，不打断循环。"""
        try:
            res = self._app.send_task(
                REPORT_TASK,
                args=[keyword, count, success, error, self.uc.vm_instance_id, self.uc.channel],
            )
            res.get(timeout=self.uc.claim_timeout)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"上报结果失败（{keyword}）：{exc}")

    def _report_heartbeat(self, current_keyword: str = None):
        """上报设备心跳（在线/当前在采词）。fire-and-forget，异常只 log，绝不阻断采集。"""
        try:
            self._app.send_task(
                HEARTBEAT_TASK,
                args=[self.uc.vm_instance_id, getattr(self.uc, "device_type", "pc"),
                      self.uc.channel, current_keyword],
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"上报心跳失败：{exc}")

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
            while True:
                try:
                    self._run_one_round()
                except Exception as exc:  # noqa: BLE001
                    # 顶层兜底：任何未预期异常都不退出，休眠后重来（自愈）。
                    self.log.exception(f"本轮发生未预期错误，{self.uc.round_sleep_sec}s 后重试：{exc}")
                    time.sleep(self.uc.round_sleep_sec)
        except KeyboardInterrupt:
            self.log.info("收到停止信号，优雅退出。已领取的词将由 worker 端到期自愈回收。")
            self._shutdown()
            return 0

    def _run_one_round(self) -> None:
        """执行一轮：先心跳+拉取最新采集参数，再领取一批词，逐词采集并上报，然后休眠。"""
        self._report_heartbeat()
        self._refresh_collect_settings()
        keywords = self._claim()
        if not keywords:
            self.log.info(f"无可采关键词，休眠 {self.uc.idle_sleep_sec}s…")
            time.sleep(self.uc.idle_sleep_sec)
            return

        self.log.info(f"本轮领取 {len(keywords)} 个关键词：{keywords}")
        for keyword in keywords:
            self.log.info("-" * 40)
            self.log.info(f"开始采集：{keyword}")
            self._report_heartbeat(current_keyword=keyword)  # 标记当前在采词
            try:
                count = self.collector.collect_keyword(keyword)
                self.log.info(f"关键词「{keyword}」完成，新增 {count} 条。")
                self._report(keyword, count, success=True)
            except Exception as exc:  # noqa: BLE001
                # 单词失败：只 log + 上报失败，继续下一词，绝不中断整轮。
                self.log.exception(f"关键词「{keyword}」采集失败：{exc}")
                self._report(keyword, 0, success=False, error=str(exc)[:200])

        self.log.info(f"本轮完成，休眠 {self.uc.round_sleep_sec}s 后进入下一轮…")
        time.sleep(self.uc.round_sleep_sec)

    def _shutdown(self) -> None:
        """释放本进程资源（关闭 Collector 落库层与 Celery 生产者）。"""
        try:
            self.collector.db.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._app.close()
        except Exception:  # noqa: BLE001
            pass
