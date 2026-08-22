"""主办方库聚合逻辑（独立于 FastAPI 层，供 API 与 Celery Beat 定时任务共用）。

抽出为独立模块的原因：重建任务要在 worker(celery-beat) 里跑，而 api/organizers.py 依赖
FastAPI/auth；把纯聚合逻辑放这里，worker 只 import 本模块即可，不牵扯 Web 层依赖。

聚合策略：保守精确归并（organizer_name 归一键 norm_key 精确匹配即同一主办方）。
数据访问遵循 DatabaseConnector 约定：只用 execute_query / execute_write（自治事务）。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

log = logging.getLogger(__name__)

# 主办方名称归一：去空白/括号/常见标点后小写，作为保守精确归并的键。
_PUNCT = re.compile(r"[\s()（）【】\[\]{}·・.,，。、:：;；\-—_/\\|＂\"'’“”]+")

# 从线索表捞主办方相关列的通用 SELECT 片段（顺序须与 _aggregate_leads 的 rows 对齐）。
_LEAD_SELECT = (
    "SELECT id, organizer_name, organizer_region, voting_platform, organizer_contact, "
    "event_name, title, COALESCE(publish_time, created_at) "
    "FROM qualified_leads "
)


def norm_org_name(name: str) -> str:
    """主办方名归一键：抹平空白/括号/标点/大小写差异，用于精确归并去重。"""
    return _PUNCT.sub("", str(name or "").strip().lower())


def norm_event(event_name: str, title: str) -> str:
    """活动归一键（举办次数按活动去重用）：优先 event_name，空则回退 title。"""
    base = (event_name or "").strip() or (title or "").strip()
    return _PUNCT.sub("", base.lower())


def merge_contacts(contacts: list) -> dict:
    """合并同一主办方名下多条结构化联系方式（取并集，人名收集为 persons 列表）。"""
    out: dict = {}
    persons: list = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        for k, v in c.items():
            if k == "person":
                if v and v not in persons:
                    persons.append(v)
            elif isinstance(v, list):
                bucket = out.setdefault(k, [])
                for it in v:
                    if it and it not in bucket:
                        bucket.append(it)
            elif v:
                bucket = out.setdefault(k, [])
                if v not in bucket:
                    bucket.append(v)
    if persons:
        out["persons"] = persons
    return out


def aggregate_leads(rows: list) -> dict:
    """把某主办方名下的线索行聚合为一份档案 dict（rebuild 与 merge 共用，口径一致）。

    rows 每行：(id, organizer_name, organizer_region, voting_platform,
                organizer_contact, event_name, title, activity_at)
    """
    names = [r[1] for r in rows if r[1]]
    canonical = Counter(names).most_common(1)[0][0] if names else ""
    aliases = sorted({n for n in names if n and n != canonical})

    regions = [r[2] for r in rows if r[2]]
    region = Counter(regions).most_common(1)[0][0] if regions else None

    platforms = sorted({r[3] for r in rows if r[3]})
    contact = merge_contacts([r[4] for r in rows])

    # 举办次数：按活动归一键去重（同一活动多篇文章算 1 次）
    event_keys = {norm_event(r[5], r[6]) for r in rows if norm_event(r[5], r[6])}
    event_count = len(event_keys)

    lead_ids = [r[0] for r in rows]
    times = [r[7] for r in rows if r[7]]
    first_at = min(times) if times else None
    last_at = max(times) if times else None

    return {
        "canonical_name": canonical, "aliases": aliases, "contact": contact,
        "region": region, "voting_platforms": platforms, "event_count": event_count,
        "lead_ids": lead_ids, "first_activity_at": first_at, "last_activity_at": last_at,
    }


def rebuild_organizers(db) -> dict:
    """从 qualified_leads 全量重建主办方档案（保守精确归并 by norm_key）。

    人工保护：ON CONFLICT 时，canonical_name/contact/region 若该档案 updated_by_human=TRUE
    则保留人工值；统计类字段（举办次数/线索/时间/平台/别名）始终按最新数据刷新。
    已被人工合并（merged_into 非空）的档案不在此重建（其线索已并入主档案）。
    """
    rows = db.execute_query(
        _LEAD_SELECT + "WHERE organizer_name IS NOT NULL AND organizer_name <> ''"
    )
    # 已被合并吸收的线索 id 集合：这些线索归主档案管，重建时跳过其原始 norm_key 分组。
    merged_lead_ids: set = set()
    for (lids,) in db.execute_query(
        "SELECT lead_ids FROM organizers WHERE merged_into IS NOT NULL AND lead_ids IS NOT NULL"
    ):
        for x in (lids or []):
            merged_lead_ids.add(int(x))

    groups: dict = {}
    for r in rows:
        if int(r[0]) in merged_lead_ids:
            continue
        key = norm_org_name(r[1])
        if not key:
            continue
        groups.setdefault(key, []).append(r)

    upserted = 0
    for key, grp in groups.items():
        agg = aggregate_leads(grp)
        db.execute_write(
            """
            INSERT INTO organizers
                (canonical_name, norm_key, aliases, contact, region, voting_platforms,
                 event_count, lead_ids, first_activity_at, last_activity_at, updated_at)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s::jsonb, %s, %s, NOW())
            ON CONFLICT (norm_key) DO UPDATE SET
                canonical_name   = CASE WHEN organizers.updated_by_human
                                        THEN organizers.canonical_name ELSE EXCLUDED.canonical_name END,
                contact          = CASE WHEN organizers.updated_by_human
                                        THEN organizers.contact ELSE EXCLUDED.contact END,
                region           = CASE WHEN organizers.updated_by_human
                                        THEN organizers.region ELSE EXCLUDED.region END,
                aliases          = EXCLUDED.aliases,
                voting_platforms = EXCLUDED.voting_platforms,
                event_count      = EXCLUDED.event_count,
                lead_ids         = EXCLUDED.lead_ids,
                first_activity_at= EXCLUDED.first_activity_at,
                last_activity_at = EXCLUDED.last_activity_at,
                updated_at       = NOW()
            WHERE organizers.merged_into IS NULL
            """,
            (
                agg["canonical_name"], key,
                json.dumps(agg["aliases"], ensure_ascii=False),
                json.dumps(agg["contact"], ensure_ascii=False),
                agg["region"],
                json.dumps(agg["voting_platforms"], ensure_ascii=False),
                agg["event_count"],
                json.dumps(agg["lead_ids"]),
                agg["first_activity_at"], agg["last_activity_at"],
            ),
        )
        upserted += 1
    log.info(f"🏛️ 主办方库重建完成：分组 {len(groups)}，写入 {upserted}")
    return {"groups": len(groups), "upserted": upserted, "leads_scanned": len(rows)}
