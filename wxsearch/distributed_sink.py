"""分布式投递汇聚层。

作为 SQLite `Database` 的“鸭子替身”：暴露同款 `save/count/close/last_reason`
接口，采集器无需感知底层是本地库还是分布式队列。启用后把每篇文章投递到
Celery(Redis broker)，由 worker 端执行 PostgreSQL 三层去重入库。

设计要点：
  - 只用 `celery.send_task(任务名)` 按名投递，**不 import wxsearch.tasks**，
    避免把 psycopg2 / SmartDedupStore 等 worker 端依赖拉进采集进程。
  - wait_result=True：同步等 worker 返回 {success, reason, id}，把真实去重
    结果映射回 last_reason，采集器现有的 new/dedup 计数分支可原样复用。
  - wait_result=False：fire-and-forget，投递成功即视为 new，last_reason=queued。
  - 字段映射：db.Article.source → models.Article.source_channel（必填）。
"""

from __future__ import annotations

import json

from celery import Celery

from datetime import datetime

from .db import Article


class DistributedSink:
    """把采集结果投递到 Celery 队列，接口对齐 db.Database。"""

    def __init__(self, dist_cfg, logger):
        self.cfg = dist_cfg
        self.log = logger
        self.last_reason = ""  # 每次 save 后的判定原因（供采集器日志）
        self._submitted = 0    # 已成功投递计数（count() 用）

        # 轻量 Celery 客户端：仅作生产者，broker/backend 与 worker 一致。
        self._app = Celery(
            "wxsearch_producer",
            broker=dist_cfg.broker_url,
            backend=dist_cfg.result_backend,
        )

    # ---- 字段映射：db.Article → worker 端 models.Article 构造参数 ----
    @staticmethod
    def _to_payload(article: Article) -> str:
        payload = {
            "title": article.title,
            "content": article.content,
            "url": article.url,
            # 关键差异：采集器叫 source，worker 端 models.Article 必填 source_channel。
            "source_channel": article.source or "wechat_pc",
            "keyword": article.keyword,
            "account": article.account,
            "account_id": getattr(article, "account_id", None),
            "publish_time": article.publish_time,
            "summary": article.summary,
            # 采集时刻：payload 在抓到文章后立即构造，此刻≈真实采集时间（区别于 worker 入库时刻）。
            "collected_at": datetime.now().isoformat(),
        }
        # 不传 created_at：让 worker 端 __post_init__ 自动填 datetime，避免字符串污染类型。
        return json.dumps(payload, ensure_ascii=False)

    def save(self, article: Article) -> bool:
        """投递一条文章。返回值/last_reason 语义对齐 Database.save：

        - wait_result=True：返回 worker 的真实 success，last_reason 为真实原因；
        - wait_result=False：投递成功即返回 True，last_reason="queued"。
        投递失败（broker 不可达等）返回 False，last_reason="submit_error"。
        """
        payload = self._to_payload(article)
        try:
            async_result = self._app.send_task(self.cfg.task_name, args=[payload])
        except Exception as exc:  # noqa: BLE001
            self.last_reason = "submit_error"
            self.log.error(f"  投递失败（broker 不可达？）：{article.title[:24]}（{exc}）")
            return False

        if not self.cfg.wait_result:
            self._submitted += 1
            self.last_reason = "queued"
            return True

        # 同步等待 worker 返回真实去重结果。
        try:
            res = async_result.get(timeout=self.cfg.result_timeout)
        except Exception as exc:  # noqa: BLE001
            # 等待超时/后端异常：任务可能仍在执行，视为已投递，避免误判丢失。
            self._submitted += 1
            self.last_reason = "queued"
            self.log.warning(f"  已投递但未取到结果（{exc}）：{article.title[:24]}")
            return True

        success = bool(res.get("success"))
        self.last_reason = str(res.get("reason") or ("new" if success else "duplicate"))
        if success:
            self._submitted += 1
        return success

    def count(self, keyword=None) -> int:
        """分布式模式下本地不持库，返回本进程已成功投递的累计条数。"""
        return self._submitted

    def close(self) -> None:
        try:
            self._app.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "DistributedSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
