"""采集编排：串联驱动与数据库，按关键词循环采集入库。"""

from __future__ import annotations

import time

from .config import AppConfig
from .db import Article, Database
from .wechat_driver import DriverError, WeChatSearchDriver
from .ai_filters.rule_filter import RuleBasedFilter
from .ingest_quality import evaluate_article, evaluate_keyword


class Collector:
    def __init__(self, config: AppConfig, logger):
        self.cfg = config
        self.log = logger
        # 落库汇聚层：分布式开关开启则投递到 Celery(worker 端做 PG 三层去重)，
        # 否则走本地 SQLite。两者接口一致(save/count/close/last_reason)，采集逻辑无感。
        dist = getattr(config, "distributed", None)
        if dist is not None and getattr(dist, "enabled", False):
            from .distributed_sink import DistributedSink
            self.db = DistributedSink(dist, logger)
            mode = "同步等结果" if getattr(dist, "wait_result", True) else "fire-and-forget"
            logger.info(f"落库模式：分布式投递 → {dist.broker_url}（{mode}）")
        else:
            self.db = Database(config.db_path, getattr(config, "dedup", None))  # 去重层：由 config.dedup 控制 basic/smart(三层)
        self.driver = self._make_driver(config, logger)
        self.rule_filter = RuleBasedFilter(getattr(config, "cleaning", None))  # AI 清洗第一层：规则过滤，阈值/黑名单由 config.cleaning 控制

    @staticmethod
    def _make_driver(config, logger):
        """按渠道选驱动：sogou/搜狗 → SogouDriver(Playwright)；其余 → PC 搜一搜(UIA)。

        渠道取 unattended.channel（无人循环）或 config.channel；三种驱动接口一致。
        """
        uc = getattr(config, "unattended", None)
        channel = str(getattr(uc, "channel", "") or getattr(config, "channel", "") or "").lower()
        if "sogou" in channel or "搜狗" in channel:
            from .collectors.sogou_pw import SogouDriver
            logger.info(f"采集驱动：搜狗微信(Playwright+MQQBrowser UA)，channel={channel}")
            return SogouDriver(config, logger)
        return WeChatSearchDriver(config, logger)

    def run(self) -> None:
        total_new = 0
        for keyword in self.cfg.keywords:
            self.log.info("=" * 50)
            self.log.info(f"开始采集关键词：{keyword}")
            try:
                new = self._collect_one(keyword)
                total_new += new
                self.log.info(f"关键词「{keyword}」完成，新增 {new} 条。")
            except DriverError as exc:
                self.log.error(f"关键词「{keyword}」采集失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                self.log.exception(f"关键词「{keyword}」发生未预期错误：{exc}")

        self.log.info("=" * 50)
        self.log.info(f"全部完成。本次新增 {total_new} 条，数据库累计 {self.db.count()} 条。")
        self.db.close()

    def collect_keyword(self, keyword: str) -> int:
        """采集单个关键词并返回新增条数（无人值守循环逐词调用）。

        与 run() 不同：**不** close() 落库层，供 UnattendedRunner 长跑时复用同一个
        Collector/DistributedSink（一批多轮共用，避免每词重建连接）。
        异常向上抛出，由调用方（循环）决定如何上报失败。
        """
        return self._collect_one(keyword)

    def _collect_one(self, keyword: str) -> int:
        # run() 与无人值守 collect_keyword() 共用这一入口；停用关键词必须在
        # 任何微信 UI 操作之前拒绝，不能只保护手工单次运行路径。
        keyword_decision = evaluate_keyword(keyword)
        if not keyword_decision.accepted:
            self.log.warning(
                f"关键词「{keyword}」已在搜索前拒绝：{keyword_decision.reason}"
            )
            return 0
        win = self._open_and_filter(keyword)

        new_count = 0
        filtered_count = 0
        dedup_count = 0
        for article in self.driver.iter_articles(win, keyword):
            # 入口门禁串联低质规则与高召回语义判断。规则自身异常也会返回拒绝，
            # 绝不能恢复旧的 filter_error_passthrough。
            decision = evaluate_article(article, mode="realtime_signal", rule_filter=self.rule_filter)
            if not decision.accepted:
                filtered_count += 1
                self.log.info(f"  - 准入拒绝：{article.title}  [{decision.reason}]")
                continue

            if self.db.save(article):
                new_count += 1
                self.log.info(f"  + {article.title}  [{article.account} {article.publish_time}]")
            elif self.db.last_reason in ("exact_duplicate", "similar_duplicate") or \
                    self.db.last_reason.startswith(("exact_duplicate", "similar_duplicate")):
                dedup_count += 1
                self.log.info(f"  = 去重跳过：{article.title}  [{self.db.last_reason}]")
            else:
                filtered_count += 1
                self.log.info(f"  - 服务端拒绝：{article.title}  [{self.db.last_reason}]")
        if filtered_count:
            self.log.info(f"【清洗】关键词「{keyword}」过滤掉 {filtered_count} 条低质内容。")
        if dedup_count:
            self.log.info(f"【去重】关键词「{keyword}」跳过 {dedup_count} 条重复/近似内容。")
        return new_count

    def _open_and_filter(self, keyword: str, retries: int = 3):
        """搜索并应用筛选；遇瞬时性 UIA/COM 错误时重试。

        多关键词连续搜索时，窗口/DOM 处于刷新中可能抛 COMError
        （如“事件无法调用任何订阅者”），重试一次即可恢复。
        """
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                win = self.driver.open_search(keyword)
                if self.cfg.result_type == "article":
                    self.driver.apply_filters(win)
                return win
            except DriverError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.log.warning(
                    f"搜索/筛选第 {attempt} 次失败：{exc}"
                    + ("，稍候重试…" if attempt < retries else "")
                )
                time.sleep(3.0)
        raise last_exc
