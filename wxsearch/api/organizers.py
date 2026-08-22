"""主办方库 API（独立业务模块）。

定位：主办方库独立于「线索公海/AI活动库/我的活动库」，是业务线索的另一个来源。
与线索库仅两个交互点：
  ① 数据来源：线索 LLM 清洗时抽出的 organizer_* 字段，经聚合成主办方档案；
  ② 详情页跳转：线索详情页主办方命中本库时，用 /organizers/lookup 查到档案 id 后可跳转。

聚合逻辑在 wxsearch.organizer_aggregator（与 Celery Beat 定时重建共用）。
数据访问遵循 DatabaseConnector 约定：只用 execute_query / execute_write（自治事务）。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Depends
from fastapi.responses import Response

from ..db_connector import DatabaseConnector
from .auth import get_current_user, require_admin
from ..organizer_aggregator import (
    rebuild_organizers, norm_org_name, norm_event, aggregate_leads, _LEAD_SELECT,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _row_to_dict(row: tuple) -> dict:
    """organizers 表行 → dict（列顺序须与查询一致）。"""
    return {
        "id": row[0], "canonical_name": row[1], "aliases": row[2] or [],
        "contact": row[3] or {}, "region": row[4], "voting_platforms": row[5] or [],
        "event_count": row[6], "lead_ids": row[7] or [],
        "first_activity_at": row[8].isoformat() if row[8] else None,
        "last_activity_at": row[9].isoformat() if row[9] else None,
        "updated_by_human": row[10],
    }


_ORG_COLS = ("id, canonical_name, aliases, contact, region, voting_platforms, "
             "event_count, lead_ids, first_activity_at, last_activity_at, updated_by_human")

_ORG_SORT = {
    "event_count": "event_count",
    "last_activity_at": "last_activity_at",
    "canonical_name": "canonical_name",
}


@router.get("/organizers/")
async def list_organizers(
    limit: int = Query(default=20, le=500),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = None,       # 主办方名/别名/地区 模糊
    region: Optional[str] = None,       # 精确地区筛选
    sort_by: Optional[str] = None,      # event_count|last_activity_at|canonical_name
    sort_dir: Optional[str] = None,     # asc|desc
    current_user: dict = Depends(get_current_user),
):
    """主办方档案列表（默认按举办次数降序）。已被人工合并的档案(merged_into 非空)不展示。"""
    db = DatabaseConnector()
    where = "WHERE merged_into IS NULL"
    params: list = []
    if search:
        where += (" AND (canonical_name ILIKE %s OR region ILIKE %s "
                  "OR aliases::text ILIKE %s)")
        like = f"%{search}%"
        params += [like, like, like]
    if region:
        where += " AND region = %s"
        params.append(region)

    col = _ORG_SORT.get(sort_by or "", "event_count")
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    total = db.execute_query(f"SELECT COUNT(*) FROM organizers {where}", tuple(params))[0][0]
    rows = db.execute_query(
        f"SELECT {_ORG_COLS} FROM organizers {where} "
        f"ORDER BY {col} {direction} NULLS LAST, id DESC LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    )
    return {"total": total, "items": [_row_to_dict(r) for r in rows]}


@router.get("/organizers/lookup")
async def lookup_organizer(
    name: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_user),
):
    """交互点②：按主办方名归一键查档案。命中返回 {id, canonical_name}，否则 {id: None}。

    线索详情页拿到线索的 organizer_name 调此接口；命中即把主办方名渲染成可点击链接。
    """
    key = norm_org_name(name)
    if not key:
        return {"id": None}
    db = DatabaseConnector()
    rows = db.execute_query(
        "SELECT id, canonical_name, COALESCE(merged_into, id) FROM organizers WHERE norm_key = %s",
        (key,),
    )
    if not rows:
        return {"id": None}
    # 命中被合并档案则跳主档案
    target = rows[0][2]
    if target != rows[0][0]:
        t = db.execute_query("SELECT id, canonical_name FROM organizers WHERE id = %s", (target,))
        if t:
            return {"id": t[0][0], "canonical_name": t[0][1]}
    return {"id": rows[0][0], "canonical_name": rows[0][1]}


@router.get("/organizers/{oid:int}")
async def get_organizer(oid: int, current_user: dict = Depends(get_current_user)):
    """主办方档案详情 + 下钻其名下活动列表（按活动归一去重，取每活动最新一条代表）。"""
    db = DatabaseConnector()
    rows = db.execute_query(f"SELECT {_ORG_COLS} FROM organizers WHERE id = %s", (oid,))
    if not rows:
        raise HTTPException(status_code=404, detail="主办方不存在")
    org = _row_to_dict(rows[0])

    # 名下活动：按 lead_ids 取线索，前端按活动展示（这里返回明细，去重由活动归一键做）
    lead_ids = org["lead_ids"]
    activities = []
    if lead_ids:
        detail = db.execute_query(
            "SELECT id, event_name, title, activity_status, publish_time, "
            "NULLIF(COALESCE(url,''),'') AS url, voting_platform, online_voting_url "
            "FROM qualified_leads WHERE id = ANY(%s) "
            "ORDER BY COALESCE(publish_time, created_at) DESC",
            (lead_ids,),
        )
        seen = set()
        for d in detail:
            k = norm_event(d[1], d[2])
            if k in seen:
                continue
            seen.add(k)
            activities.append({
                "lead_id": d[0], "event_name": d[1] or "", "title": d[2] or "",
                "activity_status": d[3] or "", "url": d[5],
                "publish_time": d[4].isoformat() if d[4] else None,
                "voting_platform": d[6] or "", "online_voting_url": d[7] or "",
            })
    org["activities"] = activities
    return org


@router.post("/organizers/rebuild")
async def rebuild(current_user: dict = Depends(require_admin)):
    """全量重建主办方库（管理员）。存量线索回填清洗后跑一次即可看到档案。"""
    db = DatabaseConnector()
    return rebuild_organizers(db)


@router.post("/organizers/{oid:int}/merge")
async def merge_organizer(
    oid: int, target_id: int = Query(..., description="并入的目标主档案 id"),
    current_user: dict = Depends(require_admin),
):
    """人工合并：把 oid 并入 target_id。oid 记 merged_into 后从列表隐藏，
    target 吸收两者线索并按合并后线索集重算档案（口径与 rebuild 一致）。"""
    if oid == target_id:
        raise HTTPException(status_code=400, detail="不能合并自身")
    db = DatabaseConnector()
    src = db.execute_query("SELECT lead_ids FROM organizers WHERE id = %s", (oid,))
    tgt = db.execute_query("SELECT lead_ids FROM organizers WHERE id = %s", (target_id,))
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="源或目标主办方不存在")

    merged_ids = sorted({int(x) for x in (src[0][0] or [])} | {int(x) for x in (tgt[0][0] or [])})
    rows = db.execute_query(_LEAD_SELECT + "WHERE id = ANY(%s)", (merged_ids,)) if merged_ids else []
    agg = aggregate_leads(rows) if rows else {
        "canonical_name": "", "aliases": [], "contact": {}, "region": None,
        "voting_platforms": [], "event_count": 0, "lead_ids": merged_ids,
        "first_activity_at": None, "last_activity_at": None,
    }
    # 目标：吸收合并结果并标记人工编辑（后续 rebuild 不再覆盖 canonical/contact/region）
    db.execute_write(
        """
        UPDATE organizers SET
            aliases = %s::jsonb, contact = %s::jsonb, region = COALESCE(region, %s),
            voting_platforms = %s::jsonb, event_count = %s, lead_ids = %s::jsonb,
            first_activity_at = %s, last_activity_at = %s,
            updated_by_human = TRUE, updated_at = NOW()
        WHERE id = %s
        """,
        (
            json.dumps(agg["aliases"], ensure_ascii=False),
            json.dumps(agg["contact"], ensure_ascii=False),
            agg["region"],
            json.dumps(agg["voting_platforms"], ensure_ascii=False),
            agg["event_count"], json.dumps(agg["lead_ids"]),
            agg["first_activity_at"], agg["last_activity_at"], target_id,
        ),
    )
    # 源：标记已并入，隐藏出列表
    db.execute_write(
        "UPDATE organizers SET merged_into = %s, updated_by_human = TRUE, updated_at = NOW() WHERE id = %s",
        (target_id, oid),
    )
    return {"ok": True, "target_id": target_id, "merged_lead_count": len(merged_ids)}


@router.get("/organizers/export.csv")
async def export_organizers(
    search: Optional[str] = None, region: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """导出主办方库为 CSV（继承 search/region 过滤；带 BOM 供 Excel 正确识别 UTF-8）。"""
    db = DatabaseConnector()
    where = "WHERE merged_into IS NULL"
    params: list = []
    if search:
        where += " AND (canonical_name ILIKE %s OR region ILIKE %s OR aliases::text ILIKE %s)"
        like = f"%{search}%"
        params += [like, like, like]
    if region:
        where += " AND region = %s"
        params.append(region)
    rows = db.execute_query(
        f"SELECT {_ORG_COLS} FROM organizers {where} ORDER BY event_count DESC NULLS LAST",
        tuple(params),
    )
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["主办方", "地区", "举办次数", "评选系统", "联系方式", "首次活动", "最近活动", "别名"])
    for r in rows:
        o = _row_to_dict(r)
        contact = o["contact"] or {}
        contact_str = json.dumps(contact, ensure_ascii=False) if contact else ""
        w.writerow([
            o["canonical_name"], o["region"] or "", o["event_count"],
            "、".join(o["voting_platforms"]), contact_str,
            o["first_activity_at"] or "", o["last_activity_at"] or "",
            "、".join(o["aliases"]),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=organizers.csv"},
    )
