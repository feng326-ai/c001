"""存量文章 AI 评分回填脚本（在 worker 容器内执行）。

用法（宿主机）：
    docker exec wxsearch_worker python -m wxsearch.backfill_scoring
    docker exec wxsearch_worker python -m wxsearch.backfill_scoring --all

默认只处理「尚未评分」(priority_score IS NULL) 的文章；带 --all 则强制重算全部。
评分走离线规则评分器 RuleScorer，零成本、无外呼；复用 SmartDedupStore.save_scoring
回写那 7 个 AI 字段，与新增文章的入库路径完全一致。
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

from wxsearch.tasks import _db_config
from wxsearch.smart_dedup_store import SmartDedupStore
from wxsearch.ai_filters.rule_scorer import RuleScorer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill_scoring")


def run(rescore_all: bool = False) -> dict:
    """回填评分。rescore_all=True 时重算全部（含已评分），否则只补未评分的。"""
    store = SmartDedupStore(_db_config())
    scorer = RuleScorer()

    where = "" if rescore_all else "WHERE priority_score IS NULL"
    store.cur.execute(f"""
        SELECT id, title, summary, account, account_id, publish_time,
               source_channel, keyword, content, content_clean
        FROM articles_core
        {where}
        ORDER BY id
    """)
    rows = store.cur.fetchall()

    stats = {"total": len(rows), "scored": 0, "lead": 0, "promoted": 0, "failed": 0,
             "by_level": {"P0": 0, "P1": 0, "P2": 0}}

    for (aid, title, summary, account, account_id, publish_time,
         source_channel, keyword, content, content_clean) in rows:
        obj = SimpleNamespace(
            title=title or "",
            summary=summary or "",
            account=account or "",
            account_id=account_id or "",
            publish_time=publish_time,   # PG timestamptz → datetime，RuleScorer 可直接解析
            source_channel=source_channel or "",
            keyword=keyword or "",
            content=content or "",
            content_clean=content_clean or "",
        )
        ai = scorer.score(obj)
        if store.save_scoring(aid, ai):
            stats["scored"] += 1
            stats["by_level"][ai.priority_level] = stats["by_level"].get(ai.priority_level, 0) + 1
            if ai.is_lead:
                stats["lead"] += 1
                # 线索同步提升进 qualified_leads，与新采文章路径一致。
                if store.promote_lead(aid):
                    stats["promoted"] += 1
        else:
            stats["failed"] += 1

    store.close()
    log.info(f"✅ 回填完成：{stats}")
    return stats


if __name__ == "__main__":
    run(rescore_all="--all" in sys.argv)
