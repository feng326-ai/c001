"""后台 LLM 清洗器——异步、可开关、不阻断主流程的大模型线索清洗流水线。

定位：采集入库 + 规则评分保持实时（秒级、稳）；大模型清洗与之彻底解耦，作为后台
周期任务「慢慢跑」，把已入库线索的活动信息与价值判定用大模型补全/纠正。挂了也只影响
补全质量，绝不拖累采集主流程。

一轮处理（run_once）：
  1. 按优先级 P0>P1>P2 捞一批「待清洗且未被人工编辑」的线索（batch_size 条）；
  2. 逐条送大模型分析（读原文全文），成功则回写线索表并标 done；
  3. 失败累加尝试次数：未达上限留 pending 等下轮重试（退避=beat 间隔），达上限标 fail；
  4. 熔断：一轮内连续失败达阈值（多半是大模型服务宕了），提前中止本轮、全体回退规则，
     下一轮再试，避免疯狂刷错误日志。

开关与配置（环境变量，默认关闭 → 零线上行为变化）：
  - LLM_CLEAN_ENABLED     总开关，默认 false；
  - LLM_CLEAN_BATCH       每轮处理条数，默认 5（配合 60s beat = 5 条/分钟）；
  - LLM_CLEAN_MAX_ATTEMPTS 单条最大尝试次数，默认 3；
  - LLM_CLEAN_BREAK_AFTER  连续失败熔断阈值，默认 3。

设计原则（与 alerting/backup 一致）：from_env 惰性构造、任何异常只 log 不抛、返回可序列化 dict。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def _truthy(val) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def _db_config() -> dict:
    """从 DATABASE_URL 解析 PG 连接参数；缺省回退 compose 默认值（与 tasks._db_config 同源）。"""
    url = os.getenv("DATABASE_URL")
    if url:
        p = urlsplit(url)
        return {
            "host": p.hostname or "localhost",
            "port": p.port or 5432,
            "database": (p.path or "/wx_search").lstrip("/") or "wx_search",
            "user": p.username or "admin",
            "password": p.password or "",
        }
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "database": os.getenv("POSTGRES_DB", "wx_search"),
        "user": os.getenv("POSTGRES_USER", "admin"),
        "password": os.getenv("POSTGRES_PASSWORD", "your_secure_password_here"),
    }


class LLMCleaner:
    """后台大模型清洗器。一次构造、可多轮调用 run_once（每轮自建/关闭一个 store 连接）。"""

    def __init__(self, db_config: dict, enabled: bool = False, batch_size: int = 5,
                 max_attempts: int = 3, break_after: int = 3):
        self.db_config = db_config
        self.enabled = enabled
        self.batch_size = max(1, int(batch_size))
        self.max_attempts = max(1, int(max_attempts))
        self.break_after = max(1, int(break_after))

    @classmethod
    def from_env(cls) -> "LLMCleaner":
        # 环境变量是部署角色的硬开关；未显式设置时才使用共享规则配置。
        env_enabled = _truthy(os.getenv("LLM_CLEAN_ENABLED", "false"))
        try:
            from wxsearch.ai_filters.llm_client import get_clean_enabled
            enabled = get_clean_enabled(default=env_enabled)
        except Exception:  # noqa: BLE001
            enabled = env_enabled
        return cls(
            db_config=_db_config(),
            enabled=enabled,
            batch_size=int(os.getenv("LLM_CLEAN_BATCH", "5") or 5),
            max_attempts=int(os.getenv("LLM_CLEAN_MAX_ATTEMPTS", "3") or 3),
            break_after=int(os.getenv("LLM_CLEAN_BREAK_AFTER", "3") or 3),
        )

    def run_once(self, limit: int = None, dry_run: bool = False) -> dict:
        """处理一批待清洗线索。返回统计 dict。

        Args:
            limit:   本轮处理条数上限，缺省用 batch_size；回填脚本可传更大值。
            dry_run: True 时只调用大模型并返回「拟写入」预览，不落库（供回填 --dry-run）。
        """
        if not self.enabled and not dry_run:
            return {"skipped": "disabled"}

        limit = self.batch_size if limit is None else int(limit)
        stats = {"picked": 0, "done": 0, "failed": 0, "skipped_empty": 0,
                 "circuit_broken": False, "dry_run": dry_run, "previews": []}

        # 惰性 import：避免模块加载期即连库/连大模型。
        from wxsearch.smart_dedup_store import SmartDedupStore
        from wxsearch.ai_filters.llm_analyzer import analyze as llm_analyze, to_ai_result, build_event_fields, build_organizer_contact

        store = SmartDedupStore(self.db_config)
        try:
            rows = store.fetch_pending_llm_leads(limit)
            stats["picked"] = len(rows)
            consecutive_fail = 0

            for lead_id, article_id, title, content, publish_time in rows:
                pub = str(publish_time) if publish_time else None
                try:
                    data = llm_analyze(str(title or ""), str(content or ""), pub)
                    ai = to_ai_result(data)
                    name, details = build_event_fields(data)
                    organizer_contact = build_organizer_contact(data)

                    if dry_run:
                        stats["previews"].append({
                            "lead_id": lead_id, "event_name": name,
                            "is_lead": ai.is_lead, "priority_level": ai.priority_level,
                            "priority_score": ai.priority_score, "details": details,
                        })
                        stats["done"] += 1
                    else:
                        store.save_llm_enrichment(
                            lead_id, article_id, ai, name, details,
                            is_online_voting=data.get("is_online_voting"),
                            online_voting_url=data.get("online_voting_url", ""),
                            is_recurring=data.get("is_recurring"),
                            activity_category=data.get("activity_category", ""),
                            activity_region=data.get("activity_region", ""),
                            recurrence=data.get("recurrence", ""),
                            activity_status=data.get("activity_status", ""),
                            organizer_name=data.get("organizer", ""),
                            organizer_region=data.get("organizer_region", ""),
                            voting_platform=data.get("voting_platform", ""),
                            organizer_contact=organizer_contact,
                            voting_status=data.get("voting_status", ""),
                            recurrence_period=data.get("recurrence_period", ""),
                            edition_no=data.get("edition_no"),
                        )
                        stats["done"] += 1
                    consecutive_fail = 0
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"LLM 清洗失败 (lead_id={lead_id})：{exc}")
                    stats["failed"] += 1
                    consecutive_fail += 1
                    if not dry_run:
                        store.mark_llm_failed(lead_id, self.max_attempts)
                    # 熔断：连续失败多半是服务宕了，提前中止本轮，交给下一轮重试。
                    if consecutive_fail >= self.break_after:
                        stats["circuit_broken"] = True
                        log.error(f"⛔ 连续 {consecutive_fail} 次失败，触发熔断，本轮中止（下轮再试）")
                        break

            if stats["done"] or stats["failed"]:
                log.info(f"🧽 LLM 清洗一轮完成：{stats}")
            return stats
        except Exception as e:  # noqa: BLE001
            log.error(f"❌ LLM 清洗轮次异常：{e}")
            stats["error"] = str(e)
            return stats
        finally:
            store.close()


# ==================== 命令行自测 ====================

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="后台 LLM 清洗器自测（默认 dry-run 预览，不落库）")
    parser.add_argument("--limit", type=int, default=3, help="处理条数")
    parser.add_argument("--write", action="store_true", help="真正落库（默认 dry-run 只预览）")
    args = parser.parse_args()

    cleaner = LLMCleaner.from_env()
    cleaner.enabled = True  # 自测时强制开启，绕过 LLM_CLEAN_ENABLED
    result = cleaner.run_once(limit=args.limit, dry_run=not args.write)
    print(json.dumps(result, ensure_ascii=False, indent=2))
