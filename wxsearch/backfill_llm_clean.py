"""存量线索 LLM 清洗回填脚本（测试期一次性把库里旧线索跑一遍，在 worker 容器内执行）。

用法（宿主机）：
    # 先干跑预览：只调用大模型看「拟写入」结果，绝不落库（强烈建议先跑这个）
    docker exec wxsearch_worker python -m wxsearch.backfill_llm_clean --dry-run --limit 5

    # 确认没问题后小步放量真写库（不带 --dry-run 即真正回写）
    docker exec wxsearch_worker python -m wxsearch.backfill_llm_clean --limit 50

    # 全量回填（--limit 0 或不传 --limit 表示把所有待清洗线索跑完）
    docker exec wxsearch_worker python -m wxsearch.backfill_llm_clean

参数：
    --dry-run     只预览不落库（每次只抽一批做样本，因为不写库无法翻页）
    --limit N     本次最多处理多少条；0 或不传 = 处理全部待清洗线索
    --batch N     每轮抽取条数（限速节流用），缺省取环境变量 LLM_CLEAN_BATCH 或 5

说明：复用 LLMCleaner.run_once —— 与后台定时任务同一套清洗/回写/人工保护逻辑，
只是这里循环成批地把存量 pending 线索跑到清空。回填时强制开启清洗（绕过
LLM_CLEAN_ENABLED 总开关），但仍尊重「人工编辑过的绝不覆盖」与熔断保护。
"""

from __future__ import annotations

import argparse
import json
import logging

from wxsearch.llm_cleaner import LLMCleaner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill_llm_clean")


def run(limit: int = 0, batch: int = None, dry_run: bool = False) -> dict:
    """成批回填存量待清洗线索。

    Args:
        limit:   本次最多处理条数；<=0 表示处理全部待清洗线索。
        batch:   每轮抽取条数（节流），None 则用 cleaner 的 batch_size。
        dry_run: True 时只预览不落库（单批样本，因不写库无法翻页）。

    Returns:
        汇总统计 dict（累计各轮 picked/done/failed + 是否熔断/干跑）。
    """
    cleaner = LLMCleaner.from_env()
    cleaner.enabled = True  # 回填是显式手动操作，强制开启，绕过总开关
    per_round = int(batch) if batch else cleaner.batch_size

    total = {"rounds": 0, "picked": 0, "done": 0, "failed": 0,
             "circuit_broken": False, "dry_run": dry_run, "limit": limit,
             "previews": []}
    processed = 0

    while True:
        # 计算本轮抽取条数：受 --limit 剩余额度与每轮 batch 双重约束
        if limit and limit > 0:
            remaining = limit - processed
            if remaining <= 0:
                break
            this_round = min(per_round, remaining)
        else:
            this_round = per_round

        res = cleaner.run_once(limit=this_round, dry_run=dry_run)
        picked = res.get("picked", 0)

        total["rounds"] += 1
        total["picked"] += picked
        total["done"] += res.get("done", 0)
        total["failed"] += res.get("failed", 0)
        if res.get("previews"):
            total["previews"].extend(res["previews"])
        processed += picked

        # 熔断（多半是大模型服务宕了）→ 立即停手，别继续刷错误
        if res.get("circuit_broken"):
            total["circuit_broken"] = True
            log.error("⛔ 触发熔断，回填中止。请检查大模型服务后重试。")
            break

        # 干跑不写库，pending 集合不会变，再抽只会拿到同一批 → 只跑一轮做样本
        if dry_run:
            log.info("🔎 干跑仅抽取一批样本预览（不落库，故不继续翻页）。")
            break

        # 真跑：本轮抽到的不足一批，说明待清洗队列已清空 → 收工
        if picked < this_round:
            break

    log.info(f"✅ 回填结束：{json.dumps({k: v for k, v in total.items() if k != 'previews'}, ensure_ascii=False)}")
    return total


def run_concurrent(concurrency: int = 6, limit: int = 0, batch: int = None) -> dict:
    """并发回填：LLM 分析多线程并行（I/O 等待型），写库在主线程串行（单连接、线程安全）。

    相比串行 run() 可提速约 concurrency 倍（受 LLM 限流与端点并发能力制约）。
    仅用于真写库；保留人工编辑保护（fetch_pending 已过滤）；连续整轮失败止损。
    """
    from concurrent.futures import ThreadPoolExecutor
    from wxsearch.smart_dedup_store import SmartDedupStore
    from wxsearch.ai_filters.llm_analyzer import analyze, to_ai_result, build_event_fields, build_organizer_contact
    from wxsearch.ai_filters.llm_client import get_client

    cleaner = LLMCleaner.from_env()
    max_attempts = cleaner.max_attempts
    per_round = int(batch) if batch else max(concurrency * 4, concurrency)
    get_client()  # 预热客户端单例，避免多线程竞争初始化

    store = SmartDedupStore(cleaner.db_config)
    total = {"rounds": 0, "picked": 0, "done": 0, "failed": 0,
             "concurrency": concurrency, "limit": limit}
    processed, fail_streak = 0, 0

    def _work(row):
        _lid, _aid, _title, _content, _pub = row
        try:
            data = analyze(str(_title or ""), str(_content or ""), str(_pub) if _pub else None)
            return (row, data, None)
        except Exception as exc:  # noqa: BLE001
            return (row, None, exc)

    try:
        while True:
            if limit and limit > 0:
                remaining = limit - processed
                if remaining <= 0:
                    break
                n = min(per_round, remaining)
            else:
                n = per_round
            rows = store.fetch_pending_llm_leads(n)
            if not rows:
                break
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                results = list(ex.map(_work, rows))

            round_done, round_failed = 0, 0
            for row, data, err in results:
                lead_id, article_id = row[0], row[1]
                if err is not None:
                    store.mark_llm_failed(lead_id, max_attempts)
                    round_failed += 1
                    log.warning(f"清洗失败 lead_id={lead_id}: {err}")
                    continue
                try:
                    ai = to_ai_result(data)
                    name, details = build_event_fields(data)
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
                        organizer_contact=build_organizer_contact(data),
                        voting_status=data.get("voting_status", ""),
                        recurrence_period=data.get("recurrence_period", ""),
                        edition_no=data.get("edition_no"),
                    )
                    round_done += 1
                except Exception as exc:  # noqa: BLE001
                    store.mark_llm_failed(lead_id, max_attempts)
                    round_failed += 1
                    log.warning(f"写库失败 lead_id={lead_id}: {exc}")

            total["rounds"] += 1
            total["picked"] += len(rows)
            total["done"] += round_done
            total["failed"] += round_failed
            processed += len(rows)
            log.info(f"🧽 并发回填第{total['rounds']}轮(并发{concurrency}): 本轮 {round_done} 成/{round_failed} 败; 累计 done={total['done']} failed={total['failed']}")

            # 安全：连续整轮全失败 → 疑似大模型宕，止损
            if round_done == 0 and round_failed > 0:
                fail_streak += 1
                if fail_streak >= 2:
                    log.error("⛔ 连续整轮全失败，疑似大模型服务异常，回填中止。")
                    break
            else:
                fail_streak = 0
            if len(rows) < n:  # 队列已清空
                break
    finally:
        store.close()
    log.info(f"✅ 并发回填结束：{json.dumps(total, ensure_ascii=False)}")
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="存量线索 LLM 清洗回填（默认真写库；加 --dry-run 只预览）")
    parser.add_argument("--dry-run", action="store_true", help="只预览拟写入结果，不落库（建议先跑）")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理条数；0=全部")
    parser.add_argument("--batch", type=int, default=None, help="每轮抽取条数（节流），缺省用配置值")
    parser.add_argument("--concurrency", type=int, default=1, help="并发分析线程数(>1 启用并发提速，仅真写库)；1=串行")
    args = parser.parse_args()

    if args.concurrency and args.concurrency > 1 and not args.dry_run:
        result = run_concurrent(concurrency=args.concurrency, limit=args.limit, batch=args.batch)
    else:
        result = run(limit=args.limit, batch=args.batch, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
