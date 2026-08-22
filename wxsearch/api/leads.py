"""
线索管理 API

线索来源表为 qualified_leads（由 worker/回填在文章判定为线索时「提升」写入）。
本模块只负责线索列表查询；统计与人工标注反馈由 main.py 统一提供（避免同路径重复注册）。

注意：项目的 DatabaseConnector 不提供 commit/rollback，且 cursor() 借出不归还连接，
所有访问必须走 execute_query / execute_write（详见 db_connector.py）。
"""

from fastapi import APIRouter, Query, HTTPException, Depends, Request
from fastapi.responses import Response
from typing import Optional
import csv
import io
from datetime import datetime

from ..db_connector import DatabaseConnector
from .auth import get_current_user, require_admin

# 个人状态(lead_user_state 别名 s)表达式：无行时按默认 false/0。
_LUS_JOIN = "LEFT JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s"
_P_PROCESSED = "COALESCE(s.processed, FALSE)"
_P_HIDDEN = "COALESCE(s.hidden, FALSE)"
_P_INLIB = "COALESCE(s.in_library, FALSE)"
_P_MARK = ("CASE WHEN COALESCE(s.hidden,FALSE) THEN 'trash' "
           "WHEN COALESCE(s.processed,FALSE) THEN 'done' ELSE 'pending_followup' END")


def _upsert_state(db, user_id: int, lead_id: int, field: str, value):
    """UPSERT 当前用户对某线索的个人状态字段(processed/hidden/in_library/notes/llm_feedback/human_label)。"""
    if field not in ("processed", "hidden", "in_library", "notes", "llm_feedback", "human_label"):
        raise HTTPException(status_code=400, detail=f"非法个人状态字段：{field}")
    return db.execute_write(
        f"""
        INSERT INTO lead_user_state (user_id, lead_id, {field}, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id, lead_id) DO UPDATE SET {field} = EXCLUDED.{field}, updated_at = NOW()
        """,
        (user_id, lead_id, value),
    )

router = APIRouter()

# 列表返回给看板的列（顺序需与 SELECT 一致，用于把元组行拼成 dict）。
_LEAD_COLUMNS = [
    "id", "article_id", "keyword", "title", "article_url",
    "intent_category", "corrected_category", "lead_type",
    "priority_score", "priority_level", "mark_status",
    "account", "source_channel", "publish_time", "created_at",
    "summary", "scoring_breakdown", "llm_reasoning", "content",
    "resource_level", "channels",
    "event_name", "event_details", "notes", "llm_status",
    "is_online_voting", "online_voting_url",
    "collected_at", "is_recurring", "activity_category",
    "activity_region", "recurrence", "activity_status",
    "organizer_name", "organizer_region", "voting_platform", "organizer_contact",
    "in_library", "llm_feedback",
    "human_label",
    "group_count",
]

# 可排序列白名单：前端传入的 sort_by 必须在此，避免 SQL 注入。
_SORTABLE = {
    "priority_score": "q.priority_score",
    "publish_time": "q.publish_time",
    "collected_at": "q.collected_at",
    "created_at": "q.created_at",
}


def _norm_title_sql(col: str) -> str:
    """标题归一化表达式（归并键与「同活动来源」共用，避免两处口径漂移）。

    两步：
      1) 去掉全部标点与空白 —— 抹平 PC/搜狗的全半角差异；
      2) 再抹掉开头的「截止期」噪音前缀（9.10截止 / 9.10截稿 / 倒计时 / 最后3天…）——
         转载号常自行加这类前缀，同一活动会因此被拆成多条（如
         「9.10截止//…浙江赛区…」与「9.10截稿——…浙江赛区…」）。

    刻意只抹日期/截止类前缀，绝不抹【铜川市初赛】这种城市或分赛标记：
    同一赛事的各城市分赛是彼此独立的子活动（各有报名入口），必须保持分开。
    """
    return ("regexp_replace(regexp_replace(lower(" + col + "), '[^[:alnum:]]', '', 'g'), "
            "'^[0-9]{0,6}(截止|截稿|即将截止|倒计时|最后[0-9]{0,3}天)[0-9]{0,6}', '', 'g')")


def _build_where(status: Optional[str], category: Optional[str], search: Optional[str],
                 prefix: str = "", online_voting: Optional[bool] = None,
                 include_non_lead: bool = False, region: Optional[str] = None,
                 resource_level: Optional[str] = None, recurrence: Optional[str] = None,
                 in_library: Optional[bool] = None, days: Optional[int] = None,
                 voting_not_started: bool = False,
                 activity_status: Optional[str] = None,
                 exclude_in_library: Optional[bool] = None):
    """根据筛选参数拼装 WHERE 子句与参数列表（列表查询与导出共用，避免两处逻辑漂移）。

    prefix: 列名前缀（如 'q.'），用于 JOIN 查询时消除列名歧义；缺省不加前缀。
    include_non_lead: 默认 False 隐藏“LLM 已清洗且判定非线索”的条目（广告/软文等）；
                     未清洗的仍保留，避免清洗前消失。传 True 可查看全部。"""
    where = "WHERE 1=1"
    params: list = []
    # 状态四态(个人)：未处理/已处理/所有线索(不含回收站)/回收站；缺省按未处理。
    if status == "trash":
        where += f" AND {_P_HIDDEN} = TRUE"
    elif status == "done":
        where += f" AND {_P_PROCESSED} = TRUE AND {_P_HIDDEN} = FALSE"
    elif status == "all":
        where += f" AND {_P_HIDDEN} = FALSE"  # 所有线索（不含回收站）
    elif status == "unprocessed":
        where += f" AND {_P_PROCESSED} = FALSE AND {_P_HIDDEN} = FALSE"
    else:
        where += f" AND {_P_PROCESSED} = FALSE AND {_P_HIDDEN} = FALSE"  # 默认=未处理
    if category:
        where += f" AND {prefix}intent_category = %s"
        params.append(category)
    if search:
        where += f" AND ({prefix}title ILIKE %s OR {prefix}keyword ILIKE %s OR {prefix}event_name ILIKE %s)"
        like = f"%{search}%"
        params.extend([like, like, like])
    if online_voting:
        where += f" AND {prefix}is_online_voting = TRUE"
    if voting_not_started:
        # AI 活动库口径：只要“投票尚未开始”的资源——活动状态由 LLM 判定，
        # 排除「进行中/已结束」（投票已在跑或已完）；征集/报名/未知保留。
        where += f" AND COALESCE({prefix}activity_status, '') NOT IN ('进行中', '已结束')"
    if region:
        where += f" AND {prefix}activity_region = %s"
        params.append(region)
    if resource_level:
        where += f" AND {prefix}resource_level = %s"
        params.append(resource_level)
    if recurrence:
        where += f" AND {prefix}recurrence = %s"
        params.append(recurrence)
    if days:
        where += f" AND {prefix}publish_time >= NOW() - make_interval(days => %s)"
        params.append(int(days))
    if in_library:
        where += f" AND {_P_INLIB} = TRUE"
    if exclude_in_library:
        where += f" AND {_P_INLIB} = FALSE"
    if activity_status:
        if activity_status == "recruiting":
            where += f" AND {prefix}activity_status IN ('征集中', '报名中')"
        else:
            where += f" AND {prefix}activity_status = %s"
            params.append(activity_status)
    if not include_non_lead:
        # 隐藏 LLM 清洗后确认无价值的（广告/软文）：仅当 llm_status='done' 且 has_lead_value=FALSE 时排除。
        where += f" AND NOT ({prefix}llm_status = 'done' AND {prefix}has_lead_value = FALSE)"
    return where, params


@router.get("/leads/")
async def list_leads(
    limit: int = Query(default=20, le=500),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,       # qualified_leads.status
    category: Optional[str] = None,     # intent_category：评选/投票/征集/活动...
    search: Optional[str] = None,       # 标题 / 关键词模糊匹配
    online_voting: Optional[bool] = None,  # 仅看支持线上投票的线索
    voting_not_started: bool = False,      # AI 活动库口径：排除活动状态进行中/已结束
    sort_by: Optional[str] = None,      # 排序列：priority_score|publish_time|collected_at|created_at
    sort_dir: Optional[str] = None,     # asc|desc
    include_non_lead: bool = False,     # 默认隐藏 LLM 已判定非线索（广告/软文）；true 显示全部
    group_by_event: bool = True,        # 默认按活动名归并同活动多来源（跨公众号转载）
    region: Optional[str] = None,       # 地区筛选：全国/省/市/县/镇
    resource_level: Optional[str] = None,  # 资源质量筛选：excellent/normal/poor（快捷“优”）
    recurrence: Optional[str] = None,   # 届次筛选：多届/第一届/单届（快捷“多届”）
    in_library: Optional[bool] = None,  # 仅看已加入我的活动库
    days: Optional[int] = None,         # 时间筛选：发布时间近 N 天（今天1/三天3/七天7/半月15）
    activity_status: Optional[str] = None,  # 活动阶段筛选：recruiting(征集报名中)/进行中
    exclude_in_library: Optional[bool] = None,  # 漏斗：排除已入活动库的（仅当前用户）
    filter_mode: Optional[str] = None,  # 视图模式：business(商机)/verify(待核实)/resource(资源池)
    current_user: dict = Depends(get_current_user),
):
    """
    获取线索列表（默认按采集入库时间倒序，可由 sort_by/sort_dir 改变）。

    GET /api/v1/leads/?limit=20&offset=0&sort_by=collected_at&sort_dir=desc
    默认过滤掉 LLM 清洗后确认无价值的广告/软文（has_lead_value=false）；include_non_lead=true 可查全部。
    默认按活动名(event_name 归一)将同一活动的多公众号转载折叠为一条（带 group_count 来源数）；group_by_event=false 不折叠。
    服务端排序覆盖全部分页（非仅当前页）。
    """
    db = DatabaseConnector()
    me = current_user["id"]

    where, params = _build_where(status, category, search, online_voting=online_voting, include_non_lead=include_non_lead, region=region, resource_level=resource_level, recurrence=recurrence, in_library=in_library, days=days, voting_not_started=voting_not_started, activity_status=activity_status, exclude_in_library=exclude_in_library)
    where_q, _ = _build_where(status, category, search, "q.", online_voting, include_non_lead, region=region, resource_level=resource_level, recurrence=recurrence, in_library=in_library, days=days, voting_not_started=voting_not_started, activity_status=activity_status, exclude_in_library=exclude_in_library)
    # 视图模式过滤（双轨制架构）
    if filter_mode == 'business':  # 商机池：有效且有投票
        where += " AND has_lead_value=TRUE AND COALESCE(voting_status,'')='has'"
        where_q += " AND q.has_lead_value=TRUE AND COALESCE(q.voting_status,'')='has'"
    elif filter_mode == 'verify':  # 待核实池：有效但投票未确认
        where += " AND has_lead_value=TRUE AND COALESCE(voting_status,'')='suspect'"
        where_q += " AND q.has_lead_value=TRUE AND COALESCE(q.voting_status,'')='suspect'"
    elif filter_mode == 'resource':  # 资源池：进行中/已结束的活动
        where += " AND activity_status IN ('进行中','已结束') AND has_lead_value=FALSE"
        where_q += " AND q.activity_status IN ('进行中','已结束') AND q.has_lead_value=FALSE"

    # 排序：白名单列 + 方向，缺省或非法则回退采集入库时间降序。
    sort_col = _SORTABLE.get(sort_by or "")   # 形如 q.xxx
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    # 列（与 _LEAD_COLUMNS 对应，不含 group_count）
    _cols = f"""q.id, q.article_id, q.keyword, q.title,
               NULLIF(COALESCE(q.url, ''), '')  AS article_url,
               q.intent_category, q.intent_category AS corrected_category,
               q.lead_type, q.priority_score, q.priority_level,
               {_P_MARK} AS mark_status, q.account, q.source_channel,
               q.publish_time, q.created_at, q.summary, q.scoring_breakdown,
               q.llm_reasoning, q.content, q.resource_level,
               string_to_array(COALESCE(NULLIF(a.source_channels, ''), q.source_channel), ',') AS channels,
               q.event_name, q.event_details, s.notes AS notes, q.llm_status,
               q.is_online_voting, q.online_voting_url,
               q.collected_at, q.is_recurring, q.activity_category,
               q.activity_region, q.recurrence, q.activity_status,
               q.organizer_name, q.organizer_region, q.voting_platform, q.organizer_contact,
               {_P_INLIB} AS in_library, COALESCE(s.llm_feedback,0) AS llm_feedback,
               COALESCE(s.human_label,'') AS human_label"""
    # 归并键：优先按【标题归一】折叠（去标点/空白 + 抹掉开头截止期噪音，见 _norm_title_sql）；
    # 同标题即同活动——确定性信号，不受 LLM 抽取波动影响；标题为空才回退活动名+地区+届次；均空用 id 各自成组。
    _nt = _norm_title_sql("q.title")
    _gk = ("CASE "
           f"WHEN COALESCE(NULLIF({_nt}, ''), '') <> '' "
           f"THEN 'tt:' || {_nt} "
           "WHEN COALESCE(NULLIF(regexp_replace(lower(q.event_name), '[[:space:]]', '', 'g'), ''), '') <> '' "
           "THEN 'ev:' || regexp_replace(lower(q.event_name), '[[:space:]]', '', 'g') "
           "|| '|' || COALESCE(q.activity_region, '') || '|' || COALESCE(q.recurrence, '') "
           "ELSE 'id:' || q.id::text END")

    if group_by_event:
        col_np = sort_col.replace("q.", "") if sort_col else None
        order_by = (f"ORDER BY {col_np} {direction} NULLS LAST, id DESC" if col_np
                    else "ORDER BY publish_time DESC NULLS LAST, id DESC")
        rows = db.execute_query(f"""
            WITH base AS (
                SELECT {_cols}, {_gk} AS group_key
                FROM qualified_leads q
                LEFT JOIN articles_core a ON a.id = q.article_id
                {_LUS_JOIN}
                {where_q}
            ),
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (PARTITION BY group_key ORDER BY priority_score DESC NULLS LAST, COALESCE(collected_at, created_at) DESC NULLS LAST, id DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY group_key) AS group_count
                FROM base
            )
            SELECT id, article_id, keyword, title, article_url, intent_category, corrected_category,
                   lead_type, priority_score, priority_level, mark_status, account, source_channel,
                   publish_time, created_at, summary, scoring_breakdown, llm_reasoning, content,
                   resource_level, channels, event_name, event_details, notes, llm_status,
                   is_online_voting, online_voting_url, collected_at, is_recurring, activity_category,
                   activity_region, recurrence, activity_status,
                   organizer_name, organizer_region, voting_platform, organizer_contact,
                   in_library, llm_feedback,
                   human_label,
                   group_count
            FROM ranked WHERE rn = 1
            {order_by}
            LIMIT %s OFFSET %s
        """, tuple([me] + params + [limit, offset]))
        total = db.execute_query(
            f"SELECT COUNT(*) FROM (SELECT {_gk} AS gk FROM qualified_leads q {_LUS_JOIN} {where_q} GROUP BY gk) t",
            tuple([me] + params),
        )[0][0]
    else:
        order_by = (f"ORDER BY {sort_col} {direction} NULLS LAST, q.id DESC" if sort_col
                    else "ORDER BY q.publish_time DESC NULLS LAST, q.id DESC")
        rows = db.execute_query(f"""
            SELECT {_cols}, 1 AS group_count
            FROM qualified_leads q
            LEFT JOIN articles_core a ON a.id = q.article_id
            {_LUS_JOIN}
            {where_q}
            {order_by}
            LIMIT %s OFFSET %s
        """, tuple([me] + params + [limit, offset]))
        total = db.execute_query(
            f"SELECT COUNT(*) FROM qualified_leads q {_LUS_JOIN} {where_q}", tuple([me] + params)
        )[0][0]

    data = [dict(zip(_LEAD_COLUMNS, row)) for row in rows]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": data,
    }


@router.get("/leads/{lead_id}/sources")
async def lead_sources(lead_id: int, current_user: dict = Depends(get_current_user)):
    """返回与该线索同活动(event_name 归一)的所有来源线索（多公众号转载）。

    GET /api/v1/leads/{lead_id}/sources
    供看板展开“同活动 N 个来源”；无活动名时仅返回自身。
    """
    db = DatabaseConnector()
    r0 = db.execute_query("SELECT title, event_name FROM qualified_leads WHERE id = %s", (lead_id,))
    if not r0:
        raise HTTPException(status_code=404, detail=f"未找到线索 id={lead_id}")
    tt = (r0[0][0] or "").strip()
    ev = (r0[0][1] or "").strip()
    cols = ["id", "title", "account", "source_channel", "article_url", "collected_at", "llm_status", "priority_level"]
    _sel = ("SELECT id, title, account, source_channel, NULLIF(COALESCE(url,''),''), collected_at, llm_status, priority_level "
            "FROM qualified_leads")
    if tt:
        rows = db.execute_query(
            _sel + f" WHERE {_norm_title_sql('title')} = {_norm_title_sql('%s')} "
            "ORDER BY priority_score DESC NULLS LAST, collected_at DESC NULLS LAST", (tt,))
    elif ev:
        rows = db.execute_query(
            _sel + " WHERE regexp_replace(lower(event_name), '[[:space:]]', '', 'g') = regexp_replace(lower(%s), '[[:space:]]', '', 'g') "
            "ORDER BY priority_score DESC NULLS LAST, collected_at DESC NULLS LAST", (ev,))
    else:
        rows = db.execute_query(_sel + " WHERE id = %s", (lead_id,))
    return {"event_name": ev, "count": len(rows), "data": [dict(zip(cols, x)) for x in rows]}


# 渠道标识 → 友好名（导出时展示用；数据库仍保留原始值以便对接 OA）。
_CHANNEL_NAMES = {
    "wechat_pc": "微信搜一搜(PC)",
    "weixin_mobile": "微信搜一搜(手机)",
    "wechat_mobile": "微信搜一搜(手机)",
    "sogou_weixin": "搜狗微信",
    "sogou_wap": "搜狗微信",
    "baidu_news": "百度新闻",
}

# 资源质量 → 友好名（导出展示用）。
_RESOURCE_NAMES = {"excellent": "优", "normal": "普", "poor": "低"}
_VOTING_NAMES = {True: "有", False: "无", None: ""}

# 导出列注册表：key -> (表头, SQL 取值表达式)。前端传 cols(逗号分隔 key)选择列。
_EXPORT_COLS = {
    "id": ("ID", "id"),
    "keyword": ("关键词", "keyword"),
    "event_name": ("活动名称", "event_name"),
    "title": ("标题", "title"),
    "article_url": ("URL", "NULLIF(COALESCE(url,''),'')"),
    "account": ("来源账号", "account"),
    "source_channel": ("渠道", "source_channel"),
    "intent_category": ("意图分类", "intent_category"),
    "resource_level": ("资源质量", "resource_level"),
    "activity_region": ("地区", "activity_region"),
    "recurrence": ("多届", "recurrence"),
    "activity_status": ("活动状态", "activity_status"),
    "activity_category": ("活动类别", "activity_category"),
    "is_online_voting": ("线上投票", "is_online_voting"),
    "priority_score": ("优先级分数", "priority_score"),
    "priority_level": ("优先级等级", "priority_level"),
    "mark_status": ("状态", _P_MARK),
    "collected_at": ("采集时间", "collected_at"),
    "publish_time": ("发布时间", "publish_time"),
    "notes": ("备注/笔记", "s.notes"),
    "summary": ("摘要", "summary"),
}
_EXPORT_DEFAULT_ORDER = [
    "id", "keyword", "event_name", "title", "article_url", "account", "source_channel",
    "intent_category", "resource_level", "activity_region", "recurrence", "activity_status",
    "activity_category", "is_online_voting", "priority_score", "priority_level", "mark_status",
    "collected_at", "publish_time", "notes", "summary",
]


@router.get("/leads/export")
async def export_leads(
    status: Optional[str] = None,       # 与 /leads/ 相同的筛选参数
    category: Optional[str] = None,
    search: Optional[str] = None,
    online_voting: Optional[bool] = None,
    voting_not_started: bool = False,  # AI 活动库口径：排除活动状态进行中/已结束（与列表同口径）
    cols: Optional[str] = None,         # 逗号分隔的导出列 key（默认全部）
    include_non_lead: bool = False,     # 与列表一致：默认不导广告/非线索
    region: Optional[str] = None,       # 地区筛选：全国/省/市/县/镇
    in_library: Optional[bool] = None,  # 我的活动库导出
    activity_status: Optional[str] = None,  # 活动阶段筛选
    exclude_in_library: Optional[bool] = None,  # 漏斗：排除已入库
    current_user: dict = Depends(get_current_user),
):
    """按当前筛选条件导出全部匹配线索为 CSV（不受分页限制）。

    GET /api/v1/leads/export?cols=id,event_name,title&status=&category=评选
    cols 选定导出列（白名单，缺省全部）；带 UTF-8 BOM 便于 Excel 直接打开。
    """
    db = DatabaseConnector()
    me = current_user["id"]
    where, params = _build_where(status, category, search, online_voting=online_voting, include_non_lead=include_non_lead, region=region, voting_not_started=voting_not_started, in_library=in_library, activity_status=activity_status, exclude_in_library=exclude_in_library)

    keys = [k for k in (cols.split(",") if cols else []) if k in _EXPORT_COLS] or list(_EXPORT_DEFAULT_ORDER)
    select_sql = ", ".join(f"{_EXPORT_COLS[k][1]} AS {k}" for k in keys)
    rows = db.execute_query(
        f"SELECT {select_sql} FROM qualified_leads q {_LUS_JOIN} {where} "
        f"ORDER BY COALESCE(q.collected_at, q.created_at) DESC NULLS LAST, q.id DESC",
        tuple([me] + params),
    )

    # 用 csv 模块正确处理逗号/引号/换行的转义
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM，Excel 中文不乱码
    writer = csv.writer(buf)
    writer.writerow([_EXPORT_COLS[k][0] for k in keys])
    for r in rows:
        out = []
        for k, v in zip(keys, r):
            if k == "source_channel":
                v = _CHANNEL_NAMES.get(v, v)
            elif k == "resource_level":
                v = _RESOURCE_NAMES.get(v, v)
            elif k == "is_online_voting":
                v = _VOTING_NAMES.get(v, "")
            out.append("" if v is None else v)
        writer.writerow(out)

    filename = f"leads_export_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# 可由人工在看板悬浮窗中编辑的字段（白名单，防止误写其他列）。
_EDITABLE_FIELDS = ("event_name", "event_details", "notes")


@router.patch("/leads/{lead_id}/annotation")
async def update_annotation(lead_id: int, payload: dict, current_user: dict = Depends(get_current_user)):
    """保存人工补充：活动名称/活动详情为全局共享(纠正活动本身)，备注为个人私有。"""
    me = current_user["id"]
    db = DatabaseConnector()
    updated = []
    gsets, gparams = [], []
    for field in ("event_name", "event_details"):
        if field in payload:
            gsets.append(f"{field} = %s")
            gparams.append("" if payload[field] is None else str(payload[field]))
            updated.append(field)
    if gsets:
        gparams.append(lead_id)
        aff = db.execute_write(
            f"UPDATE qualified_leads SET {', '.join(gsets)}, updated_by_human = TRUE, updated_at = NOW() WHERE id = %s",
            tuple(gparams),
        )
        if not aff:
            raise HTTPException(status_code=404, detail=f"未找到线索 id={lead_id}")
    if "notes" in payload:
        _upsert_state(db, me, lead_id, "notes", "" if payload["notes"] is None else str(payload["notes"]))
        updated.append("notes")
    if not updated:
        raise HTTPException(status_code=400, detail="无可更新字段（event_name/event_details/notes 至少传一个）")
    return {"status": "saved", "lead_id": lead_id, "updated": updated}


# 线索状态白名单：未处理/已处理/回收站。
_STATUS_VALUES = {"pending_followup", "done", "trash"}


@router.patch("/leads/{lead_id}/status")
async def update_lead_status(lead_id: int, payload: dict, current_user: dict = Depends(get_current_user)):
    """变更【个人】线索状态：done(已处理)/trash(回收站,仅自己隐藏)/pending_followup(未处理/恢复)。"""
    status = str(payload.get("status") or "").strip()
    if status not in _STATUS_VALUES:
        raise HTTPException(status_code=400, detail=f"非法状态：{status}")
    me = current_user["id"]
    db = DatabaseConnector()
    if status == "trash":
        db.execute_write(
            """INSERT INTO lead_user_state (user_id, lead_id, hidden, updated_at)
               VALUES (%s, %s, TRUE, NOW())
               ON CONFLICT (user_id, lead_id) DO UPDATE SET hidden = TRUE, updated_at = NOW()""",
            (me, lead_id),
        )
    else:
        processed = (status == "done")
        db.execute_write(
            """INSERT INTO lead_user_state (user_id, lead_id, processed, hidden, updated_at)
               VALUES (%s, %s, %s, FALSE, NOW())
               ON CONFLICT (user_id, lead_id) DO UPDATE SET processed = EXCLUDED.processed, hidden = FALSE, updated_at = NOW()""",
            (me, lead_id, processed),
        )
    return {"status": "ok", "lead_id": lead_id, "new_status": status}


@router.delete("/leads/{lead_id}")
async def delete_lead(lead_id: int, current_user: dict = Depends(require_admin)):
    """永久删除线索（仅管理员；全局操作）。先清个人状态再删线索，避免外键报错。"""
    db = DatabaseConnector()
    db.execute_write("DELETE FROM lead_user_state WHERE lead_id = %s", (lead_id,))
    affected = db.execute_write("DELETE FROM qualified_leads WHERE id = %s", (lead_id,))
    if not affected:
        raise HTTPException(status_code=404, detail=f"未找到线索 id={lead_id}")
    return {"status": "deleted", "lead_id": lead_id}


@router.patch("/leads/{lead_id}/library")
async def toggle_library(lead_id: int, payload: dict, current_user: dict = Depends(get_current_user)):
    """加入/移出【我的】活动库(个人)。PATCH Body: {\"in_library\": true/false}。"""
    in_library = bool(payload.get("in_library", True))
    db = DatabaseConnector()
    _upsert_state(db, current_user["id"], lead_id, "in_library", in_library)
    return {"status": "ok", "lead_id": lead_id, "in_library": in_library}


@router.patch("/leads/{lead_id}/llm_feedback")
async def set_llm_feedback(lead_id: int, payload: dict, current_user: dict = Depends(get_current_user)):
    """对 LLM 判定的【个人】反馈。PATCH Body: {\"feedback\": 1|-1|0}。"""
    try:
        fb = int(payload.get("feedback", 0))
    except (TypeError, ValueError):
        fb = 0
    if fb not in (1, -1, 0):
        raise HTTPException(status_code=400, detail="feedback 只能为 1/-1/0")
    db = DatabaseConnector()
    _upsert_state(db, current_user["id"], lead_id, "llm_feedback", fb)
    return {"status": "ok", "lead_id": lead_id, "llm_feedback": fb}


# 人工分型白名单（搜集人员视角，供 AI 学习）；空串=取消标注。
# 行内快捷按钮用：有活动/线下专家评选/需跟进确认/非优质-*/无效/垃圾；详情弹窗用全集。
_HUMAN_LABELS = {
    "有活动", "优质", "普通", "无效", "垃圾", "需跟进确认",
    "线下专家评选", "非优质-无线上评选", "非优质-活动太小", "",
}


@router.patch("/leads/{lead_id}/human_label")
async def set_human_label(lead_id: int, payload: dict, current_user: dict = Depends(get_current_user)):
    """对线索的【个人】人工分型标注（每人一份，不互盖）。
    PATCH Body: {\"label\": \"有活动|无效|优质|普通|垃圾\"}；传空串=取消。"""
    label = str(payload.get("label", "") or "").strip()
    if label not in _HUMAN_LABELS:
        raise HTTPException(status_code=400, detail=f"非法人工分型：{label}")
    db = DatabaseConnector()
    _upsert_state(db, current_user["id"], lead_id, "human_label", label)
    return {"status": "ok", "lead_id": lead_id, "human_label": label}


@router.patch("/leads/batch/status")
async def batch_update_status(payload: dict, current_user: dict = Depends(get_current_user)):
    """批量变更【个人】线索状态（批量已处理/批量回收站）。

    PATCH Body: {"ids": [1,2,3], "status": "done"|"trash", "human_label": "无效"(可选)}
    限制单次最多 200 条，防误操作。
    """
    me = current_user["id"]
    ids = payload.get("ids") or []
    status = str(payload.get("status") or "").strip()
    human_label = str(payload.get("human_label") or "").strip()
    if status not in ("done", "trash"):
        raise HTTPException(status_code=400, detail="status 只能为 done 或 trash")
    if not ids or not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids 必须为非空数组")
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="单次最多 200 条")
    db = DatabaseConnector()
    for lid in ids:
        lid = int(lid)
        if status == "trash":
            _upsert_state(db, me, lid, "hidden", True)
        else:
            _upsert_state(db, me, lid, "processed", True)
            # done 时确保 hidden=FALSE（可能之前在回收站里，现在恢复）
            _upsert_state(db, me, lid, "hidden", False)
        if human_label and human_label in _HUMAN_LABELS:
            _upsert_state(db, me, lid, "human_label", human_label)
    return {"status": "ok", "count": len(ids), "new_status": status}
