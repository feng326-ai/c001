"""判断进化 · Phase 0 评测工具（只读）。

作用：把 LLM 对每条线索的判断，与搜集人员的人工分型(lead_user_state.human_label)对照，
产出三个维度的一致率、分歧明细与错误类型分布，作为"进化"前的准确率基线。

三个判断维度：
  1) 是否有活动   —— LLM: qualified_leads.has_lead_value；人工: 有活动类=True / 无效·垃圾=False
  2) 线上 vs 线下 —— LLM: is_online_voting(True=线上/False=线下)；人工: 有活动=线上 / 线下专家评选·非优质-无线上评选=线下
  3) 是否优质     —— LLM: resource_level(excellent=优)；人工: 优质=优 / 普通·非优质-*=非优

设计：
  - 纯只读：只 SELECT，不写库、不改配置、不训练；任何异常只打印不抛出。
  - 人工分型是"单选"，一条只带部分维度信号；本工具按维度分别对照有信号的部分，
    并显式报告"单标签无法同时独立表达三维"这一局限（供是否拆成正交字段决策参考）。

用法（宿主机）：
    docker exec wxsearch_worker python -m wxsearch.ai_filters.judgment_eval
    docker exec wxsearch_worker python -m wxsearch.ai_filters.judgment_eval --limit 10   # 每类分歧示例数
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from urllib.parse import urlsplit

import psycopg2

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def _db_config() -> dict:
    """从 DATABASE_URL 解析连接参数（与 tasks._db_config 同源，此处自带以免引入 celery）。"""
    url = os.getenv("DATABASE_URL")
    if url:
        p = urlsplit(url)
        return {
            "host": p.hostname or "localhost", "port": p.port or 5432,
            "database": (p.path or "/wx_search").lstrip("/") or "wx_search",
            "user": p.username or "admin", "password": p.password or "",
        }
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "database": os.getenv("POSTGRES_DB", "wx_search"),
        "user": os.getenv("POSTGRES_USER", "admin"),
        "password": os.getenv("POSTGRES_PASSWORD", "your_secure_password_here"),
    }


# ---- 人工分型 → 三维判断（单标签只带部分维度，未知维度返回 None）----
_HAS_ACTIVITY_TRUE = {"有活动", "优质", "普通", "需跟进确认", "线下专家评选",
                      "非优质-无线上评选", "非优质-活动太小"}
_HAS_ACTIVITY_FALSE = {"无效", "垃圾"}
_MODE_ONLINE = {"有活动"}                              # "有活动"=有线上活动
_MODE_OFFLINE = {"线下专家评选", "非优质-无线上评选"}
_QUALITY_PREMIUM = {"优质"}
_QUALITY_NORMAL = {"普通", "非优质-无线上评选", "非优质-活动太小"}


def human_dims(label: str) -> dict:
    """把一个 human_label 映射为它能表达的维度（无信号维度为 None）。"""
    d = {"has_activity": None, "mode": None, "quality": None}
    if label in _HAS_ACTIVITY_FALSE:
        d["has_activity"] = False
        return d
    if label in _HAS_ACTIVITY_TRUE:
        d["has_activity"] = True
    if label in _MODE_ONLINE:
        d["mode"] = "online"
    elif label in _MODE_OFFLINE:
        d["mode"] = "offline"
    if label in _QUALITY_PREMIUM:
        d["quality"] = "premium"
    elif label in _QUALITY_NORMAL:
        d["quality"] = "normal"
    return d


def llm_dims(has_lead_value, is_online_voting, resource_level) -> dict:
    """把 LLM 的落库字段映射为三维判断。"""
    return {
        "has_activity": bool(has_lead_value),
        # 注：is_online_voting=False 仅表示"无线上投票环节"，不完全等于线下（schema 局限，见报告结尾）。
        "mode": "online" if is_online_voting else "offline",
        "quality": "premium" if resource_level == "excellent" else "normal",
    }


def run(limit_examples: int = 5) -> None:
    conn = psycopg2.connect(**_db_config())
    cur = conn.cursor()

    # ===== 一、LLM 判断分布基线（全部已清洗线索，无需人工标注即可看）=====
    cur.execute("""
        SELECT has_lead_value, is_online_voting, resource_level, llm_status
        FROM qualified_leads
    """)
    rows = cur.fetchall()
    cleaned = [r for r in rows if r[3] == "done"]
    print("=" * 64)
    print(f"一、LLM 判断分布基线（线索总 {len(rows)} 条，其中已清洗 {len(cleaned)} 条）")
    if cleaned:
        act = Counter("有活动" if r[0] else "无活动/非线索" for r in cleaned)
        mode = Counter("线上(有投票)" if r[1] else "线下/无投票" for r in cleaned)
        qual = Counter({"excellent": "优", "normal": "普", "poor": "低"}.get(r[2], "未定") for r in cleaned)
        print("  是否有活动 :", dict(act))
        print("  线上 vs 线下:", dict(mode))
        print("  是否优质   :", dict(qual))
    else:
        print("  （暂无已清洗线索）")

    # ===== 二、人工分型覆盖情况 =====
    cur.execute("""
        SELECT q.id, q.has_lead_value, q.is_online_voting, q.resource_level,
               s.human_label, q.title
        FROM qualified_leads q
        JOIN lead_user_state s ON s.lead_id = q.id
        WHERE COALESCE(s.human_label, '') <> ''
    """)
    labeled = cur.fetchall()
    print("=" * 64)
    print(f"二、人工分型覆盖：已标注 {len(labeled)} 条")
    if not labeled:
        print("  ⚠️ 尚无人工分型数据 —— 分歧报告需要先在看板逐条反馈（行内“反馈”按钮）。")
        print("     建议：先标注 ~100 条作为评测集，再重跑本工具即可得到基线准确率。")
        _print_caveats()
        conn.close()
        return

    print("  标签分布：", dict(Counter(r[4] for r in labeled)))

    # ===== 三、按维度对照（只在人工该维度有信号时计入）=====
    dims = ["has_activity", "mode", "quality"]
    dim_cn = {"has_activity": "是否有活动", "mode": "线上vs线下", "quality": "是否优质"}
    stats = {d: {"n": 0, "agree": 0, "examples": []} for d in dims}

    for lead_id, hlv, iov, rl, label, title in labeled:
        hd, ld = human_dims(label), llm_dims(hlv, iov, rl)
        for d in dims:
            hv = hd[d]
            if hv is None:
                continue                     # 该标签不表达此维度，跳过
            stats[d]["n"] += 1
            if hv == ld[d]:
                stats[d]["agree"] += 1
            elif len(stats[d]["examples"]) < limit_examples:
                stats[d]["examples"].append(
                    f"#{lead_id} 人工={hv} / LLM={ld[d]} | {label} | {(title or '')[:30]}")

    print("=" * 64)
    print("三、按维度一致率（分母=人工在该维度有信号的条数）")
    for d in dims:
        n, a = stats[d]["n"], stats[d]["agree"]
        rate = f"{a / n * 100:.1f}%" if n else "—"
        print(f"\n  【{dim_cn[d]}】可比 {n} 条，一致 {a}，一致率 {rate}")
        for ex in stats[d]["examples"]:
            print(f"      分歧例: {ex}")

    _print_caveats()
    conn.close()


def _print_caveats() -> None:
    print("=" * 64)
    print("说明与局限（供决策参考）：")
    print("  1) 人工分型是单选，一条只带部分维度信号，无法同时独立表达"
          "『有活动+线上/线下+优质』三维；")
    print("     若要做严格三维评测，需把人工分型拆成 3 个正交字段（决策点②相关）。")
    print("  2) LLM 的 is_online_voting=False 仅表示『无线上投票』，不完全等于『线下』；")
    print("     显式线上/线下需新增 activity_mode 字段后才能精确对照。")
    print("  3) 本工具只读：不写库、不改配置、不训练。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="判断进化 Phase 0 只读评测：LLM vs 人工分型")
    ap.add_argument("--limit", type=int, default=5, help="每维度打印的分歧示例条数")
    args = ap.parse_args()
    try:
        run(limit_examples=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"[judgment_eval] 运行异常（只读工具，不影响线上）：{exc}")
